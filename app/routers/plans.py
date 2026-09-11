"""Planning endpoints: solve, technician validation, cards/versions/seal/branch."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..models import (BranchRequest, CardVersionOut, PartCreate,
                      TechnicianSequence)
from ..runtime import get_store
from ..service import CatalogError, load_context, solve, solve_for_card

router = APIRouter(tags=["planning"])


def _card_out(row: dict, include_svg: bool) -> dict:
    return {
        "card_id": row["card_id"],
        "part_id": row["part_id"],
        "version": row["version"],
        "status": row["status"],
        "parent_card_id": row["parent_card_id"],
        "created_at": row["created_at"],
        "sealed_at": row["sealed_at"],
        "result": row["result"],
        "input_snapshot": row["input_snapshot"],
        **({"svg": row["svg"]} if include_svg else {}),
    }


# ----------------------------------------------------------- solve / check

@router.post("/parts/{part_id}/solve")
def solve_part(part_id: int):
    store = get_store()
    try:
        result, svgs, snapshot = solve_for_card(store, part_id)
    except CatalogError as e:
        raise HTTPException(400, str(e))
    card_id = store.create_card(part_id, result, svgs,
                                input_snapshot=snapshot)
    return _card_out(store.get_card(card_id), include_svg=True)


@router.post("/parts/{part_id}/check-sequence")
def check_sequence(part_id: int, body: TechnicianSequence):
    store = get_store()
    try:
        result, svgs, snapshot = solve_for_card(
            store, part_id, forced_order=body.bend_ids)
    except CatalogError as e:
        raise HTTPException(400, str(e))
    # technician validation is not persisted as a process card
    return {"requested_order": body.bend_ids, "note": body.note,
            **result, "svg": svgs}


# ----------------------------------------------------------- cards

@router.get("/parts/{part_id}/cards")
def list_part_cards(part_id: int):
    store = get_store()
    if store.get_part(part_id) is None:
        raise HTTPException(404, "part not found")
    return store.list_cards(part_id)


@router.get("/cards")
def list_all_cards():
    return get_store().list_cards()


@router.get("/cards/{card_id}")
def get_card(card_id: int, with_svg: bool = True):
    row = get_store().get_card(card_id)
    if row is None:
        raise HTTPException(404, "card not found")
    return _card_out(row, include_svg=with_svg)


@router.get("/cards/{card_id}/svg")
def card_svg(card_id: int, step: int = 0):
    """Step-by-step side view; step 0 returns an HTML page with all steps."""
    row = get_store().get_card(card_id)
    if row is None:
        raise HTTPException(404, "card not found")
    svgs = row["svg"]
    if not svgs:
        raise HTTPException(409, "card has no feasible sequence (no views)")
    if step == 0:
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><body style='background:"
            "#fafafa'>" + "".join(svgs) + "</body>")
    if step < 1 or step > len(svgs):
        raise HTTPException(404, f"step must be in 1..{len(svgs)}")
    return HTMLResponse(svgs[step - 1])


@router.post("/cards/{card_id}/seal")
def seal_card(card_id: int):
    store = get_store()
    row = store.get_card(card_id)
    if row is None:
        raise HTTPException(404, "card not found")
    if row["status"] == "sealed":
        raise HTTPException(409, "card already sealed (immutable)")
    if not row["result"]["feasible"]:
        raise HTTPException(409, "only feasible cards can be sealed")
    if not store.seal_card(card_id):
        raise HTTPException(409, "cannot seal card")
    return _card_out(store.get_card(card_id), include_svg=False)


@router.get("/cards/{card_id}/lineage")
def lineage(card_id: int):
    store = get_store()
    if store.get_card(card_id) is None:
        raise HTTPException(404, "card not found")
    return store.card_lineage(card_id)


@router.post("/cards/{card_id}/branch", status_code=201)
def branch_card(card_id: int, body: BranchRequest):
    """Recompute from a sealed card when dimensions or equipment change.

    The original card is never modified; the new card records the parent and
    the next version number.
    """
    store = get_store()
    parent = store.get_card(card_id)
    if parent is None:
        raise HTTPException(404, "parent card not found")
    if parent["status"] != "sealed":
        raise HTTPException(409, "can only branch from a sealed card")

    if body.part_changes is not None:
        data = body.part_changes
    else:
        data = PartCreate(**parent["input_snapshot"])
        if body.machine_id is not None:
            data = data.model_copy(update={"machine_id": body.machine_id})
        if body.candidate_dies is not None:
            data = data.model_copy(
                update={"candidate_dies": body.candidate_dies})
        if body.candidate_punches is not None:
            data = data.model_copy(
                update={"candidate_punches": body.candidate_punches})

    # dimension / equipment change must actually differ from the sealed input
    snap = parent["input_snapshot"]
    if data.model_dump() == snap:
        raise HTTPException(
            409, "no dimension or equipment change vs the sealed card; "
                 "branching requires a change")
    try:
        load_context(store, data)
    except CatalogError as e:
        raise HTTPException(400, str(e))
    new_part_id = store.create_part(data)
    result, svgs, new_snapshot = solve_for_card(store, new_part_id)
    new_card = store.create_card(new_part_id, result, svgs,
                                 parent_card_id=card_id,
                                 input_snapshot=new_snapshot)
    out = _card_out(store.get_card(new_card), include_svg=True)
    out["branch_note"] = body.note
    return out
