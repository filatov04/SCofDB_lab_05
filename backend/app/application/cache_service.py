"""Redis cache service — LAB 05."""

import json
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.cache_keys import catalog_key, order_card_key

# TTL values
_CATALOG_TTL = 60        # 1 minute
_ORDER_CARD_TTL = 300    # 5 minutes


class CacheService:
    """
    Сервис кэширования каталога товаров и карточки заказа.

    Стратегия: cache-aside (read-through / write-around).
    При cache miss — загружает из PostgreSQL, сохраняет в Redis с TTL.
    """

    def __init__(self, session: AsyncSession, redis: Redis) -> None:
        self.session = session
        self.redis = redis

    # ──────────────────────────────────────────
    # Catalog
    # ──────────────────────────────────────────

    async def get_catalog(self, *, use_cache: bool = True) -> dict[str, Any]:
        """Вернуть каталог (уникальные товары + минимальная цена)."""
        key = catalog_key()

        if use_cache:
            cached = await self.redis.get(key)
            if cached:
                return {"items": json.loads(cached), "_source": "cache"}

        # Cache miss — load from DB
        result = await self.session.execute(text("""
            SELECT product_name, MIN(price) AS price, SUM(quantity) AS total_qty
            FROM order_items
            GROUP BY product_name
            ORDER BY product_name
        """))
        items = [
            {
                "product_name": r.product_name,
                "price": float(r.price),
                "total_qty": int(r.total_qty),
            }
            for r in result.fetchall()
        ]

        if use_cache:
            await self.redis.setex(key, _CATALOG_TTL, json.dumps(items))

        return {"items": items, "_source": "db"}

    async def invalidate_catalog(self) -> None:
        """Удалить ключ каталога из Redis."""
        await self.redis.delete(catalog_key())

    # ──────────────────────────────────────────
    # Order card
    # ──────────────────────────────────────────

    async def get_order_card(
        self, order_id: str, *, use_cache: bool = True
    ) -> dict[str, Any]:
        """Вернуть карточку заказа (заказ + позиции)."""
        key = order_card_key(order_id)

        if use_cache:
            cached = await self.redis.get(key)
            if cached:
                data = json.loads(cached)
                data["_source"] = "cache"
                return data

        # Cache miss — load from DB
        result = await self.session.execute(
            text("""
                SELECT id, user_id, status, total_amount, created_at
                FROM orders WHERE id = :oid
            """),
            {"oid": order_id},
        )
        row = result.fetchone()
        if not row:
            return {}

        items_result = await self.session.execute(
            text("""
                SELECT product_name, price, quantity
                FROM order_items WHERE order_id = :oid
            """),
            {"oid": order_id},
        )
        items = [
            {
                "product_name": r.product_name,
                "price": float(r.price),
                "quantity": int(r.quantity),
                "subtotal": float(r.price) * int(r.quantity),
            }
            for r in items_result.fetchall()
        ]

        card = {
            "id": str(row.id),
            "user_id": str(row.user_id),
            "status": row.status,
            "total_amount": float(row.total_amount),
            "created_at": str(row.created_at),
            "items": items,
        }

        if use_cache:
            await self.redis.setex(key, _ORDER_CARD_TTL, json.dumps(card))

        card["_source"] = "db"
        return card

    async def invalidate_order_card(self, order_id: str) -> None:
        """Удалить ключ карточки заказа из Redis."""
        await self.redis.delete(order_card_key(order_id))
