"""Physical tool segment catalog endpoints (实体模段目录).

Segments are same-profile punch/die sections the shop butts together for
long bend lines.  Retiring a segment invalidates only the *draft* cards
whose frozen layout references it; sealed cards stay valid history.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from ..models import ToolSegment
from ..runtime import get_store

router = APIRouter(prefix="/catalog/segments", tags=["segments"])


@router.get("")
def list_segments(kind: Optional[str] = None, active_only: bool = False):
    if kind is not None and kind not in ("die", "punch"):
        raise HTTPException(400, "kind must be 'die' or 'punch'")
    return get_store().list_segments(kind=kind, active_only=active_only)


@router.post("", status_code=201)
def create_segment(body: ToolSegment):
    store = get_store()
    if store.catalog(body.kind, body.profile_id) is None:
        raise HTTPException(
            400, f"unknown {body.kind} profile {body.profile_id}")
    if store.get_segment(body.id) is not None:
        raise HTTPException(409, f"segment {body.id} already exists")
    store.create_segment(body.model_dump())
    return store.get_segment(body.id)


@router.get("/{segment_id}")
def get_segment(segment_id: str):
    row = get_store().get_segment(segment_id)
    if row is None:
        raise HTTPException(404, "segment not found")
    return row


@router.post("/{segment_id}/retire")
def retire_segment(segment_id: str):
    store = get_store()
    res = store.retire_segment(segment_id)
    if res is None:
        raise HTTPException(404, "segment not found")
    if res["already_retired"]:
        raise HTTPException(409, "segment already retired")
    return {"segment_id": segment_id, "retired": True,
            "invalidated_card_ids": res["invalidated_card_ids"]}
