"""LLM 服务：DeepSeek（OpenAI 兼容协议）。

包含四个能力：
1. stream_answer  —— RAG 回答流式生成（附带 token 用量统计）
2. rewrite_query  —— 多轮对话查询改写（"那第二条呢" → 独立完整问题）
3. suggest_followups —— 回答后生成推荐追问
4. build_context  —— 检索片段拼接为带编号引用的参考资料
"""
import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings
from app.services.retriever import RetrievedChunk

logger = logging.getLogger(__name__)


# ---------------- 熔断器（进程级）----------------
# 连续失败 N 次后打开，冷却期内所有调用快速失败（不再等超时），冷却结束进入半开试探。
# 进程级状态对多实例部署来说是"每实例独立熔断"，这正是想要的行为。
class CircuitBreaker:
    def __init__(self, fail_threshold: int, cooldown_seconds: int):
        self.fail_threshold = fail_threshold
        self.cooldown_seconds = cooldown_seconds
        self._fail_count = 0
        self._open_until = 0.0

    def check(self) -> None:
        if time.monotonic() < self._open_until:
            raise RuntimeError("LLM 服务熔断中，请稍后重试")

    def record_success(self) -> None:
        self._fail_count = 0

    def record_failure(self) -> None:
        self._fail_count += 1
        if self._fail_count >= self.fail_threshold:
            self._open_until = time.monotonic() + self.cooldown_seconds
            self._fail_count = 0
            logger.error("LLM 连续失败 %d 次，熔断 %ds", self.fail_threshold, self.cooldown_seconds)


breaker = CircuitBreaker(settings.breaker_fail_threshold, settings.breaker_cooldown_seconds)


def _providers() -> list[dict]:
    """模型提供方列表：主模型 + 可选备用模型（任意 OpenAI 兼容服务）。"""
    providers = [
        {"api_key": settings.llm_api_key, "base_url": settings.llm_base_url, "model": settings.llm_model}
    ]
    if settings.llm_fallback_api_key and settings.llm_fallback_model:
        providers.append(
            {
                "api_key": settings.llm_fallback_api_key,
                "base_url": settings.llm_fallback_base_url or settings.llm_base_url,
                "model": settings.llm_fallback_model,
            }
        )
    return providers


def _client_for(provider: dict) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=provider["api_key"],
        base_url=provider["base_url"],
        timeout=settings.llm_timeout_seconds,
        max_retries=0,  # 重试逻辑自己控制（要配合熔断计数）
    )


async def chat_completion(messages: list[dict], *, tools: list | None = None,
                          temperature: float | None = None, max_tokens: int | None = None):
    """非流式调用统一入口：熔断检查 + 超时 + 重试 + 备用模型容灾。

    调用顺序：主模型（重试 N 次）→ 备用模型（如配置）。
    主模型熔断打开时直接跳到备用模型——故障期间不再浪费超时等待。
    """
    providers = _providers()
    primary_available = True
    try:
        breaker.check()
    except RuntimeError:
        primary_available = False
        if len(providers) == 1:
            raise  # 没有备胎，只能对外报熔断

    last_exc: Exception | None = None
    for idx, provider in enumerate(providers):
        if idx == 0 and not primary_available:
            continue
        retries = settings.llm_max_retries if idx == 0 else 0  # 备胎只试一次
        for attempt in range(retries + 1):
            try:
                kwargs = dict(
                    model=provider["model"],
                    messages=messages,
                    temperature=settings.llm_temperature if temperature is None else temperature,
                    max_tokens=max_tokens or settings.llm_max_tokens,
                )
                if tools:
                    kwargs["tools"] = tools
                resp = await _client_for(provider).chat.completions.create(**kwargs)
                if idx == 0:
                    breaker.record_success()
                else:
                    logger.warning("已切换备用模型: %s", provider["model"])
                return resp
            except Exception as exc:
                last_exc = exc
                if idx == 0:
                    breaker.record_failure()
                logger.warning("LLM 调用失败（provider=%s 第 %d 次）: %s", provider["model"], attempt + 1, exc)
                if attempt < retries:
                    await asyncio.sleep(1.0 * (attempt + 1))  # 退避
    raise last_exc  # type: ignore[misc]

SYSTEM_PROMPT = """你是一个严谨的知识库问答助手。请严格根据提供的参考资料回答用户问题，规则：
1. 只使用参考资料中的信息回答，不要编造资料中没有的内容；
2. 引用资料时用 [1]、[2] 这样的编号标注来源；
3. 如果参考资料不足以回答问题，明确说明"知识库中没有找到相关信息"，可以给出建议但要说明这不来自知识库；
4. 用中文回答，可使用 Markdown 排版，条理清晰。"""

REWRITE_PROMPT = """根据对话历史，把用户的最新问题改写成一个不依赖上下文、指代明确的独立问题。
只输出改写后的问题本身，不要任何解释。如果最新问题本身已经完整独立，原样输出。"""

