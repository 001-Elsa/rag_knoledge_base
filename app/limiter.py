"""接口限流：基于 slowapi。

计数存储：默认进程内存（单实例）；配置 RATE_LIMIT_STORAGE_URL 为 Redis 后
支持多实例部署下的全局限流。
"""
import hashlib

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings


def authenticated_principal_or_ip(request: Request) -> str:
    """Keep users behind one NAT independent without putting credentials in Redis."""
    authorization = request.headers.get("authorization", "")
    api_key = request.headers.get("x-api-key", "")
    credential = authorization if authorization.lower().startswith("bearer ") else api_key
    if credential:
        digest = hashlib.sha256(credential.encode()).hexdigest()[:24]
        return f"credential:{digest}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=authenticated_principal_or_ip,
    storage_uri=settings.rate_limit_storage_url or settings.redis_url,
    # Settings already loads .env as UTF-8. Avoid SlowAPI reading it again
    # with the platform default encoding (which breaks on Windows locales).
    config_filename="",
)
