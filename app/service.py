"""Service layer: request models -> geometry/planner -> result DTOs/SVG."""
from __future__ import annotations

from dataclasses import asdict
from typing import List, Optional, Tuple

from . import engineering as E
from . import svg as svgmod
from .models import (BranchRequest, Die, LayoutCheckRequest, Material,
                     PartCreate, PressBrake, Punch, TechnicianSequence)
from .partmodel import Bend, Part
from .planner import PlanningContext, StepPlan, plan
from .segments import (LayoutSolver, Segment, choose_step_layouts,
                       tool_changes, validate_manual_layout)


class CatalogError(ValueError):
    pass


def load_segments(store) -> List[Segment]:
    return [Segment(id=r["segment_id"], kind=r["kind"],
                    profile_id=r["profile_id"], length_mm=r["length_mm"],
                    handedness=r["handedness"],
                    clamp_system=r["clamp_system"], retired=r["retired"])
            for r in store.list_segments()]


def build_layout_solver(store, machine: PressBrake,
                        data: PartCreate) -> LayoutSolver:
    spec = data.layout
    return LayoutSolver(
        load_segments(store),
        bed_width_mm=machine.bed_width_mm,
        clamp_forbidden_zones=[(z.start_mm, z.end_mm)
                               for z in machine.clamp_forbidden_zones],
        seam_keepout_zones=[(z.start_mm, z.end_mm)
                            for z in (spec.seam_keepout_zones
                                      if spec else [])],
        punch_clamp_system=machine.punch_clamp_system,
        die_clamp_system=machine.die_clamp_system,
        end_margin_mm=spec.end_margin_mm if spec else 10.0)


def load_context(store, data: PartCreate) -> Tuple[
        PlanningContext, Material, PressBrake, List[Die], List[Punch]]:
    mat = store.catalog("material", data.material_id)
    if not mat:
        raise CatalogError(f"unknown material {data.material_id}")
    material = Material(**mat)
    mch = store.catalog("press", data.machine_id)
    if not mch:
        raise CatalogError(f"unknown machine {data.machine_id}")
    machine = PressBrake(**mch)
    dies: List[Die] = []
    for did in data.candidate_dies:
        d = store.catalog("die", did)
        if not d:
            raise CatalogError(f"unknown die {did}")
        dies.append(Die(**d))
    punches: List[Punch] = []
    for pid in data.candidate_punches:
        p = store.catalog("punch", pid)
        if not p:
            raise CatalogError(f"unknown punch {pid}")
        punches.append(Punch(**p))
    if not dies or not punches:
        raise CatalogError("at least one die and one punch are required")

    bends = [Bend(id=b.id, p0=tuple(b.p0), p1=tuple(b.p1),
                  target_angle=b.target_angle_deg,
                  radius=b.inside_radius,
                  flange_side=b.flange_side)
             for b in data.bends]
    ids = [b.id for b in bends]
    if len(set(ids)) != len(ids):
        raise CatalogError("bend ids must be unique")
    overrides = data.springback_overrides or {}
    unknown = set(overrides) - set(ids)
    if unknown:
        raise CatalogError(
            f"springback overrides reference unknown bends: "
            f"{sorted(unknown)}")

    ctx = PlanningContext(
        contour=[tuple(p) for p in data.contour], bends=bends,
        thickness=data.thickness,
        grain_angle_deg=data.grain_direction_deg,
        springback_deg=data.springback_deg, material=material,
        machine=machine, dies=dies, punches=punches,
        springback_overrides=overrides,
        layout=build_layout_solver(store, machine, data))
    return ctx, material, machine, dies, punches


def _check_dto(c):
    return {"code": c.code, "ok": c.ok, "detail": c.detail,
            "value": c.value, "limit": c.limit}


def _r3(x: float) -> float:
    return round(float(x), 3)


def _rail_dto(L) -> dict:
    return {
        "rail": L.rail,
        "profile_id": L.profile_id,
        "integral": L.integral,
        "pieces": [{"ref": p.ref, "kind": p.kind, "profile_id": p.profile_id,
                    "x0_mm": _r3(p.x0), "x1_mm": _r3(p.x1),
                    "length_mm": _r3(p.x1 - p.x0),
                    "handedness": p.handedness} for p in L.pieces],
        "seams_mm": [_r3(s) for s in L.seams],
        "offset_mm": _r3(L.offset),
        "covered_mm": [_r3(L.covered[0]), _r3(L.covered[1])],
        "required_mm": [_r3(L.required[0]), _r3(L.required[1])],
        "bend_span_mm": [_r3(L.bend_span[0]), _r3(L.bend_span[1])],
        "overhang_left_mm": _r3(L.bend_span[0] - L.covered[0]),
        "overhang_right_mm": _r3(L.covered[1] - L.bend_span[1]),
    }


