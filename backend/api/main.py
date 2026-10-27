"""Autolyze FastAPI backend."""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.routers import data_analysis
from auth.auth import (
    REFRESH_TOKEN_EXPIRE_DAYS,
    authenticate_user,
    create_access_token,
    create_refresh_token,
    verify_token,
)
from auth.dependencies import get_current_user
from jose import JWTError
from utils.logging_utils import get_logger

logger = get_logger(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Autolyze backend started.")
    yield
    logger.info("Autolyze backend shutting down.")


app = FastAPI(
    title="Autolyze AI Platform",
    description="Backend API for dataset analytics.",
    version="1.0.0",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


def _load_allowed_origins() -> list[str]:
    raw = os.getenv("AUTOLYZE_CORS_ORIGINS", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if any(origin == "*" for origin in origins):
        raise RuntimeError("Wildcard CORS origins are forbidden.")
    return origins


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _client_identifier(request: Request) -> str:
    trust_proxy = os.getenv("AUTOLYZE_TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"}
    if trust_proxy:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            first = forwarded_for.split(",")[0].strip()
            if first:
                return first

    if request.client and request.client.host:
        return request.client.host
    return "unknown"


_RATE_LIMIT_RULES = [
    ("/login", 10, 60),
    ("/auth/refresh", 20, 60),
    ("/auth/logout", 20, 60),
    ("/api/v1/data/analyze", 10, 60),
    ("/api/v1/data/chat", 30, 60),
    ("/api/v1/", 120, 60),
]
_RATE_LIMIT_RULES.sort(key=lambda item: len(item[0]), reverse=True)
_RATE_LIMIT_STATE: dict[str, deque[float]] = defaultdict(deque)
_RATE_LIMIT_LOCK = asyncio.Lock()


def _match_rate_limit(path: str):
    for prefix, limit, window in _RATE_LIMIT_RULES:
        if path.startswith(prefix):
            return limit, window
    return None


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)

    if request.url.path.startswith("/api/v1/"):
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            request.state.user = verify_token(token)
        except JWTError:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token."},
                headers={"WWW-Authenticate": "Bearer"},
            )

    rule = _match_rate_limit(request.url.path)
    if rule:
        limit, window = rule
        key = f"{_client_identifier(request)}|{request.url.path}"
        now = time.monotonic()
        async with _RATE_LIMIT_LOCK:
            # Evict stale buckets so the state dict cannot grow unbounded
            if len(_RATE_LIMIT_STATE) > 10_000:
                stale = [k for k, v in _RATE_LIMIT_STATE.items() if not v or now - v[-1] > 3600]
                for k in stale:
                    del _RATE_LIMIT_STATE[k]
            bucket = _RATE_LIMIT_STATE[key]
            while bucket and now - bucket[0] > window:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(window - (now - bucket[0]))) if bucket else window
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Please try again later."},
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)

    return await call_next(request)


_ALLOWED_ORIGINS = _load_allowed_origins()
if _ALLOWED_ORIGINS:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


REFRESH_COOKIE_NAME = "autolyze_refresh_token"
REFRESH_COOKIE_PATH = "/auth"
REFRESH_COOKIE_SECURE = _parse_bool_env("AUTH_REFRESH_COOKIE_SECURE", False)
REFRESH_COOKIE_SAMESITE = os.getenv("AUTH_REFRESH_COOKIE_SAMESITE", "lax").strip().lower()

if REFRESH_COOKIE_SAMESITE not in {"lax", "strict", "none"}:
    raise RuntimeError("AUTH_REFRESH_COOKIE_SAMESITE must be one of: lax, strict, none.")


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        expires=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=REFRESH_COOKIE_SECURE,
        samesite=REFRESH_COOKIE_SAMESITE,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
    )


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


@app.post("/login", tags=["Auth"])
async def login(body: LoginRequest, response: Response):
    user = authenticate_user(body.email, body.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user)
    refresh_token = create_refresh_token(user)
    _set_refresh_cookie(response, refresh_token)
    return {
        "access_token": token,
        "token_type": "bearer",
        "email": user["sub"],
    }


@app.post("/auth/refresh", tags=["Auth"])
async def refresh_access_token(request: Request, response: Response, body: RefreshRequest | None = None):
    refresh_token = ""
    if body and body.refresh_token:
        refresh_token = body.refresh_token.strip()
    if not refresh_token:
        refresh_token = (request.cookies.get(REFRESH_COOKIE_NAME) or "").strip() if request else ""
    if not refresh_token:
        error = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or expired refresh token."},
            headers={"WWW-Authenticate": "Bearer"},
        )
        _clear_refresh_cookie(error)
        return error

    try:
        payload = verify_token(refresh_token, token_type="refresh")
    except JWTError:
        error = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or expired refresh token."},
            headers={"WWW-Authenticate": "Bearer"},
        )
        _clear_refresh_cookie(error)
        return error

    user = {"sub": payload["sub"]}
    new_refresh_token = create_refresh_token(user)
    _set_refresh_cookie(response, new_refresh_token)
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "email": user["sub"],
    }


@app.post("/auth/logout", tags=["Auth"])
async def logout(response: Response):
    _clear_refresh_cookie(response)
    return {"detail": "Logged out."}


@app.get("/api/v1/auth/verify", tags=["Auth"])
async def verify_auth(user: dict = Depends(get_current_user)):
    return {"valid": True, "email": user["sub"]}


app.include_router(
    data_analysis.router,
    prefix="/api/v1",
    tags=["Data Analysis"],
    dependencies=[Depends(get_current_user)],
)


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(
        os.path.join(FRONTEND_DIR, "index.html"),
        headers={"Cache-Control": "no-cache"},
    )


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
