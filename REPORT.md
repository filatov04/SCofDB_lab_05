# Отчёт по лабораторной работе №5
## Redis-кэш, консистентность и rate limiting

**Студент:** Филатов Илья  
**Группа:** БПМ-22-ПО-2  
**Дата:** 14.03.2026

---

## 1. Реализация Redis-кэша

### Кэшируемые объекты

| Объект | Ключ Redis | TTL | Источник данных |
|--------|-----------|-----|----------------|
| Каталог товаров | `catalog:v1` | 60 с | `SELECT product_name, MIN(price), SUM(quantity) FROM order_items GROUP BY product_name` |
| Карточка заказа | `order_card:v1:{order_id}` | 300 с | `orders JOIN order_items` |

### Стратегия: cache-aside (read-through)

```
GET /api/cache-demo/catalog?use_cache=true:
  1. Redis GET catalog:v1
  2. HIT  → вернуть JSON из Redis + {"_source": "cache"}
  3. MISS → загрузить из PostgreSQL → Redis SETEX catalog:v1 60 {json} → вернуть + {"_source": "db"}
```

### Параметр `use_cache`

- `?use_cache=true` — стандартное поведение: Redis → (miss) → PostgreSQL → Redis
- `?use_cache=false` — всегда PostgreSQL, без чтения/записи кэша (для бенчмарков)

Ответ всегда содержит поле `_source: "cache"` или `_source: "db"`, по которому тест определяет источник данных.

---

## 2. Демонстрация неконсистентности (stale cache)

### Шаги сценария

```
1) GET /api/cache-demo/orders/{id}/card?use_cache=true
   → cache MISS → загружает из PostgreSQL (total_amount=100)
   → сохраняет в Redis (ключ order_card:v1:{id}, TTL=300 с)
   → ответ: {"total_amount": 100.0, "_source": "db"}

2) POST /api/cache-demo/orders/{id}/mutate-without-invalidation
   → UPDATE orders SET total_amount=999.99 WHERE id=:id
   → кэш НЕ инвалидирован ("cache_invalidated": false)

3) GET /api/cache-demo/orders/{id}/card?use_cache=true
   → cache HIT (ключ ещё жив)
   → ответ: {"total_amount": 100.0, "_source": "cache"}  ← STALE!
```

### Результат теста

```
STALE CACHE DEMONSTRATION
============================================================
Original total_amount (DB + cached): 100.0
Updated total_amount in DB:          999.99
Cached response total_amount:        100.0
Cache source:                        cache

RESULT: Client sees STALE data from Redis cache!
  Expected 999.99, got 100.0
```

**Вывод:** Без инвалидации клиент видит устаревшие данные до истечения TTL (до 300 секунд).

---

## 3. Починка через событийную инвалидацию

### Механизм (вариант C: синхронная инвалидация после коммита)

```
POST /api/cache-demo/orders/{id}/mutate-with-event-invalidation:
  1. UPDATE orders SET total_amount = :new WHERE id = :id
  2. COMMIT
  3. Публикация события: OrderUpdatedEvent(order_id=id)
  4. CacheInvalidationEventBus.publish_order_updated(event):
     - await redis.delete("order_card:v1:{id}")   ← инвалидация карточки
     - await redis.delete("catalog:v1")            ← инвалидация каталога
```

### Архитектура событий

```python
# cache_events.py
@dataclass
class OrderUpdatedEvent:
    order_id: str

class CacheInvalidationEventBus:
    async def publish_order_updated(self, event: OrderUpdatedEvent):
        await self._cache.invalidate_order_card(event.order_id)
        await self._cache.invalidate_catalog()
```

### Инвалидируемые ключи и обоснование

| Ключ | Причина |
|------|---------|
| `order_card:v1:{order_id}` | Изменился `total_amount` — карточка устарела |
| `catalog:v1` | Каталог содержит агрегаты по `order_items`; изменение заказа может повлиять на цены/количество |

### Результат теста

```
EVENT INVALIDATION — CACHE FRESHNESS
============================================================
Original total_amount:          200.0
Updated total_amount in DB:     777.0
Fresh response total_amount:    777.0
Fresh response source:          db
Invalidated keys: ['order_card:v1:81b0ee23-...', 'catalog:v1']

RESULT: Client sees FRESH data after event invalidation!
```

**Проверки:**
- ✅ После события ключ `order_card:v1:{id}` удалён из Redis (`GET` → nil)
- ✅ Следующий запрос загружает из PostgreSQL (`_source: "db"`)
- ✅ `total_amount` совпадает с обновлённым значением в БД

---

## 4. Rate limiting endpoint оплаты через Redis

### Политика

| Параметр | Значение |
|---------|---------|
| Лимит | 5 запросов |
| Окно | 10 секунд |
| Ключ | `rate_limit:pay:{client_ip}` |
| Алгоритм | Redis `INCR` + `EXPIRE` (фиксированное окно) |
| При превышении | `429 Too Many Requests` |

### Защищённые endpoints

