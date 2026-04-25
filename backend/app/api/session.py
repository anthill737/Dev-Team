"""Session routes: API key intake and validation.

The user's Anthropic API key is kept in an in-memory singleton on the backend.
It is ALSO persisted to a local file (`.devteam-run/api_key`) so the user
doesn't have to re-enter it on every backend restart. The file lives next to
the app, not in version control, and is chmod'd 0600 on POSIX systems.

The user can clear it anytime via DELETE /api/session/key, which also removes
the file. If anyone has access to the machine they can read the file, so this
is a convenience trade-off — matching the .env / OS-keyring approach most
developer tools take.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


def _key_file_path() -> Path:
    """File where the API key persists between backend restarts."""
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".devteam-run"
        / "api_key"
    )


class _KeyStore:
    """In-memory + file-persisted storage for the API key. Singleton."""

    def __init__(self) -> None:
        self._key: str | None = None
        # Try to load from disk on construction so a backend restart keeps
        # the key the user entered previously.
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        path = _key_file_path()
        if not path.exists():
            return
        try:
            key = path.read_text(encoding="utf-8").strip()
            if key.startswith("sk-ant-"):
                self._key = key
                logger.info("Loaded persisted API key from %s", path)
        except Exception:
            logger.exception("Failed to load persisted API key; ignoring")

    def _save_to_disk(self, key: str) -> None:
        path = _key_file_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(key, encoding="utf-8")
            # Tighten permissions on POSIX. Windows: ACLs via Start-Process
            # inherit the user's own access, which is already user-scoped.
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        except Exception:
            logger.exception("Failed to persist API key; key works for this session only")

    def _remove_from_disk(self) -> None:
        path = _key_file_path()
        try:
            if path.exists():
                path.unlink()
        except Exception:
            logger.exception("Failed to remove persisted API key")

    def set(self, key: str) -> None:
        self._key = key
        self._save_to_disk(key)

    def get(self) -> str | None:
        return self._key

    def clear(self) -> None:
        self._key = None
        self._remove_from_disk()

    def has(self) -> bool:
        return self._key is not None


_store = _KeyStore()


def get_api_key() -> str:
    """Dependency-style accessor for routes that need the API key.

    In claude_code mode, returns a placeholder string so routes that declare
    this dependency (as a form of auth gate) still pass, while the actual
    runner doesn't use the value. Routes that would bill API calls either
    branch on `settings.runner` or use `_build_runner(store)` which does the
    branching internally — so the placeholder is safe.
    """
    from ..config import get_settings

    if get_settings().runner == "claude_code":
        return "claude-code-subscription-no-key-needed"
    key = _store.get()
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No Anthropic API key set. POST /api/session/key first.",
        )
    return key


class SetKeyRequest(BaseModel):
    api_key: str = Field(..., min_length=10, description="Anthropic API key (sk-ant-...)")


class SetKeyResponse(BaseModel):
    ok: bool
    message: str


@router.post("/key", response_model=SetKeyResponse)
async def set_key(body: SetKeyRequest) -> SetKeyResponse:
    """Validate the API key by making a small call, then store it in memory."""
    key = body.api_key.strip()
    if not key.startswith("sk-ant-"):
        raise HTTPException(
            status_code=400,
            detail="Key does not look like an Anthropic key (expected 'sk-ant-...').",
        )

    # Validate by issuing a cheap request — any 401 will bubble up
    try:
        client = AsyncAnthropic(api_key=key)
        # The models list endpoint is a cheap validation probe
        await client.models.list(limit=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("API key validation failed: %s", exc)
        raise HTTPException(
            status_code=401,
            detail=f"API key validation failed: {exc}",
        ) from exc

    _store.set(key)
    logger.info("API key accepted and stored in memory")
    return SetKeyResponse(ok=True, message="API key validated and stored for this session.")


class SessionStatusResponse(BaseModel):
    has_key: bool
    # Which agent runner the backend is configured for. Frontend uses this to
    # decide whether to show the API-key setup screen. In claude_code mode,
    # no key is needed — the user authenticates via the `claude` CLI.
    runner: str = "api"
    # Human-readable description of how auth works for this runner. Shown on
    # the settings/about screen so users know what's backing their usage.
    runner_description: str = ""


@router.get("/status", response_model=SessionStatusResponse)
async def session_status() -> SessionStatusResponse:
    from ..config import get_settings

    runner = get_settings().runner
    if runner == "claude_code":
        desc = (
            "Subscription billing via Claude Code CLI. "
            "Usage counts against your Pro/Max plan."
        )
    else:
        desc = "Per-token billing via Anthropic API key."
    return SessionStatusResponse(
        has_key=_store.has(),
        runner=runner,
        runner_description=desc,
    )


@router.delete("/key", response_model=SetKeyResponse)
async def clear_key() -> SetKeyResponse:
    _store.clear()
    return SetKeyResponse(ok=True, message="API key cleared from memory.")
