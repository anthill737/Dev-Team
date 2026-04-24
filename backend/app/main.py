"""FastAPI application entry point."""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import projects_router, session_router, ws_router
from .api.session import _store as _key_store
from .config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _bootstrap_api_key_from_env() -> None:
    """If ANTHROPIC_API_KEY is set in the environment, pre-load it into the session store
    so the user doesn't have to paste it every time they start the app.

    Validation: we do a cheap sanity check on the prefix. We deliberately don't make a
    live API call here — that would slow startup and fail the whole app on transient
    network issues. If the env var is malformed, the user sees an auth error on their
    first action and the frontend falls back to the key-entry form (they can clear
    and re-enter).
    """
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not env_key:
        return
    if not env_key.startswith("sk-ant-"):
        logger.warning(
            "ANTHROPIC_API_KEY env var is set but doesn't look like an Anthropic key "
            "(expected 'sk-ant-...'). Ignoring; user will be prompted in the UI."
        )
        return
    _key_store.set(env_key)
    logger.info(
        "API key loaded from ANTHROPIC_API_KEY env var (%d chars). "
        "User won't be prompted.",
        len(env_key),
    )


def create_app() -> FastAPI:
    settings = get_settings()

    # Load persisted API key BEFORE registering routes so health checks etc work cleanly.
    _bootstrap_api_key_from_env()

    app = FastAPI(
        title="Dev Team",
        description="Autonomous software development team powered by Claude",
        version="0.1.0",
    )

    # During local dev the frontend runs on :3000 and talks to the backend on :8000.
    # In production both are served from the same origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(session_router, prefix="/api/session", tags=["session"])
    app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
    app.include_router(ws_router, prefix="/ws", tags=["websocket"])

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    logger.info("Dev Team backend initialized (log_level=%s)", settings.log_level)
    return app


app = create_app()