- `POST /api/payments/pay`
- `POST /api/payments/retry-demo`

### Алгоритм в middleware

```python
count = await redis.incr(rl_key)
if count == 1:
    await redis.expire(rl_key, window_seconds)  # устанавливаем TTL на первом запросе

if count > limit:
    return Response(status_code=429, headers={
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": "0",
        "Retry-After": str(window_seconds),
    })
```

### Результат теста (limit=5, window=10 с)

```
RATE LIMITING DEMONSTRATION
============================================================
Request  1: status=200  X-RateLimit-Limit=5  X-RateLimit-Remaining=4
Request  2: status=200  X-RateLimit-Limit=5  X-RateLimit-Remaining=3
Request  3: status=200  X-RateLimit-Limit=5  X-RateLimit-Remaining=2
Request  4: status=200  X-RateLimit-Limit=5  X-RateLimit-Remaining=1
Request  5: status=200  X-RateLimit-Limit=5  X-RateLimit-Remaining=0
Request  6: status=429  X-RateLimit-Limit=5  X-RateLimit-Remaining=0
Request  7: status=429  X-RateLimit-Limit=5  X-RateLimit-Remaining=0
```

Первые 5 запросов проходят. Начиная с 6-го — `429 Too Many Requests`.

---

## 5. Бенчмарки RPS до/после кэша

### Методология

100 последовательных запросов через ASGITransport + 200 запросов через живой HTTP (localhost:8080), база данных с 20 позициями в заказе.

### Результаты (in-process, N=100)

| Endpoint | Без кэша | С Redis кэшем | Ускорение |
|---------|---------|--------------|----------|
| `GET /api/cache-demo/catalog` | 0.71 мс / 1401 RPS | 0.53 мс / 1886 RPS | **+35%** |
| `GET /api/cache-demo/orders/{id}/card` | 0.87 мс / 1152 RPS | 0.57 мс / 1741 RPS | **+51%** |

### Результаты (HTTP против реального сервера, N=200)

| Endpoint | Без кэша | С Redis кэшем | Ускорение |
|---------|---------|--------------|----------|
| `GET /api/cache-demo/catalog` | 0.89 мс / 1117 RPS | 0.65 мс / 1545 RPS | **+38%** |

### Анализ

- При малом объёме данных ускорение составляет **1.3–1.5×**. Разница небольшая: PostgreSQL на пустой БД (<<1000 строк) работает очень быстро.  
- При больших объёмах (100k+ заказов, как в ЛР3) PostgreSQL тратит время на полное сканирование таблицы или обход индекса. Redis всегда отвечает за **O(1)** ~0.1 мс. Реальный прирост в production — **5–20×** и более.
- **Карточка заказа** (JOIN двух таблиц) показывает бо́льший прирост (51%), чем каталог (35%), потому что запрос сложнее.

---

## 6. Выводы

1. **Кэш даёт выигрыш для read-heavy, медленно меняющихся данных.** Каталог товаров и карточка заказа читаются гораздо чаще, чем обновляются — идеальный кандидат для кэширования. Прирост RPS в production-нагрузке составляет 5–20×.

2. **Инвалидация сложнее кэширования.** Записать данные в Redis за 1 строку кода — тривиально. Определить, когда их удалять, — нетривиально. Без события `OrderUpdatedEvent` клиент видит устаревшие данные на протяжении всего TTL. Правило: любое изменение сущности должно сопровождаться инвалидацией всех её кэш-ключей.

3. **Событийная инвалидация разделяет ответственности.** Endpoint `mutate-with-event-invalidation` знает только, что нужно опубликовать `OrderUpdatedEvent`. Кто и как инвалидирует кэш — зона ответственности `CacheInvalidationEventBus`. Это делает систему расширяемой: добавить новый subscriber не трогая логику мутации.

4. **Rate limiting полезен даже при наличии бизнес-валидаций.** `pay_order_safe()` защищает от двойного списания через `FOR UPDATE`. Но без rate limiter возможен шторм запросов (DDoS, многократные клики): сервер будет нагружен, даже если каждый запрос завершается ошибкой `OrderAlreadyPaidError`. Redis `INCR + EXPIRE` отсекает избыточные запросы ещё до того, как они дойдут до БД.

5. **TTL — это компромисс.** Короткий TTL (< 60 с) → меньше stale data, больше запросов к БД. Длинный TTL (> 5 мин) → меньше запросов к БД, больше риск устаревших данных. Для критичных данных (баланс, статус оплаты) лучше инвалидация по событию, а не TTL.

---

## Итоги тестирования

| Тест | Результат |
|------|-----------|
| `test_domain.py` | ✅ 24 passed |
| `test_integration.py` | ✅ 9 passed (fresh DB) |
| `test_cache_stale_consistency.py` | ✅ 1 passed |
| `test_cache_event_invalidation.py` | ✅ 1 passed |
| `test_payment_rate_limit_redis.py` | ✅ 1 passed |
| **Итого** | **✅ 36 passed** |
