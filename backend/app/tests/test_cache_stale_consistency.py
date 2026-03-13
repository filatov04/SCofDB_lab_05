"""
LAB 05: Демонстрация неконсистентности кэша (stale data).

Сценарий:
1) GET /api/cache-demo/orders/{id}/card?use_cache=true
   → cache miss → загружает из БД → сохраняет в Redis (total_amount=100)
2) POST /api/cache-demo/orders/{id}/mutate-without-invalidation
   → обновляет total_amount в БД до 999  (кэш НЕ инвалидирован)
3) GET /api/cache-demo/orders/{id}/card?use_cache=true
   → cache HIT → возвращает СТАРЫЕ данные (total_amount=100)

Результат: клиент видит устаревшие данные (stale cache).
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
    """Создать заказ с позициями, вернуть order_id, убрать после теста."""
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
            {"id": str(user_id), "email": f"stale_{user_id}@example.com", "name": "Stale"},
        )
        await s.execute(
            text("INSERT INTO orders (id, user_id, status, total_amount, created_at) VALUES (:id, :uid, 'created', 100, NOW())"),
            {"id": str(order_id), "uid": str(user_id)},
        )
        await s.execute(
            text("INSERT INTO order_items (id, order_id, product_name, price, quantity) VALUES (:id, :oid, 'Widget', 50, 2)"),
            {"id": str(item_id), "oid": str(order_id)},
        )
        await s.execute(
            text("INSERT INTO order_status_history (id, order_id, status, changed_at) VALUES (gen_random_uuid(), :oid, 'created', NOW())"),
            {"oid": str(order_id)},
        )
        await s.commit()

    yield order_id

    # Invalidate cache + cleanup DB
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
async def test_stale_order_card_when_db_updated_without_invalidation(pg_order_with_items):
    """
    Stale data демонстрация:
    После mutate-without-invalidation кэш содержит старые данные.
    """
    from app.main import app

    order_id = pg_order_with_items

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Step 1: прогрев кэша (cache miss → DB → save to Redis)
        resp_warmup = await client.get(f"/api/cache-demo/orders/{order_id}/card?use_cache=true")
        assert resp_warmup.status_code == 200
        warmup_data = resp_warmup.json()
        assert warmup_data["_source"] == "db", "First request must populate cache from DB"
        original_total = warmup_data["total_amount"]

        # Step 2: изменяем заказ в БД, кэш НЕ инвалидируем
        new_total = 999.99
        resp_mutate = await client.post(
            f"/api/cache-demo/orders/{order_id}/mutate-without-invalidation",
            json={"new_total_amount": new_total},
        )
        assert resp_mutate.status_code == 200
        assert resp_mutate.json()["cache_invalidated"] is False

        # Step 3: повторный запрос — должен вернуть STALE данные из кэша
        resp_stale = await client.get(f"/api/cache-demo/orders/{order_id}/card?use_cache=true")
        assert resp_stale.status_code == 200
        stale_data = resp_stale.json()

    print("\n" + "=" * 60)
    print("STALE CACHE DEMONSTRATION")
    print("=" * 60)
    print(f"Original total_amount (DB + cached): {original_total}")
    print(f"Updated total_amount in DB:          {new_total}")
    print(f"Cached response total_amount:        {stale_data['total_amount']}")
    print(f"Cache source:                        {stale_data['_source']}")
    print()
    print("RESULT: Client sees STALE data from Redis cache!")
    print(f"  Expected {new_total}, got {stale_data['total_amount']}")
    print("=" * 60)

    # The stale cache returns the old total
    assert stale_data["_source"] == "cache", "Should have returned cached data"
    assert stale_data["total_amount"] == original_total, \
        f"Expected stale total {original_total}, got {stale_data['total_amount']}"
    assert stale_data["total_amount"] != new_total, \
        "Stale cache must NOT reflect the DB update!"
