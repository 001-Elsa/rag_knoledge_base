"""接口限流：基于 slowapi。

计数存储：默认进程内存（单实例）；配置 RATE_LIMIT_STORAGE_URL 为 Redis 后
支持多实例部署下的全局限流。
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.rate_limit_storage_url or None,
)
