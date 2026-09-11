"""Shared store handle (overridable in tests via ``set_store``)."""
from __future__ import annotations

import os
from typing import Optional

from .db import Store

_store: Optional[Store] = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(os.environ.get("BENDPLAN_DB"))
    return _store


def set_store(store: Store) -> None:
    global _store
    _store = store
