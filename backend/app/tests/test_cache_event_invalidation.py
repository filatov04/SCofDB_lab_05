"""
LAB 05: Починка через событийную инвалидацию.

Сценарий:
1) GET /api/cache-demo/orders/{id}/card?use_cache=true
   → cache miss → загружает из БД → сохраняет в Redis
2) POST /api/cache-demo/orders/{id}/mutate-with-event-invalidation
   → обновляет БД → публикует OrderUpdatedEvent → инвалидирует кэш
3) GET /api/cache-demo/orders/{id}/card?use_cache=true
   → cache MISS → загружает СВЕЖИЕ данные из БД

Результат: клиент всегда видит актуальные данные.
"""

import os
import uuid
import pytest

from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

_DB_URL = os.getenv("DATABASE_URL", "")
_REDIS_URL = os.getenv("REDIS_URL", "")


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
async def pg_order_with_items():
    if not _pg_available():
        pytest.skip("PostgreSQL not available")
    if not await _redis_available():
        pytest.skip("Redis not available")

    engine = _make_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    user_id = uuid.uuid4()
    order_id = uuid.uuid4()
    item_id = uuid.uuid4()

    async with factory() as s:
        await s.execute(
            text("INSERT INTO users (id, email, name, created_at) VALUES (:id, :email, :name, NOW())"),
            {"id": str(user_id), "email": f"event_{user_id}@example.com", "name": "Event"},
        )
        await s.execute(
            text("INSERT INTO orders (id, user_id, status, total_amount, created_at) VALUES (:id, :uid, 'created', 200, NOW())"),
            {"id": str(order_id), "uid": str(user_id)},
        )
        await s.execute(
            text("INSERT INTO order_items (id, order_id, product_name, price, quantity) VALUES (:id, :oid, 'Gadget', 100, 2)"),
            {"id": str(item_id), "oid": str(order_id)},
        )
        await s.execute(
            text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'created', NOW())"),
            {"oid": str(order_id)},
        )
        await s.commit()

    yield order_id

    from app.infrastructure.redis_client import get_redis
    from app.infrastructure.cache_keys import order_card_key, catalog_key
    r = get_redis()
    await r.delete(order_card_key(str(order_id)))
    await r.delete(catalog_key())

    async with factory() as s:
        await s.execute(text("DELETE FROM order_status_history WHERE order_id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM order_items WHERE order_id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM orders WHERE id = :oid"), {"oid": str(order_id)})
        await s.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": str(user_id)})
        await s.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_order_card_is_fresh_after_event_invalidation(pg_order_with_items):
    """
    После mutate-with-event-invalidation клиент получает свежие данные.
    """
    from app.main import app
    from app.infrastructure.redis_client import get_redis
    from app.infrastructure.cache_keys import order_card_key

    order_id = pg_order_with_items

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Step 1: прогрев кэша
        resp_warmup = await client.get(f"/api/cache-demo/orders/{order_id}/card?use_cache=true")
        assert resp_warmup.status_code == 200
        warmup_data = resp_warmup.json()
        assert warmup_data["_source"] == "db"
        original_total = warmup_data["total_amount"]

        # Verify cache was populated
        redis = get_redis()
        cached_before = await redis.get(order_card_key(str(order_id)))
        assert cached_before is not None, "Cache should be populated after warmup"

        # Step 2: изменяем заказ С инвалидацией кэша
        new_total = 777.0
        resp_mutate = await client.post(
            f"/api/cache-demo/orders/{order_id}/mutate-with-event-invalidation",
            json={"new_total_amount": new_total},
        )
        assert resp_mutate.status_code == 200
        mutate_data = resp_mutate.json()
        assert mutate_data["cache_invalidated"] is True

        # Verify cache key was deleted
        cached_after = await redis.get(order_card_key(str(order_id)))
        assert cached_after is None, "Cache key must be deleted after event invalidation"

        # Step 3: следующий запрос должен вернуть СВЕЖИЕ данные
        resp_fresh = await client.get(f"/api/cache-demo/orders/{order_id}/card?use_cache=true")
        assert resp_fresh.status_code == 200
        fresh_data = resp_fresh.json()

    print("\n" + "=" * 60)
    print("EVENT INVALIDATION — CACHE FRESHNESS")
    print("=" * 60)
    print(f"Original total_amount:          {original_total}")
    print(f"Updated total_amount in DB:     {new_total}")
    print(f"Fresh response total_amount:    {fresh_data['total_amount']}")
    print(f"Fresh response source:          {fresh_data['_source']}")
    print(f"Invalidated keys: {mutate_data['invalidated_keys']}")
    print()
    print("RESULT: Client sees FRESH data after event invalidation!")
    print("=" * 60)

    # Fresh data must match DB
    assert fresh_data["_source"] == "db", "After invalidation, data must come from DB"
    assert fresh_data["total_amount"] == new_total, \
        f"Expected fresh total {new_total}, got {fresh_data['total_amount']}"
    assert fresh_data["total_amount"] != original_total, \
        "Fresh data must NOT match stale cached value"
