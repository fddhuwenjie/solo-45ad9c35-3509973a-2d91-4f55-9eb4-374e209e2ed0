"""Bend sequence planner.

Depth-first, deterministic search over (remaining bend, die, punch, flip)
configurations.  Folded geometry depends only on the *set* of completed bends,
so evaluated subsets are memoised; ties are broken by fixed id ordering, which
makes the returned feasible order stable across runs.

Each step checks, in order: minimum flange, minimum radius/grain, die V-angle
and length, punch fit, tonnage, stroke/daylight, then the machine setup
(backgauge reach/reference/occlusion, frame/throat, tool and self collisions
both at closure and while the ram descends).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from . import engineering as E
from .machine import Check, simulate_setup, swing_sections
from .models import Die, Material, PressBrake, Punch
from .partmodel import Bend, Part

# maximum bend sets explored before giving up (10 bends -> 1024 subsets max)
NODE_CAP = 20000


@dataclass
class StepPlan:
    bend_id: str
    die_id: str
    punch_id: str
    flip: bool
    orientation: str
    feed_direction: str
    backgauge_distance_mm: float
    backgauge_contact: dict
    tonnage_kn: float
    bend_angle_deg: float
    fold_angle_deg: float
    checks: List[Check]
    section: List[Tuple[float, float, float, float, int]]
    part: Part


@dataclass
class DeadEnd:
    depth: int
    bend_id: str
    reasons: Dict[str, Check] = field(default_factory=dict)
    count: int = 0


class PlanningContext:
    def __init__(self, contour, bends: Sequence[Bend], thickness: float,
                 grain_angle_deg: float, springback_deg: float,
                 material: Material, machine: PressBrake,
                 dies: List[Die], punches: List[Punch],
                 springback_overrides: Optional[Dict[str, float]] = None):
        self.contour = contour
        self.bends = sorted(bends, key=lambda b: b.id)
        overrides = springback_overrides or {}
        for b in self.bends:
            b.springback_used = float(
                overrides.get(b.id, springback_deg))
            b.fold_angle = E.included_to_press_angle(
                b.target_angle, b.springback_used)
        self.thickness = thickness
        self.grain = grain_angle_deg
        self.springback = springback_deg
        self.springback_overrides = dict(overrides)
        self.material = material
        self.machine = machine
        self.dies = {d.id: d for d in dies}
        self.punches = {p.id: p for p in punches}

    def bend(self, bid: str) -> Bend:
        return next(b for b in self.bends if b.id == bid)


def _engineering_checks(ctx: PlanningContext, b: Bend, die: Die,
                        punch: Punch) -> Tuple[List[Check], float, float]:
    m = ctx.material
    checks: List[Check] = []
    # V window
    vmin, vpref, vmax = E.v_opening_range(ctx.thickness)
    if vmin - 1e-6 <= die.v_width <= vmax + 1e-6:
        checks.append(Check("v_opening", True,
                            f"V {die.v_width:g} in [{vmin:g}, {vmax:g}] "
                            f"(preferred {vpref:g})", die.v_width, vmax))
    else:
        checks.append(Check("v_opening", False,
                            f"V {die.v_width:g} outside [{vmin:g}, {vmax:g}]",
                            die.v_width, vmax))
    # minimum flange (flat outward width on the flange side)
    flange = E.flange_depth_outward(b.p0, b.p1, ctx.contour, b.flange_side)
    need = E.min_flange_length(ctx.thickness, die.v_width)
    if flange + ctx.thickness >= need:
        checks.append(Check("min_flange", True,
                            f"flange {flange:.1f} mm >= {need:.1f} mm",
                            flange, need))
    else:
        checks.append(Check("min_flange", False,
                            f"flange {flange:.1f} mm shorter than the "
                            f"minimum {need:.1f} mm on V {die.v_width:g}",
                            flange, need))
    # inside radius vs grain
    parallel = b.axis_angle_to_grain <= 15.0
    min_r_t = (m.min_inside_radius_t_parallel if parallel
               else m.min_inside_radius_t)
    min_r = min_r_t * ctx.thickness
    if b.radius + 1e-6 >= min_r:
        checks.append(Check("min_radius_grain", True,
                            f"R{b.radius:g} >= {min_r:g} "
                            f"({'parallel' if parallel else 'across'} grain)",
                            b.radius, min_r))
    else:
        checks.append(Check(
            "min_radius_grain", False,
            f"inside R {b.radius:g} < {min_r:g} required for a bend "
            f"{'parallel' if parallel else 'across'} the grain "
            f"(cracking risk)", b.radius, min_r))
    # die V angle
    if E.die_supports_angle(die, b.target_angle):
        checks.append(Check("die_angle", True,
                            f"die V {die.v_angle_deg:g}° accepts "
                            f"{b.target_angle:g}°"))
    else:
        checks.append(Check("die_angle", False,
                            f"die V {die.v_angle_deg:g}° wider than the "
                            f"{b.target_angle:g}° target"))
    # tooling length along the bend
    if die.die_length + 1e-6 >= b.length():
        checks.append(Check("die_length", True,
                            f"die {die.die_length:g} >= bend {b.length():.1f}",
                            b.length(), die.die_length))
    else:
        checks.append(Check("die_length", False,
                            f"die {die.die_length:g} shorter than bend "
                            f"{b.length():.1f}", b.length(), die.die_length))
    if punch.punch_length + 1e-6 >= b.length():
        checks.append(Check("punch_length", True,
                            f"punch {punch.punch_length:g} >= bend "
                            f"{b.length():.1f}", b.length(),
                            punch.punch_length))
    else:
        checks.append(Check("punch_length", False,
                            f"punch {punch.punch_length:g} shorter than "
                            f"bend {b.length():.1f}", b.length(),
                            punch.punch_length))
    # punch fit
    ok, problems = E.punch_fits(punch, die, b.radius, ctx.thickness,
                                b.target_angle, b.springback_used)
    checks.append(Check("punch_fit", ok,
                        "; ".join(problems) if problems else
                        f"punch {punch.id} fits V {die.id} and R{b.radius:g}"))
    # tonnage
    f = E.tonnage_kn(ctx.thickness, b.length(), die.v_width,
                     m.tensile_strength_mpa, b.target_angle)
    if f <= ctx.machine.tonnage_kn + 1e-6:
        checks.append(Check("tonnage", True,
                            f"{f:.1f} kN <= press {ctx.machine.tonnage_kn:g}",
                            f, ctx.machine.tonnage_kn))
    else:
        checks.append(Check("tonnage", False,
                            f"{f:.1f} kN exceeds press "
                            f"{ctx.machine.tonnage_kn:g} kN",
                            f, ctx.machine.tonnage_kn))
    # stroke and daylight
    stroke = E.required_stroke(ctx.thickness, die.v_width,
                               b.target_angle, b.springback_used)
    if stroke <= ctx.machine.stroke_mm + 1e-6:
        checks.append(Check("stroke", True,
                            f"stroke {stroke:.1f} <= "
                            f"{ctx.machine.stroke_mm:g}",
                            stroke, ctx.machine.stroke_mm))
    else:
        checks.append(Check("stroke", False,
                            f"stroke {stroke:.1f} > press travel "
                            f"{ctx.machine.stroke_mm:g}",
                            stroke, ctx.machine.stroke_mm))
    daylight = (punch.punch_height + die.die_height + stroke +
                ctx.thickness)
    if daylight <= ctx.machine.open_height_mm + 1e-6:
        checks.append(Check("daylight", True,
                            f"tool stack {daylight:.1f} <= daylight "
                            f"{ctx.machine.open_height_mm:g}",
                            daylight, ctx.machine.open_height_mm))
    else:
        checks.append(Check("daylight", False,
                            f"tool stack {daylight:.1f} exceeds daylight "
                            f"{ctx.machine.open_height_mm:g}",
                            daylight, ctx.machine.open_height_mm))
    return checks, f, stroke


def _try_config(ctx: PlanningContext, part_before: Part, b: Bend,
                die: Die, punch: Punch, done: FrozenSet[str]
                ) -> Tuple[Optional[StepPlan], List[Check]]:
    eng_checks, force, stroke = _engineering_checks(ctx, b, die, punch)
    hard = [c for c in eng_checks if not c.ok]
    if hard:
        return None, hard
    best_failed: List[Check] = []
    for flip in (False, True):
        closed_part = part_before.clone()
        closed_part.apply_fold(b.id)
        setup = simulate_setup(closed_part, b, flip=flip, die=die,
                               punch=punch, machine=ctx.machine,
                               thickness=ctx.thickness,
                               applied_ids=list(done))
        checks = list(eng_checks)
        checks.extend(setup.checks)
        # ram descent: intermediate fold fractions, same placement as closed
        for stage in swing_sections(part_before, b, 4)[:-1]:
            swing = simulate_setup(stage, b, flip=flip, die=die,
                                   punch=punch, machine=ctx.machine,
                                   thickness=ctx.thickness,
                                   applied_ids=list(done),
                                   axis_q=setup.axis_q,
                                   axis_u=setup.axis_u)
            for c in swing.checks:
                if not c.ok and c.code in ("frame_collision",
                                           "tool_collision",
                                           "self_collision"):
                    checks.append(Check(c.code, False,
                                        f"during ram descent: {c.detail}"))
        if all(c.ok for c in checks):
            return StepPlan(
                bend_id=b.id, die_id=die.id, punch_id=punch.id,
                flip=flip, orientation=setup.orientation,
                feed_direction=setup.feed_direction,
                backgauge_distance_mm=setup.gauge.distance,
                backgauge_contact={
                    "edge_bend_id": setup.gauge.edge_bend_id,
                    "distance_mm": round(setup.gauge.distance, 3),
                    "contact_z": round(setup.gauge.contact_z, 3),
                },
                tonnage_kn=round(force, 2),
                bend_angle_deg=b.target_angle,
                fold_angle_deg=b.fold_angle,
                checks=checks, section=setup.section,
                part=closed_part), []
        failed = [c for c in checks if not c.ok]
        if not best_failed or len(failed) < len(best_failed):
            best_failed = failed
    return None, best_failed


def plan(ctx: PlanningContext, forced_order: Optional[List[str]] = None
         ) -> Tuple[bool, List[StepPlan], Optional[dict]]:
    all_ids = [b.id for b in ctx.bends]
    if forced_order is not None:
        if sorted(forced_order) != sorted(all_ids) or \
                len(set(forced_order)) != len(forced_order):
            return False, [], {
                "earliest_step": 0, "bend_id": None,
                "joint_constraints": [{
                    "code": "sequence", "ok": False,
                    "detail": "sequence must list every bend exactly once",
                    "value": None, "limit": None}],
                "all_orders_exhausted": 0,
                "message": "invalid technician sequence"}

    dies_sorted = [ctx.dies[d] for d in sorted(ctx.dies)]
    punches_sorted = [ctx.punches[p] for p in sorted(ctx.punches)]

    dead: Dict[Tuple[int, str], DeadEnd] = {}
    stats = {"nodes": 0, "capped": False}
    failed_subtrees: Dict[FrozenSet[str], bool] = {}

    def state_part(done: List[str]) -> Part:
        part = Part.build(ctx.contour, ctx.bends, ctx.thickness, ctx.grain)
        for bid in done:
            part.apply_fold(bid)
        return part

    def record_fail(depth, bid, failed):
        key = (depth, bid)
        d = dead.get(key)
        if d is None:
            d = DeadEnd(depth, bid)
            dead[key] = d
        d.count += 1
        for c in failed:
            if c.code not in d.reasons:
                d.reasons[c.code] = c

    def dfs(done: List[str], steps: List[StepPlan]) -> Optional[List[StepPlan]]:
        doneset = frozenset(done)
        if doneset in failed_subtrees:
            return None
        stats["nodes"] += 1
        if stats["nodes"] > NODE_CAP:
            stats["capped"] = True
            failed_subtrees[doneset] = True
            return None
        if len(done) == len(all_ids):
            return list(steps)
        remaining = ([forced_order[len(done)]] if forced_order is not None
                     else [i for i in all_ids if i not in doneset])
        for bid in remaining:
            b = ctx.bend(bid)
            part_before = state_part(done)
            depth = len(done) + 1
            step_ok = False
            best_failed: List[Check] = []
            for die in dies_sorted:
                for punch in punches_sorted:
                    sp, failed = _try_config(ctx, part_before, b, die,
                                             punch, doneset)
                    if sp is None:
                        # keep the smallest failure set seen for this bend
                        if not best_failed or len(failed) < len(best_failed):
                            best_failed = failed
                        continue
                    res = dfs(done + [bid], steps + [sp])
                    if res is not None:
                        return res
                    step_ok = True  # step feasible, only suffix failed
            if not step_ok:
                record_fail(depth, bid, best_failed)
        failed_subtrees[doneset] = True
        return None

    result = dfs([], [])
    if result is not None:
        return True, result, None
    if not dead:
        return False, [], _failure(
            1, None,
            [Check("search_cap", True,
                   "search node cap reached" if stats["capped"]
                   else "no feasible configuration found")],
            1, stats["capped"])
    d = dead[min(dead)]
    return False, [], _failure(d.depth, d.bend_id,
                              list(d.reasons.values()), d.count,
                              stats["capped"])


def _failure(depth, bid, checks, count, capped) -> dict:
    from dataclasses import asdict
    return {
        "earliest_step": depth,
        "bend_id": bid,
        "joint_constraints": [
            {"code": c.code, "ok": False, "detail": c.detail,
             "value": c.value, "limit": c.limit} for c in checks],
        "all_orders_exhausted": count,
        "message": ("no feasible order: at step "
                    f"{depth} (bend {bid}) every tried configuration "
                    "violates the listed constraints"
                    + (" [search capped]" if capped else "")),
    }
