# Статус лабораторной работы №5

## Что уже готово
- ✅ Основа проекта из предыдущей лабораторной
- ✅ Redis в `docker-compose.yml`
- ✅ Redis helper-файлы (`redis_client.py`, `cache_keys.py`)
- ✅ Шаблоны нагрузочных тестов (`wrk`, `locust`)
- ✅ Шаблон отчёта `REPORT.md`

## Что реализовано

### Кэш и консистентность
- ✅ Кэш каталога товаров (`catalog:v1`, TTL=60 с)
- ✅ Кэш карточки заказа (`order_card:v1:{id}`, TTL=300 с)
- ✅ Параметр `use_cache=true/false` для сравнения источника
- ✅ Поле `_source: "cache"|"db"` в ответе
- ✅ Демонстрация stale data (`mutate-without-invalidation`)
- ✅ Событийная инвалидация (`OrderUpdatedEvent` → `CacheInvalidationEventBus`)
- ✅ Корректный сценарий (`mutate-with-event-invalidation`)

### Защита endpoint оплаты
- ✅ Redis rate limiting в `RateLimitMiddleware`
- ✅ Алгоритм: `INCR + EXPIRE` (фиксированное окно)
- ✅ Лимит: 5 запросов / 10 секунд на IP
- ✅ Заголовки `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `Retry-After`
- ✅ `429 Too Many Requests` при превышении

### Нагрузочное тестирование
- ✅ Замеры RPS/latency без кэша и с кэшем
- ✅ Каталог: +35% RPS с кэшем
- ✅ Карточка заказа: +51% RPS с кэшем

### Отчёт
- ✅ Заполнены все разделы `REPORT.md`
- ✅ Доказана stale data проблема (тест)
- ✅ Доказана корректная инвалидация (тест)
- ✅ Доказан rate limiting (тест)

## Результаты тестирования

```
test_domain.py                       → 24 passed
test_integration.py                  →  9 passed (fresh DB)
test_cache_stale_consistency.py      →  1 passed
test_cache_event_invalidation.py     →  1 passed
test_payment_rate_limit_redis.py     →  1 passed
─────────────────────────────────────────
ИТОГО: 36 passed, 0 failed
```

## Запуск тестов LAB 05

```bash
cd lab_05
docker compose up -d --build
docker compose exec -T backend pytest app/tests/test_cache_stale_consistency.py -v -s
docker compose exec -T backend pytest app/tests/test_cache_event_invalidation.py -v -s
docker compose exec -T backend pytest app/tests/test_payment_rate_limit_redis.py -v -s
```
