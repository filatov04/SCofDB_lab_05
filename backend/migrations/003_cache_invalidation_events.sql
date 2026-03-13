-- ============================================
-- LAB 05: Outbox-таблица событий инвалидации кэша
-- ============================================
-- Реализованный вариант: C — синхронная инвалидация после коммита.
-- Таблица добавлена для аудита: фиксирует каждое событие инвалидации.
-- ============================================

CREATE TABLE IF NOT EXISTS cache_invalidation_events (
    id           UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type   VARCHAR(64) NOT NULL,         -- ORDER_UPDATED
    entity_type  VARCHAR(64) NOT NULL,         -- ORDER
    entity_id    UUID        NOT NULL,
    payload      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    processed    BOOLEAN     NOT NULL DEFAULT TRUE,   -- синхронная обработка → сразу TRUE
    created_at   TIMESTAMP   NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- Индекс для аналитики (сколько событий за период)
CREATE INDEX idx_cache_events_entity
    ON cache_invalidation_events (entity_type, entity_id, created_at DESC);