SUGGEST_PROMPT = """根据这轮问答内容，猜测用户接下来最可能追问的 3 个问题。
要求：与已有回答不重复、简短（15 字以内）、可以用知识库继续回答。
只输出 JSON 数组，如 ["问题一","问题二","问题三"]。"""

EXPAND_PROMPT = """把用户问题改写成 2 个语义相同但表述不同的检索查询（换用同义词、换角度），
用于提高检索召回覆盖。只输出 JSON 数组，如 ["查询一","查询二"]。"""


def _source_label(c: RetrievedChunk) -> str:
    return f"{c.filename} 第 {c.page} 页" if c.page else f"{c.filename} 第 {c.seq + 1} 段"


def build_context(chunks: list[RetrievedChunk]) -> str:
    """把检索片段拼成带编号的参考资料块。"""
    if not chunks:
        return "（未检索到相关资料）"
    parts = [f"[{i}]（来源：{_source_label(c)}）\n{c.content}" for i, c in enumerate(chunks, start=1)]
    return "\n\n".join(parts)


async def stream_answer(
    question: str,
    chunks: list[RetrievedChunk],
    history: list[dict] | None = None,
    summary: str | None = None,
) -> AsyncIterator[dict]:
    """流式生成回答（主模型失败时自动切换备用模型）。

    产出事件字典：
      {"type": "delta", "text": str}                       —— 文本增量
      {"type": "usage", "prompt_tokens": int, "completion_tokens": int}  —— 结束时（如可用）
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if summary:
        # 长对话滚动摘要：旧轮次的压缩记忆，配合最近几轮原文一起进 Prompt
        messages.append({"role": "system", "content": f"此前对话的摘要（供理解上下文）：\n{summary}"})
    if history:
        messages.extend(history)
    messages.append(
        {"role": "user", "content": f"参考资料：\n{build_context(chunks)}\n\n用户问题：{question}"}
    )

    stream = None
    last_exc: Exception | None = None
    for idx, provider in enumerate(_providers()):
        if idx == 0:
            try:
                breaker.check()
            except RuntimeError as exc:
                last_exc = exc
                continue  # 主模型熔断中，直接试备胎
        try:
            stream = await _client_for(provider).chat.completions.create(
                model=provider["model"],
                messages=messages,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                stream=True,
                stream_options={"include_usage": True},  # 让最后一个数据块携带 token 用量
            )
            if idx == 0:
                breaker.record_success()
            else:
                logger.warning("流式生成已切换备用模型: %s", provider["model"])
            break
        except Exception as exc:
            last_exc = exc
            if idx == 0:
                breaker.record_failure()
            logger.warning("流式生成失败（provider=%s）: %s", provider["model"], exc)
    if stream is None:
        raise last_exc or RuntimeError("LLM 不可用")

    async for event in stream:
        if event.choices and event.choices[0].delta.content:
            yield {"type": "delta", "text": event.choices[0].delta.content}
        if getattr(event, "usage", None):
            yield {
                "type": "usage",
                "prompt_tokens": event.usage.prompt_tokens or 0,
                "completion_tokens": event.usage.completion_tokens or 0,
            }


async def rewrite_query(question: str, history: list[dict]) -> str:
    """多轮改写：失败时兜底返回原问题（改写是增强，不能变成故障点）。"""
    if not history:
        return question
    try:
        dialogue = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in history[-6:])
        resp = await chat_completion(
            [
                {"role": "system", "content": REWRITE_PROMPT},
                {"role": "user", "content": f"对话历史：\n{dialogue}\n\n最新问题：{question}"},
            ],
            temperature=0.0,
            max_tokens=120,
        )
        rewritten = (resp.choices[0].message.content or "").strip()
        return rewritten or question
    except Exception:
        logger.warning("查询改写失败，使用原问题", exc_info=True)
        return question


async def expand_queries(question: str) -> list[str]:
    """多查询扩展：生成 2 个同义变体（失败返回空，检索层只用原问题）。"""
    try:
        resp = await chat_completion(
            [
                {"role": "system", "content": EXPAND_PROMPT},
                {"role": "user", "content": question},
            ],
            temperature=0.5,
            max_tokens=120,
        )
        text = (resp.choices[0].message.content or "").strip()
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            return []
        return [str(x)[:200] for x in json.loads(text[start : end + 1]) if str(x).strip()][:2]
    except Exception:
        logger.warning("多查询扩展失败", exc_info=True)
        return []


async def suggest_followups(question: str, answer: str) -> list[str]:
    """生成推荐追问：失败返回空列表（静默降级）。"""
    try:
        resp = await chat_completion(
            [
                {"role": "system", "content": SUGGEST_PROMPT},
                {"role": "user", "content": f"问题：{question}\n回答：{answer[:1500]}"},
            ],
            temperature=0.7,
            max_tokens=150,
        )
        text = (resp.choices[0].message.content or "").strip()
        # 容错：截取 JSON 数组部分再解析
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            return []
        items = json.loads(text[start : end + 1])
        return [str(x)[:50] for x in items if str(x).strip()][:3]
    except Exception:
        logger.warning("推荐追问生成失败", exc_info=True)
        return []
