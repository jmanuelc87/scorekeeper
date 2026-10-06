"""Live list of Claude models, refreshed from Anthropic's List Models API.

The judges' hardcoded ``KNOWN_MODELS`` is only the seed: once a refresh succeeds the
fetched ids become the owned set, so a newly released model needs no code change. A
failed refresh (no key, network error) keeps the last good list, and a process that
never refreshed keeps the seed, so startup and offline/test runs are unaffected.
"""

from __future__ import annotations

import threading
from typing import Any

import httpx2
import structlog

log = structlog.get_logger(__name__)

_listed: frozenset[str] | None = None


def listed_models() -> frozenset[str] | None:
    """The last successfully fetched model ids, or ``None`` if none was fetched yet."""
    return _listed


MODELS_URL = "https://api.anthropic.com/v1/models"
_PAGE_SIZE = 1000


def _fetch_ids(api_key: str, client: httpx2.Client) -> frozenset[str]:
    """Walk every page of the List Models API (``has_more`` / ``after_id``)."""
    ids: set[str] = set()
    params: dict[str, Any] = {"limit": _PAGE_SIZE}
    while True:
        response = client.get(
            MODELS_URL,
            params=params,
            headers={"anthropic-version": "2023-06-01", "X-Api-Key": api_key},
        )
        response.raise_for_status()
        page = response.json()
        ids.update(model["id"] for model in page["data"])
        if not page.get("has_more"):
            return frozenset(ids)
        params["after_id"] = page["last_id"]


def refresh_models(api_key: str | None, *, client: httpx2.Client | None = None) -> bool:
    """Fetch the model list and, on success, make it the owned set. Never raises."""
    global _listed
    if not api_key:
        log.warning("claude_models_refresh_skipped", reason="sin ANTHROPIC_API_KEY")
        return False
    try:
        with client or httpx2.Client(timeout=30.0) as http:
            ids = _fetch_ids(api_key, http)
    except Exception as exc:  # noqa: BLE001 - a refresh must never break the process
        log.warning("claude_models_refresh_failed", error=str(exc))
        return False
    if not ids:
        log.warning("claude_models_refresh_failed", error="lista vacia")
        return False
    _listed = ids
    log.info("claude_models_refreshed", count=len(ids))
    return True


def start_periodic_refresh(api_key: str | None, interval_seconds: float) -> threading.Event:
    """Refresh now and then every ``interval_seconds`` on a daemon thread.

    Returns the stop event (set it to end the loop).
    """
    stop = threading.Event()

    def _loop() -> None:
        while True:
            refresh_models(api_key)
            if stop.wait(interval_seconds):
                return

    threading.Thread(target=_loop, name="claude-models-refresh", daemon=True).start()
    return stop
