"""Redis-based rate limiting middleware — LAB 05."""

import json
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# Endpoints that are subject to rate limiting
_RATE_LIMITED_PATHS = {
    "/api/payments/pay",
    "/api/payments/retry-demo",
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Redis INCR/EXPIRE sliding-counter rate limiter.

    Policy: limit_per_window POST-запросов за window_seconds секунд,
    сгруппированных по IP-адресу клиента.

    При превышении:
    - HTTP 429 Too Many Requests
    - заголовки X-RateLimit-Limit / X-RateLimit-Remaining / Retry-After
    """

    def __init__(
        self,
        app,
        limit_per_window: int = 5,
        window_seconds: int = 10,
    ):
        super().__init__(app)
        self.limit = limit_per_window
        self.window = window_seconds

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        from app.infrastructure.db import _is_sqlite

        # Only apply to POST payment endpoints; bypass in SQLite test mode
        if (
            _is_sqlite
            or request.method != "POST"
            or request.url.path not in _RATE_LIMITED_PATHS
        ):
            return await call_next(request)

        from app.infrastructure.redis_client import get_redis
        from app.infrastructure.cache_keys import payment_rate_limit_key

        client_ip = request.client.host if request.client else "unknown"
        rl_key = payment_rate_limit_key(client_ip)

        redis = get_redis()
        try:
            count = await redis.incr(rl_key)
            if count == 1:
                # First request in this window — set expiry
                await redis.expire(rl_key, self.window)

            remaining = max(0, self.limit - count)
            over_limit = count > self.limit

        except Exception:
            # Fail open: Redis unavailable → let request through
            return await call_next(request)

        if over_limit:
            body = json.dumps({
                "detail": f"Rate limit exceeded. Max {self.limit} requests per {self.window}s.",
            })
            return Response(
                content=body,
                status_code=429,
                media_type="application/json",
                headers={
                    "X-RateLimit-Limit": str(self.limit),
                    "X-RateLimit-Remaining": "0",
                    "Retry-After": str(self.window),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
