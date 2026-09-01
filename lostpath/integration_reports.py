"""Short-lived cache for the expensive software-integration source reports.

The software detail endpoint filters the same environment, uninstall-registry and
Explorer context-menu reports for every selected entity.  Building those reports
repeats registry enumeration and entity matching, while the underlying state rarely
changes between two clicks.  Keep one short-lived snapshot and explicitly invalidate
it after a write operation.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


CACHE_SECONDS = 15.0
_lock = threading.Lock()
_cached_key: tuple[int, int, int, int] | None = None
_cached_entities: list[dict] | None = None
_cached_at = 0.0
_cached_reports: tuple[dict, dict, dict] | None = None


def get(
    entities: list[dict],
    environment_builder: Callable[[list[dict]], dict],
    registry_builder: Callable[[list[dict]], dict],
    context_menu_builder: Callable[[list[dict]], dict],
) -> tuple[dict, dict, dict]:
    """Return environment, registry and context-menu reports for one data revision."""
    global _cached_at, _cached_entities, _cached_key, _cached_reports
    key = (
        id(entities), id(environment_builder), id(registry_builder),
        id(context_menu_builder),
    )
    now = time.monotonic()
    with _lock:
        if (_cached_entities is entities and _cached_key == key and _cached_reports is not None
                and now - _cached_at < CACHE_SECONDS):
            return _cached_reports

    reports = (
        environment_builder(entities),
        registry_builder(entities),
        context_menu_builder(entities),
    )
    with _lock:
        _cached_key = key
        _cached_entities = entities
        _cached_at = time.monotonic()
        _cached_reports = reports
    return reports


def invalidate() -> None:
    global _cached_at, _cached_entities, _cached_key, _cached_reports
    with _lock:
        _cached_key = None
        _cached_entities = None
        _cached_at = 0.0
        _cached_reports = None


__all__ = ["get", "invalidate"]
