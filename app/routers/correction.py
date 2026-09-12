"""First-piece springback correction endpoints.

Measurements are filed against sealed cards; comparable historical samples
drive a robust per-bend overbend estimate.  A derived *draft* card is only
created when every acceptance gate passes — the API never seals it.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..correction import (CorrectionConflict, CorrectionError,
                          submit_first_piece)
from ..models import FirstPieceRequest
from ..runtime import get_store
from ..service import CatalogError

router = APIRouter(tags=["first-piece"])


@router.post("/cards/{card_id}/first-piece")
def first_piece(card_id: int, body: FirstPieceRequest):
    store = get_store()
    try:
        return submit_first_piece(store, card_id, body)
    except CatalogError as e:
        raise HTTPException(404, str(e))
    except CorrectionError as e:
        raise HTTPException(400, str(e))
    except CorrectionConflict as e:
        raise HTTPException(409, str(e))


@router.get("/cards/{card_id}/first-piece")
def list_card_runs(card_id: int):
    store = get_store()
    if store.get_card(card_id) is None:
        raise HTTPException(404, "card not found")
    return store.list_runs(card_id)


@router.get("/first-piece/{run_id}")
def get_run(run_id: int):
    run = get_store().get_run(run_id)
    if run is None:
        raise HTTPException(404, "first-piece run not found")
    return run


@router.get("/first-piece")
def list_runs():
    return get_store().list_runs()
