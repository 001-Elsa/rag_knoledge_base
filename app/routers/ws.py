"""WebSocket 实时通知：文档处理状态推送。

流程：浏览器连 /api/ws?token=<access_token> → 服务端校验后订阅该用户的
Redis 频道（notify:{user_id}）→ Celery worker 发布状态变化 → 实时转发给浏览器。

对比轮询：状态秒级可见、无空转请求；Redis 发布订阅让「多个 API 实例 + 独立
worker 进程」之间解耦（worker 不需要知道用户连在哪个实例上）。
"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.security import decode_access_token
from app.services.history import get_redis
from app.services.notify import channel_for

logger = logging.getLogger(__name__)

router = APIRouter(tags=["通知"])


@router.websocket("/api/ws")
async def notifications(websocket: WebSocket, token: str = ""):
    user_id = decode_access_token(token)
    if user_id is None:
        await websocket.close(code=4401, reason="unauthorized")
        return

    await websocket.accept()
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(channel_for(user_id))
    try:
        while True:
            # 同时等待：Redis 消息（转发给浏览器）与客户端消息（心跳/断连检测）
            redis_task = asyncio.create_task(pubsub.get_message(ignore_subscribe_messages=True, timeout=15))
            client_task = asyncio.create_task(websocket.receive_text())
            done, pending = await asyncio.wait(
                {redis_task, client_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

            if client_task in done:
                client_task.result()  # 抛 WebSocketDisconnect 即退出；收到内容视为心跳，忽略
            if redis_task in done:
                message = redis_task.result()
                if message and message.get("type") == "message":
                    await websocket.send_text(message["data"])
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.warning("WebSocket 异常断开", exc_info=True)
    finally:
        try:
            await pubsub.unsubscribe(channel_for(user_id))
            await pubsub.aclose()
        except Exception:
            pass
