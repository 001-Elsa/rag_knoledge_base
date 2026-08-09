"""WebSocket notification tasks must not accumulate cancelled receivers."""

import inspect

from app.routers import ws


def test_websocket_uses_one_long_lived_sender_and_receiver():
    source = inspect.getsource(ws.notifications)
    assert "async def forward_notifications" in source
    assert "async def receive_client_messages" in source
    assert "await asyncio.gather" in source
    assert "while True:\n            redis_task = asyncio.create_task" not in source