def _allocate_layouts(ctx, steps, die_by_id, punch_by_id):
    """Choose one bed layout per step and compute tool-change actions.

    Returns (per-step layout DTOs, summary, used segment ids); empty when
    there is nothing to lay out.
    """
    solver = ctx.layout
    if solver is None or not steps:
        return [], None, []
    per_step = []
    for sp in steps:
        b = ctx.bend(sp.bend_id)
        die = die_by_id[sp.die_id]
        punch = punch_by_id[sp.punch_id]
        die_cands, _ = solver.rail_layouts("die", die.id, die.die_length,
                                           b.length())
        punch_cands, _ = solver.rail_layouts("punch", punch.id,
                                             punch.punch_length, b.length())
        per_step.append((die_cands, punch_cands))
    chosen = choose_step_layouts(per_step)
    if not chosen:
        return [], None, []
    layouts: List[dict] = []
    totals = {"mount": 0, "remove": 0, "shift": 0, "keep": 0}
    used = set()
    prev = None
    for sl in chosen:
        actions = tool_changes(prev, sl)
        for a in actions:
            totals[a["action"]] += 1
        for rail in (sl.die, sl.punch):
            for p in rail.pieces:
                if p.kind == "segment":
                    used.add(p.ref)
        layouts.append({
            "end_margin_mm": solver.end_margin_mm,
            "die": _rail_dto(sl.die),
            "punch": _rail_dto(sl.punch),
            "tool_changes": actions,
        })
        prev = sl
    summary = {
        "mounts": totals["mount"],
        "removals": totals["remove"],
        "shifts": totals["shift"],
        "kept": totals["keep"],
        "segmented_steps": sum(
            1 for sl in chosen if not sl.die.integral
            or not sl.punch.integral),
        "segments_used": sorted(used),
    }
    return layouts, summary, sorted(used)


def solve(store, data: PartCreate,
          forced_order: Optional[List[str]] = None) -> dict:
    ctx, material, machine, dies, punches = load_context(store, data)
    feasible, steps, failure = plan(ctx, forced_order=forced_order)
    die_by_id = {d.id: d for d in dies}
    punch_by_id = {p.id: p for p in punches}
    layouts, summary, segment_ids = ([], None, [])
    if feasible and steps:
        layouts, summary, segment_ids = _allocate_layouts(
            ctx, steps, die_by_id, punch_by_id)
    step_dtos: List[dict] = []
    svgs: List[str] = []
    for i, sp in enumerate(steps, start=1):
        b = ctx.bend(sp.bend_id)
        die = die_by_id[sp.die_id]
        punch = punch_by_id[sp.punch_id]
        title = (f"Step {i} — bend {sp.bend_id} "
                 f"{b.target_angle:g}° ({'flipped' if sp.flip else 'direct'})")
        drawing = svgmod.render_step(
            sp.section, die, punch, machine, title=title,
            gauge_distance=sp.backgauge_distance_mm)
        svgs.append(drawing)
        step_dtos.append({
            "step_no": i,
            "bend_id": sp.bend_id,
            "workpiece_orientation": sp.orientation,
            "feed_direction": sp.feed_direction,
            "flip": sp.flip,
            "die_id": sp.die_id,
            "punch_id": sp.punch_id,
            "springback_used_deg": round(b.springback_used, 3),
            "backgauge_distance_mm": round(sp.backgauge_distance_mm, 3),
            "backgauge_contact": sp.backgauge_contact,
            "tonnage_kn": sp.tonnage_kn,
            "bend_angle_deg": sp.bend_angle_deg,
            "fold_angle_deg": sp.fold_angle_deg,
            "checks": [_check_dto(c) for c in sp.checks],
            "section": [
                {"s1": round(a, 3), "z1": round(bz, 3),
                 "s2": round(c2, 3), "z2": round(d2, 3),
                 "face": fi}
                for a, bz, c2, d2, fi in sp.section],
            "layout": layouts[i - 1] if layouts else None,
        })
    return {
        "feasible": feasible,
        "order": [s.bend_id for s in steps],
        "steps": step_dtos,
        "failure": failure,
        "geometry_version": 1,
        "tool_change_summary": summary,
        "_svgs": svgs,
        "_segment_ids": segment_ids,
    }


def solve_for_card(store, part_id: int,
                   forced_order: Optional[List[str]] = None) -> dict:
    data = store.part_input(part_id)
    if data is None:
        raise CatalogError("unknown part")
    res = solve(store, data, forced_order=forced_order)
    svgs = res.pop("_svgs")
    segment_ids = res.pop("_segment_ids")
    return res, svgs, data.model_dump(), segment_ids


def check_manual_layout(store, part_id: int, body: LayoutCheckRequest
                        ) -> dict:
    """Validate technician-specified segment layouts (人工配段校验)."""
    data = store.part_input(part_id)
    if data is None:
        raise CatalogError("unknown part")
    ctx, material, machine, dies, punches = load_context(store, data)
    solver = ctx.layout
    seg_by_id = {s.id: s for s in solver.segments}
    bend_len = {b.id: b.length() for b in ctx.bends}
    results: List[dict] = []
    for entry in body.layouts:
        if entry.bend_id not in bend_len:
            results.append({
                "bend_id": entry.bend_id, "ok": False,
                "violations": [{"code": "unknown_bend",
                                "detail": f"bend {entry.bend_id} is not "
                                          f"part of this workpiece"}]})
            continue
        violations, info = validate_manual_layout(
            solver, bend_length_mm=bend_len[entry.bend_id],
            die_profile=entry.die_profile,
            punch_profile=entry.punch_profile,
            die_pieces=[p.model_dump() for p in entry.die_pieces],
            punch_pieces=[p.model_dump() for p in entry.punch_pieces],
            segments_by_id=seg_by_id,
            candidate_dies=data.candidate_dies,
            candidate_punches=data.candidate_punches)
        results.append({"bend_id": entry.bend_id, "ok": not violations,
                        "violations": violations, **info})
    return {"part_id": part_id, "ok": all(r["ok"] for r in results),
            "layouts": results}
