"""First-piece springback correction.

A sealed process card is issued with a single nominal ``springback_deg``.
When the coil or the actual thickness changes the real springback differs;
this module:

1. accepts the inspector's per-bend measured included angles against a
   **sealed, feasible** card (measurements must match the original step,
   die, punch and bend direction; an idempotency key records each run once);
2. selects *comparable* historical samples by material, thickness band,
   grain relation (bend axis parallel/perpendicular to the grain), target
   angle and die/punch combination;
3. estimates the per-bend correction with robust statistics (median of
   observed springback, dispersion as 1.4826*MAD);
4. derives a **draft** card from the sealed input using per-bend springback
   overrides, forcing the original order and tooling, which re-checks punch
   angle, stroke, daylight and the ram-descent swing collisions.

Too few samples, an incompatible working condition, excessive dispersion or
a correction past the configurable cap produce a suggestion with reasons
only — no card is derived and nothing is ever sealed automatically.  The
sealed card and measurement records stay read-only.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import (CorrectionThresholds, FirstPieceRequest,
                     PartCreate)
from .partmodel import Bend
from .service import CatalogError, solve

GRAIN_PARALLEL_DEG = 15.0


class CorrectionError(ValueError):
    """Request-level problem (HTTP 400)."""


class CorrectionConflict(ValueError):
    """Idempotent replay against another card (HTTP 409)."""


# ----------------------------------------------------------- small helpers

def _r3(x: float) -> float:
    return round(float(x), 3)


def grain_relation(part_input: PartCreate, b: Bend) -> str:
    ax = math.degrees(math.atan2(
        -(b.p1[0] - b.p0[0]), b.p1[1] - b.p0[1])) % 180.0
    d = min(abs(ax - part_input.grain_direction_deg % 180.0),
            180.0 - abs(ax - part_input.grain_direction_deg % 180.0))
    return "parallel" if d <= GRAIN_PARALLEL_DEG else "perpendicular"


def observed_springback(measured_included: float,
                        planned_overbend: float,
                        target_included: float) -> float:
    """Springback realised on the shop floor (deg).

    planned fold: 180 - target + overbend; after unloading the fold relaxes
    by the real springback, so the measured included angle equals
    target - overbend + springback.
    """
    return measured_included - target_included + planned_overbend


# ----------------------------------------------------------- robust stats

def median(values: List[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def robust_sigma(values: List[float], k: float) -> float:
    if len(values) < 2:
        return 0.0
    med = median(values)
    mad = median([abs(v - med) for v in values])
    return k * mad


# ----------------------------------------------------------- sample pool

def comparable_samples(pool: List[dict], *, material_id: str,
                       nominal_thickness: float,
                       grain: str, target_angle: float,
                       die_id: str, punch_id: str,
                       flip: bool, th: CorrectionThresholds
                       ) -> Tuple[List[dict], Dict[str, int]]:
    """Filter the historical measurement pool to comparable bends.

    The pool already includes the just-filed first-piece run: the first
    piece is the freshest evidence for its own correction.  The thickness
    band is tested around the nominal card thickness; a sample's effective
    thickness is its measured run thickness (falling back to the recorded
    nominal thickness for older records).
    """
    tlo = nominal_thickness * (1.0 - th.thickness_band)
    thi = nominal_thickness * (1.0 + th.thickness_band)
    breakdown = {"total": len(pool), "material": 0, "thickness": 0,
                 "grain": 0, "target_angle": 0, "tooling": 0,
                 "direction": 0, "kept": 0}
    kept: List[dict] = []
    for s in pool:
        if s["material_id"] != material_id:
            continue
        breakdown["material"] += 1
        st = s.get("measured_thickness_mm") or s["nominal_thickness_mm"]
        if not (tlo <= st <= thi):
            continue
        breakdown["thickness"] += 1
        if s["grain_relation"] != grain:
            continue
        breakdown["grain"] += 1
        if abs(s["target_included_angle_deg"] - target_angle) > \
                th.target_angle_tol_deg:
            continue
        breakdown["target_angle"] += 1
        if s["die_id"] != die_id or s["punch_id"] != punch_id:
            continue
        breakdown["tooling"] += 1
        if bool(s["flip"]) != flip:
            continue
        breakdown["direction"] += 1
        kept.append(s)
        breakdown["kept"] += 1
    return kept, breakdown


# ----------------------------------------------------------- validation

def _validate_measurements(card: dict, part_input: PartCreate,
                           body: FirstPieceRequest
                           ) -> List[Tuple[dict, dict]]:
    """Pair each submitted measurement with its sealed-card step."""
    result = card["result"]
    if not result.get("feasible"):
        raise CorrectionError("the sealed card has no feasible sequence; "
                              "first-piece correction is not possible")
    steps = result["steps"]
    by_step = {s["step_no"]: s for s in steps}
    by_bend = {s["bend_id"]: s for s in steps}
    if len(body.measurements) != len(steps):
        raise CorrectionError(
            f"expected one measurement per step ({len(steps)}), "
            f"got {len(body.measurements)}")
    seen_steps, seen_bends = set(), set()
    pairs: List[Tuple[dict, dict]] = []
    valid_bend_ids = {b.id for b in part_input.bends}
    for m in body.measurements:
        if m.step_no in seen_steps:
            raise CorrectionError(
                f"duplicate measurement for step {m.step_no}")
        if m.bend_id in seen_bends:
            raise CorrectionError(
                f"duplicate measurement for bend {m.bend_id}")
        seen_steps.add(m.step_no)
        seen_bends.add(m.bend_id)
        step = by_step.get(m.step_no)
        if step is None:
            raise CorrectionError(
                f"step {m.step_no} does not exist on the sealed card "
                f"(1..{len(steps)})")
        if m.bend_id not in valid_bend_ids:
            raise CorrectionError(
                f"unknown bend id {m.bend_id}")
        if step["bend_id"] != m.bend_id:
            raise CorrectionError(
                f"measurement for step {m.step_no} names bend "
                f"{m.bend_id!r} but the sealed card performs "
                f"{step['bend_id']!r} there")
        if step["die_id"] != m.die_id:
            raise CorrectionError(
                f"bend {m.bend_id}: measured with die {m.die_id!r}, "
                f"but the sealed step uses {step['die_id']!r}")
        if step["punch_id"] != m.punch_id:
            raise CorrectionError(
                f"bend {m.bend_id}: measured with punch {m.punch_id!r}, "
                f"but the sealed step uses {step['punch_id']!r}")
        if bool(step["flip"]) != m.flip:
            raise CorrectionError(
                f"bend {m.bend_id}: measured flip={m.flip}, but the sealed "
                f"step records flip={step['flip']} (bend direction mismatch)")
        pairs.append((m.model_dump(), step))
    return pairs


# ----------------------------------------------------------- main entry

def submit_first_piece(store, card_id: int, body: FirstPieceRequest) -> dict:
    card = store.get_card(card_id)
    if card is None:
        raise CatalogError("card not found")
    if card["status"] != "sealed":
        raise CorrectionError("first-piece measurements can only be "
                              "submitted against a sealed card")
    part_input = PartCreate(**card["input_snapshot"])

    existing = store.get_run_by_key(body.idempotency_key)
    if existing is not None:
        if existing["card_id"] != card_id:
            raise CorrectionConflict(
                "idempotency key already used against card "
                f"{existing['card_id']}")
        run = store.get_run(existing["run_id"])
        return {"decision": run["report"].get("decision", "recorded"),
                "replay": True, "run_id": run["run_id"],
                "card_id": card_id,
                "draft_card_id": run["draft_card_id"],
                "report": run["report"],
                "draft_card": (_card_brief(store, run["draft_card_id"])
                               if run["draft_card_id"] else None)}

    pairs = _validate_measurements(card, part_input, body)
    th = body.thresholds or CorrectionThresholds()
    measured_at = body.measured_at or datetime.now(timezone.utc)
    if measured_at.tzinfo is None:  # naive client timestamp -> assume UTC
        measured_at = measured_at.replace(tzinfo=timezone.utc)
    measured_at_s = measured_at.astimezone(timezone.utc).isoformat(
        timespec="seconds")

    # -- persist the run and its (immutable) measurements -------------
    run_id = store.create_first_piece_run(
        card_id, body.idempotency_key, body.material_lot,
        body.measured_thickness_mm, measured_at_s,
        (body.instrument.model_dump() if body.instrument else {}),
        body.note)
    bends_by_id = {b.id: b for b in part_input.bends}
    stored: List[dict] = []
    for m, step in pairs:
        b = bends_by_id[m["bend_id"]]
        planned = float(step.get("springback_used_deg",
                                 part_input.springback_deg))
        obs = observed_springback(m["measured_included_angle_deg"],
                                  planned, b.target_angle_deg)
        row = {
            "step_no": m["step_no"], "bend_id": m["bend_id"],
            "die_id": m["die_id"], "punch_id": m["punch_id"],
            "flip": m["flip"], "planned_overbend_deg": planned,
            "measured_included_angle_deg":
                m["measured_included_angle_deg"],
            "target_included_angle_deg": b.target_angle_deg,
            "observed_springback_deg": obs,
            "nominal_thickness_mm": part_input.thickness,
            "material_id": part_input.material_id,
            "grain_relation": grain_relation(part_input, b),
        }
        store.add_measurement(run_id, row)
        stored.append(row)

    report = _build_report(store, card, part_input, body, th, run_id,
                           stored, measured_at_s)
    draft_card_id: Optional[int] = None
    if report["decision"] == "draft_created":
        draft_card_id = report["draft_card_id"]
    store.finish_first_piece_run(run_id, report, draft_card_id)

    return {"decision": report["decision"], "replay": False,
            "run_id": run_id, "card_id": card_id,
            "draft_card_id": draft_card_id, "report": report,
            "draft_card": (_card_brief(store, draft_card_id)
                           if draft_card_id else None)}


# ----------------------------------------------------------- reporting

def _build_report(store, card: dict, part_input: PartCreate,
                  body: FirstPieceRequest, th: CorrectionThresholds,
                  run_id: int, stored: List[dict],
                  measured_at_s: str) -> dict:
    pool = store.all_measurements()
    t_nom = part_input.thickness
    incompatible = (
        abs(body.measured_thickness_mm - t_nom) >
        th.thickness_incompatible * t_nom)

    bend_reports: List[dict] = []
    overrides: Dict[str, float] = {}
    rejected: List[dict] = []
    for row in stored:
        bid = row["bend_id"]
        samples, breakdown = comparable_samples(
            pool, material_id=row["material_id"],
            nominal_thickness=t_nom,
            grain=row["grain_relation"],
            target_angle=row["target_included_angle_deg"],
            die_id=row["die_id"], punch_id=row["punch_id"],
            flip=row["flip"], th=th)
        obs_values = [s["observed_springback_deg"] for s in samples]
        med = median(obs_values) if obs_values else float("nan")
        sigma = robust_sigma(obs_values, th.mad_k) if obs_values else 0.0
        rejected_codes: List[str] = []
        if len(samples) < th.min_samples:
            rejected_codes.append("insufficient_samples")
        if sigma > th.max_robust_sigma_deg + 1e-9:
            rejected_codes.append("dispersion_exceeded")
        if obs_values and abs(med) > th.max_compensation_deg + 1e-9:
            rejected_codes.append("compensation_cap")
        # suggestion clamps into the allowed band; an automatic draft does
        # not (see compensation_cap rejection above)
        comp = None
        clamped = False
        if obs_values:
            comp = min(max(med, 0.0), th.max_compensation_deg)
            clamped = comp != med
        br = {
            "step_no": row["step_no"], "bend_id": bid,
            "grain_relation": row["grain_relation"],
            "die_id": row["die_id"], "punch_id": row["punch_id"],
            "flip": row["flip"],
            "angles": {
                "target_included_deg":
                    _r3(row["target_included_angle_deg"]),
                "measured_included_deg":
                    _r3(row["measured_included_angle_deg"]),
                "planned_overbend_deg": _r3(row["planned_overbend_deg"]),
                "observed_springback_deg":
                    _r3(row["observed_springback_deg"]),
                "overbend_before_deg": _r3(row["planned_overbend_deg"]),
                "overbend_after_deg": _r3(comp) if comp is not None else None,
                "ram_included_before_deg": _r3(
                    row["target_included_angle_deg"]
                    - row["planned_overbend_deg"]),
                "ram_included_after_deg": (
                    _r3(row["target_included_angle_deg"] - comp)
                    if comp is not None else None),
                "fold_before_deg": _r3(
                    180.0 - row["target_included_angle_deg"]
                    + row["planned_overbend_deg"]),
                "fold_after_deg": (
                    _r3(180.0 - row["target_included_angle_deg"] + comp)
                    if comp is not None else None),
            },
            "statistics": {
                "sample_count": len(samples),
                "min_samples": th.min_samples,
                "median_springback_deg": _r3(med)
                    if obs_values else None,
                "robust_sigma_deg": _r3(sigma),
                "sigma_limit_deg": th.max_robust_sigma_deg,
                "compensation_cap_deg": th.max_compensation_deg,
                "filter_counts": breakdown,
            },
            "samples_used": [_sample_ref(s) for s in samples],
            "suggested_overbend_deg": _r3(comp)
                if comp is not None else None,
            "clamped_to_cap": bool(clamped),
            "rejected_reasons": rejected_codes,
            "accepted": not rejected_codes,
        }
        bend_reports.append(br)
        if rejected_codes:
            rejected.append({"bend_id": bid,
                             "reasons": rejected_codes})
        else:
            overrides[bid] = _r3(comp)

    if incompatible:
        rejected.append({"global": "incompatible_condition",
                         "reasons": ["incompatible_condition"],
                         "detail": (
                             f"measured thickness "
                             f"{body.measured_thickness_mm:g} mm deviates "
                             f"from the nominal {t_nom:g} mm by more than "
                             f"{th.thickness_incompatible:.0%}")})

    order = [s["bend_id"] for s in card["result"]["steps"]]
    tool_drift: List[dict] = []
    replan_ok = False
    draft_card_id: Optional[int] = None
    derived: Dict[str, Any] = {}

    gates_blocked = bool(rejected) or incompatible
    if not gates_blocked:
        derived_input, derived = _derive_input(part_input, card, overrides)
        res = solve(store, derived_input, forced_order=order)
        svgs = res.pop("_svgs")
        segment_ids = res.pop("_segment_ids")
        replan_ok = bool(res["feasible"])
        if replan_ok:
            tool_drift = _tooling_drift(card, res)
            if tool_drift:
                replan_ok = False
        if not replan_ok:
            rejection = {
                "reasons": (["replanning_failed"] if not res["feasible"]
                            else ["tooling_direction_drift"]),
                "failure": res.get("failure"),
                "tooling_drift": tool_drift,
            }
            rejected.append({"global": "derived_plan", **rejection})
        else:
            new_part_id = store.create_part(derived_input)
            correction_meta = _correction_meta(
                card_id=card["card_id"], run_id=run_id, body=body, th=th,
                bend_reports=bend_reports, kind="first_piece_correction",
                material_lot=body.material_lot,
                measured_thickness_mm=body.measured_thickness_mm,
                measured_at=measured_at_s)
            draft_card_id = store.create_card(
                new_part_id, res, svgs, parent_card_id=card["card_id"],
                input_snapshot=derived_input.model_dump(),
                correction=correction_meta, segment_ids=segment_ids)

    decision = "draft_created" if draft_card_id is not None \
        else "suggestion_only"
    report = {
        "decision": decision,
        "sealed_card_id": card["card_id"],
        "sealed_card_version": card["version"],
        "material_id": part_input.material_id,
        "material_lot": body.material_lot,
        "nominal_thickness_mm": t_nom,
        "measured_thickness_mm": body.measured_thickness_mm,
        "measured_at": measured_at_s,
        "instrument": (body.instrument.model_dump()
                       if body.instrument else None),
        "thresholds": th.model_dump(),
        "bends": bend_reports,
        "rejections": rejected,
        "order_forced": order,
        "derived_plan": derived,
        "draft_card_id": draft_card_id,
        "note": body.note,
    }
    return report


def _sample_ref(s: dict) -> dict:
    return {
        "run_id": s["run_id"], "card_id": s["card_id"],
        "step_no": s["step_no"], "bend_id": s["bend_id"],
        "material_id": s["material_id"],
        "material_lot": s.get("material_lot"),
        "thickness_mm": _r3(s.get("measured_thickness_mm")
                            or s["nominal_thickness_mm"]),
        "grain_relation": s["grain_relation"],
        "die_id": s["die_id"], "punch_id": s["punch_id"],
        "flip": bool(s["flip"]),
        "target_included_deg": _r3(s["target_included_angle_deg"]),
        "planned_overbend_deg": _r3(s["planned_overbend_deg"]),
        "measured_included_deg":
            _r3(s["measured_included_angle_deg"]),
        "observed_springback_deg": _r3(s["observed_springback_deg"]),
        "measured_at": s.get("measured_at"),
    }


def _derive_input(part_input: PartCreate, card: dict,
                  overrides: Dict[str, float]
                  ) -> Tuple[PartCreate, dict]:
    """Branch the sealed part input with per-bend overbend allowances.

    Candidate tools are restricted to the tool set actually used on the
    sealed card so the re-plan cannot silently change tooling.
    """
    used_dies = sorted({s["die_id"] for s in card["result"]["steps"]})
    used_punches = sorted({s["punch_id"]
                           for s in card["result"]["steps"]})
    data = part_input.model_copy(update={
        "candidate_dies": used_dies,
        "candidate_punches": used_punches,
        "springback_overrides": dict(overrides),
    })
    return data, {
        "candidate_dies": used_dies,
        "candidate_punches": used_punches,
        "springback_overrides": {k: _r3(v) for k, v in overrides.items()},
    }


def _tooling_drift(card: dict, res: dict) -> List[dict]:
    """Every derived step must keep the original bend, die, punch, flip."""
    drift: List[dict] = []
    old = {s["step_no"]: s for s in card["result"]["steps"]}
    for step in res["steps"]:
        o = old[step["step_no"]]
        for key in ("bend_id", "die_id", "punch_id", "flip"):
            if step[key] != o[key]:
                drift.append({"step_no": step["step_no"], "field": key,
                              "sealed": o[key], "derived": step[key]})
    return drift


def _correction_meta(*, card_id: int, run_id: int, body: FirstPieceRequest,
                     th: CorrectionThresholds, bend_reports: List[dict],
                     kind: str, material_lot: str,
                     measured_thickness_mm: float,
                     measured_at: Optional[str]) -> dict:
    return {
        "kind": kind,
        "source_card_id": card_id,
        "first_piece_run_id": run_id,
        "material_lot": material_lot,
        "measured_thickness_mm": measured_thickness_mm,
        "measured_at": measured_at,
        "instrument": (body.instrument.model_dump()
                       if body.instrument else None),
        "thresholds": th.model_dump(),
        "bends": [{
            "step_no": b["step_no"], "bend_id": b["bend_id"],
            "grain_relation": b["grain_relation"],
            "die_id": b["die_id"], "punch_id": b["punch_id"],
            "flip": b["flip"],
            "angles": b["angles"],
            "statistics": b["statistics"],
            "samples_used": b["samples_used"],
            "suggested_overbend_deg": b["suggested_overbend_deg"],
        } for b in bend_reports],
    }


def _card_brief(store, card_id: Optional[int]) -> Optional[dict]:
    if card_id is None:
        return None
    row = store.get_card(card_id)
    if row is None:
        return None
    return {
        "card_id": row["card_id"], "part_id": row["part_id"],
        "version": row["version"], "status": row["status"],
        "parent_card_id": row["parent_card_id"],
        "result": row["result"],
        "input_snapshot": row["input_snapshot"],
        "correction": row["correction"],
    }
