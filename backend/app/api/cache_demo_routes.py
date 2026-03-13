"""Cache consistency demo endpoints — LAB 05."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db import get_db
from app.infrastructure.redis_client import get_redis
from app.application.cache_service import CacheService
from app.application.cache_events import CacheInvalidationEventBus, OrderUpdatedEvent


router = APIRouter(prefix="/api/cache-demo", tags=["cache-demo"])


class UpdateOrderRequest(BaseModel):
    new_total_amount: float


def _build_cache_service(db: AsyncSession) -> CacheService:
    return CacheService(session=db, redis=get_redis())


def _build_event_bus(db: AsyncSession) -> CacheInvalidationEventBus:
    return CacheInvalidationEventBus(_build_cache_service(db))


# ──────────────────────────────────────────
# 1. Каталог товаров
# ──────────────────────────────────────────

@router.get("/catalog")
async def get_catalog(
    use_cache: bool = True,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Каталог товаров (агрегат по order_items).

    Параметр use_cache:
    - true  → сначала читаем Redis, при miss — DB → сохраняем в Redis (TTL 60 с)
    - false → всегда DB, кэш не используется и не обновляется
    """
    svc = _build_cache_service(db)
    try:
        return await svc.get_catalog(use_cache=use_cache)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────
# 2. Карточка заказа
# ──────────────────────────────────────────

@router.get("/orders/{order_id}/card")
async def get_order_card(
    order_id: uuid.UUID,
    use_cache: bool = True,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """
    Карточка заказа (заказ + позиции).

    Ключ Redis: order_card:v1:{order_id}  (TTL 300 с)
    При use_cache=true возвращает кэш или загружает из DB и сохраняет.
    """
    svc = _build_cache_service(db)
    try:
        card = await svc.get_order_card(str(order_id), use_cache=use_cache)
        if not card:
            raise HTTPException(status_code=404, detail="Order not found")
        return card
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────
# 3. Намеренно сломанный сценарий (stale data)
# ──────────────────────────────────────────

@router.post("/orders/{order_id}/mutate-without-invalidation")
async def mutate_without_invalidation(
    order_id: uuid.UUID,
    payload: UpdateOrderRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Намеренно сломанный сценарий консистентности.

    Шаги:
    1. Изменяет total_amount в БД.
    2. НЕ инвалидирует кэш.

    Результат: последующий GET /orders/{id}/card?use_cache=true
    вернёт устаревшие данные из Redis.
    """
    result = await db.execute(
        text("UPDATE orders SET total_amount = :amt WHERE id = :oid RETURNING total_amount"),
        {"amt": payload.new_total_amount, "oid": str(order_id)},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    await db.commit()

    return {
        "order_id": str(order_id),
        "new_total_amount": payload.new_total_amount,
        "cache_invalidated": False,
        "warning": "Cache NOT invalidated — next GET may return stale data!",
    }


# ──────────────────────────────────────────
# 4. Починка через событийную инвалидацию
# ──────────────────────────────────────────

@router.post("/orders/{order_id}/mutate-with-event-invalidation")
async def mutate_with_event_invalidation(
    order_id: uuid.UUID,
    payload: UpdateOrderRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Корректная инвалидация через событие OrderUpdated.

    Шаги:
    1. Изменяет total_amount в БД.
    2. Публикует OrderUpdatedEvent.
    3. Обработчик инвалидирует:
       - order_card:v1:{order_id}
       - catalog:v1

    Результат: следующий GET вернёт свежие данные из БД.
    """
    result = await db.execute(
        text("UPDATE orders SET total_amount = :amt WHERE id = :oid RETURNING total_amount"),
        {"amt": payload.new_total_amount, "oid": str(order_id)},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    await db.commit()

    # Publish event → cache invalidation
    bus = _build_event_bus(db)
    event = OrderUpdatedEvent(order_id=str(order_id))
    await bus.publish_order_updated(event)

    return {
        "order_id": str(order_id),
        "new_total_amount": payload.new_total_amount,
        "cache_invalidated": True,
        "invalidated_keys": [
            f"order_card:v1:{order_id}",
            "catalog:v1",
        ],
    }
