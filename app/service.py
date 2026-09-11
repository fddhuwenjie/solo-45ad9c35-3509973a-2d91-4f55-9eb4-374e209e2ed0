"""Service layer: request models -> geometry/planner -> result DTOs/SVG."""
from __future__ import annotations

from dataclasses import asdict
from typing import List, Optional, Tuple

from . import engineering as E
from . import svg as svgmod
from .models import (BranchRequest, Die, Material, PartCreate, PressBrake,
                     Punch, TechnicianSequence)
from .partmodel import Bend, Part
from .planner import PlanningContext, StepPlan, plan


class CatalogError(ValueError):
    pass


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

    ctx = PlanningContext(
        contour=[tuple(p) for p in data.contour], bends=bends,
        thickness=data.thickness,
        grain_angle_deg=data.grain_direction_deg,
        springback_deg=data.springback_deg, material=material,
        machine=machine, dies=dies, punches=punches)
    return ctx, material, machine, dies, punches


def _check_dto(c):
    return {"code": c.code, "ok": c.ok, "detail": c.detail,
            "value": c.value, "limit": c.limit}


def solve(store, data: PartCreate,
          forced_order: Optional[List[str]] = None) -> dict:
    ctx, material, machine, dies, punches = load_context(store, data)
    feasible, steps, failure = plan(ctx, forced_order=forced_order)
    die_by_id = {d.id: d for d in dies}
    punch_by_id = {p.id: p for p in punches}
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
        })
    return {
        "feasible": feasible,
        "order": [s.bend_id for s in steps],
        "steps": step_dtos,
        "failure": failure,
        "geometry_version": 1,
        "_svgs": svgs,
    }


def solve_for_card(store, part_id: int,
                   forced_order: Optional[List[str]] = None) -> dict:
    data = store.part_input(part_id)
    if data is None:
        raise CatalogError("unknown part")
    res = solve(store, data, forced_order=forced_order)
    svgs = res.pop("_svgs")
    return res, svgs, data.model_dump()
