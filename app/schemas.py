"""Pydantic 请求/响应模型。"""
from datetime import datetime

from pydantic import BaseModel, Field


# ---- 认证 ----
class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class MeResponse(BaseModel):
    id: str
    username: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- 知识库 ----
class KBCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)


class KBOut(BaseModel):
    id: str
    name: str
    description: str
    created_at: datetime
    doc_count: int = 0

    model_config = {"from_attributes": True}


# ---- 文档 ----
class DocumentOut(BaseModel):
    id: str
    kb_id: str
    filename: str
    status: str
    chunk_count: int
    error: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- 对话 ----
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None  # 为空则新建会话
    kb_id: str | None = None            # 为空则检索该用户全部文档
    mode: str = Field(default="rag", pattern="^(rag|agent)$")  # rag=固定管道 / agent=工具循环


class ConversationRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ConversationOut(BaseModel):
    id: str
    title: str
    kb_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    role: str
    content: str
    sources: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- 统计 ----
class StatsOverview(BaseModel):
    kb_count: int
    doc_count: int
    chunk_count: int
    conversation_count: int
    question_count: int
    prompt_tokens: int
    completion_tokens: int
    avg_first_token_ms: int
    avg_total_ms: int


class DailyUsage(BaseModel):
    date: str
    questions: int
    prompt_tokens: int
    completion_tokens: int
