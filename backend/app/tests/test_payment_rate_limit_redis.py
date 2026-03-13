"""
LAB 05: Redis rate limiting для endpoint оплаты.

Политика: 5 запросов за 10 секунд на IP-адрес.
При превышении → 429 Too Many Requests + заголовки X-RateLimit-*.
"""

import os
import uuid
import pytest

from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

_DB_URL = os.getenv("DATABASE_URL", "")
_REDIS_URL = os.getenv("REDIS_URL", "")

RATE_LIMIT = 5   # should match RateLimitMiddleware default


def _pg_available() -> bool:
    try:
        import asyncpg  # noqa
        return _DB_URL.startswith("postgresql")
    except ImportError:
        return False


async def _redis_available() -> bool:
    if not _REDIS_URL:
        return False
    try:
        from redis.asyncio import Redis
        r = Redis.from_url(_REDIS_URL, decode_responses=True)
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False


def _make_engine():
    return create_async_engine(_DB_URL, echo=False)


@pytest.fixture
async def pg_order_for_rate_limit():
    """Создать заказ для rate limit теста."""
    if not _pg_available():
        pytest.skip("PostgreSQL not available")
    if not await _redis_available():
        pytest.skip("Redis not available")

    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()

    async with factory() as s:
        await s.execute(
            text("INSERT INTO users (id, email, name, created_at) VALUES (:id, :email, :name, NOW())"),
            {"id": str(user_id), "email": f"rl_{user_id}@example.com", "name": "RLUser"},
        )
        await s.execute(
            text("INSERT INTO orders (id, user_id, status, total_amount, created_at) VALUES (:id, :uid, 'created', 50, NOW())"),
            {"id": str(order_id), "uid": str(user_id)},
        )
        await s.execute(
            text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'created', NOW())"),
            {"oid": str(order_id)},
        )
        await s.commit()

    yield order_id

    # Clean up rate limit key
    from app.infrastructure.redis_client import get_redis
    from app.infrastructure.cache_keys import payment_rate_limit_key
    r = get_redis()
    await r.delete(payment_rate_limit_key("testclient"))

    async with factory() as s:
        await s.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(user_id)})
        await s.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_payment_endpoint_rate_limit(pg_order_for_rate_limit):
    """
    Первые RATE_LIMIT запросов проходят.
    (RATE_LIMIT + 1)-й запрос получает 429.
    Заголовки X-RateLimit-Limit / X-RateLimit-Remaining присутствуют.
    """
    from app.main import app
    from app.infrastructure.redis_client import get_redis
    from app.infrastructure.cache_keys import payment_rate_limit_key

    order_id = pg_order_for_rate_limit

    # Reset counter before test (testclient IP)
    redis = get_redis()
    await redis.delete(payment_rate_limit_key("testclient"))

    payload = {"order_id": str(order_id), "mode": "unsafe"}
    responses = []

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testclient",  # deterministic IP = "testclient"
    ) as client:
        for i in range(RATE_LIMIT + 2):
            resp = await client.post("/api/payments/retry-demo", json=payload)
            responses.append(resp)

    print("\n" + "=" * 60)
    print("RATE LIMITING DEMONSTRATION")
    print("=" * 60)
    print(f"Limit: {RATE_LIMIT} requests per window")
    print()
    for i, r in enumerate(responses):
        limit_hdr = r.headers.get("X-RateLimit-Limit", "-")
        remaining_hdr = r.headers.get("X-RateLimit-Remaining", "-")
        print(f"Request {i+1:2d}: status={r.status_code}  "
              f"X-RateLimit-Limit={limit_hdr}  X-RateLimit-Remaining={remaining_hdr}")
    print("=" * 60)

    # First RATE_LIMIT requests should succeed (200 or business error, not 429)
    for i in range(RATE_LIMIT):
        assert responses[i].status_code != 429, \
            f"Request {i+1} should NOT be rate limited (got 429)"
        assert "X-RateLimit-Limit" in responses[i].headers, \
            f"Request {i+1} must carry X-RateLimit-Limit header"

    # The (RATE_LIMIT+1)-th request should be rejected
    assert responses[RATE_LIMIT].status_code == 429, \
        f"Request {RATE_LIMIT+1} must be rate limited (got {responses[RATE_LIMIT].status_code})"

    # 429 response must carry rate limit headers
    rl_resp = responses[RATE_LIMIT]
    assert rl_resp.headers.get("X-RateLimit-Limit") == str(RATE_LIMIT)
    assert rl_resp.headers.get("X-RateLimit-Remaining") == "0"
    assert "Retry-After" in rl_resp.headers
