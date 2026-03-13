"""Main FastAPI application."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.api.payment_routes import router as payment_router
from app.api.cache_demo_routes import router as cache_demo_router
from app.middleware.idempotency_middleware import IdempotencyMiddleware
from app.middleware.rate_limit_middleware import RateLimitMiddleware
from app.infrastructure.db import create_tables_if_sqlite


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables_if_sqlite()
    yield


app = FastAPI(
    title="Marketplace API",
    description="DDD-based marketplace API for lab work",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# LAB 04: идемпотентность платёжных запросов
app.add_middleware(IdempotencyMiddleware)
# LAB 05: Redis rate limiting на endpoint оплаты
app.add_middleware(RateLimitMiddleware)

app.include_router(router, prefix="/api")
app.include_router(payment_router)
app.include_router(cache_demo_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
