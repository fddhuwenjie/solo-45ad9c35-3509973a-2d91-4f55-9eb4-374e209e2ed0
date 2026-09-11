"""Part endpoints: create a workpiece and inspect the part library."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from ..models import PartCreate, PartOut
from ..runtime import get_store
from ..service import CatalogError, load_context

router = APIRouter(prefix="/parts", tags=["parts"])


@router.post("", response_model=PartOut, status_code=201)
def create_part(body: PartCreate):
    store = get_store()
    try:
        load_context(store, body)
    except CatalogError as e:
        raise HTTPException(400, str(e))
    part_id = store.create_part(body)
    row = store.get_part(part_id)
    return PartOut(part_id=part_id, name=body.name,
                   created_at=row["created_at"])


@router.get("")
def list_parts():
    return get_store().list_parts()


@router.get("/{part_id}")
def get_part(part_id: int):
    row = get_store().get_part(part_id)
    if row is None:
        raise HTTPException(404, "part not found")
    return {"part_id": part_id, "name": row["name"],
            "created_at": row["created_at"],
            "input": json.loads(row["input_json"])}
