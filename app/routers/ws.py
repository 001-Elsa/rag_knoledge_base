"""WebSocket 实时通知：文档处理状态推送。

流程：浏览器连 /api/ws?token=<access_token> → 服务端校验后订阅该用户的
Redis 频道（notify:{user_id}）→ Celery worker 发布状态变化 → 实时转发给浏览器。

对比轮询：状态秒级可见、无空转请求；Redis 发布订阅让「多个 API 实例 + 独立
worker 进程」之间解耦（worker 不需要知道用户连在哪个实例上）。
"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.history import get_redis
from app.services.notify import channel_for
from app.services.tokens import consume_websocket_ticket

logger = logging.getLogger(__name__)

router = APIRouter(tags=["通知"])


@router.websocket("/api/ws")
async def notifications(websocket: WebSocket, ticket: str = ""):
    # One-time, short-lived ticket avoids leaking a reusable access token in proxy logs.
    user_id = await consume_websocket_ticket(ticket)
    if user_id is None:
        await websocket.close(code=4401, reason="unauthorized")
        return

    await websocket.accept()
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(channel_for(user_id))

    async def forward_notifications() -> None:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if message and message.get("type") == "message":
                await websocket.send_text(message["data"])
            else:
                await asyncio.sleep(0.05)

    async def receive_client_messages() -> None:
        while True:
            await websocket.receive_text()

    forward_task = asyncio.create_task(forward_notifications())
    receive_task = asyncio.create_task(receive_client_messages())
    try:
        done, pending = await asyncio.wait(
            {forward_task, receive_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.warning("WebSocket 异常断开", exc_info=True)
    finally:
        for task in (forward_task, receive_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(forward_task, receive_task, return_exceptions=True)
        try:
            await pubsub.unsubscribe(channel_for(user_id))
            await pubsub.aclose()
        except Exception:
            pass
