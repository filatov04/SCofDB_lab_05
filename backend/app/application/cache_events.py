"""Event-driven cache invalidation — LAB 05.

Выбранный вариант: вариант C — синхронная публикация события после коммита.
После изменения заказа в БД сразу же инвалидируются связанные ключи Redis.
Это простейший и надёжный вариант для учебного сценария.
"""

from dataclasses import dataclass

from app.application.cache_service import CacheService


@dataclass
class OrderUpdatedEvent:
    """Событие изменения заказа."""
    order_id: str


class CacheInvalidationEventBus:
    """
    Минимальный event bus.

    При OrderUpdatedEvent инвалидирует:
    - order_card:v1:{order_id}
    - catalog:v1  (цены/количество могут измениться)
    """

    def __init__(self, cache_service: CacheService) -> None:
        self._cache = cache_service

    async def publish_order_updated(self, event: OrderUpdatedEvent) -> None:
        """Публикует событие → синхронно инвалидирует кэш."""
        await self._cache.invalidate_order_card(event.order_id)
        await self._cache.invalidate_catalog()
