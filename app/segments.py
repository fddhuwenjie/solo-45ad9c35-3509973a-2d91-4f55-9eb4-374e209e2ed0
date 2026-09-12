"""Segmented tooling layout (组合模具配段).

Long bend lines are formed with several same-profile punch/die segments
butted end to end.  This module:

* enumerates deterministic rail layouts — an integral catalog tool, or an
  ordered train of physical segments — that cover the bend line plus the
  end margins, fit the bed width, keep out of the clamp forbidden zones
  and keep every seam (joint between segments) out of the seam keep-out
  zones;
* chooses one layout per step so that adjacent steps reuse mounted
  segments first, then minimise mounts, removals and lateral shift
  (lexicographic, stable);
* validates technician-specified manual layouts: duplicate physical
  occupancy, profile/kind/clamp incompatibility, out-of-bed placement,
  forbidden zones, seam keep-out and insufficient coverage.

Bed coordinate: x along the bed, origin at the machine centre.  The bend
line is centred at x=0, so a bend of length L with end margin m must be
covered by [-L/2-m, L/2+m].
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

EPS = 1e-6
MAX_ASSEMBLIES = 200       # ordered segment trains kept per rail
MAX_ASSEMBLY_NODES = 200000  # enumeration safety cap
MAX_TRAINS_FOR_OFFSETS = 40  # best trains expanded to placements
MAX_OFFSETS = 6            # discrete placements kept per assembly
MAX_RAIL_CANDIDATES = 12   # rail layouts forwarded to the sequencer
MAX_STEP_CANDIDATES = 16   # die x punch combinations per step
CONTIGUITY_TOL = 0.5       # mm slack accepted in manual piece placement

Zone = Tuple[float, float]


# ---------------------------------------------------------------- entities

@dataclass(frozen=True)
class Segment:
    """One physical tool segment (实体模段) from the inventory."""
    id: str
    kind: str                 # "die" | "punch"
    profile_id: str
    length_mm: float
    handedness: str = "any"   # "left" | "right" | "any"
    clamp_system: str = "STD"
    retired: bool = False


@dataclass
class Piece:
    """A tool placed on the bed: a physical segment or an integral tool."""
    ref: str                  # segment id, or profile id for integral tools
    kind: str                 # "segment" | "integral"
    profile_id: str
    x0: float
    x1: float
    handedness: str = "any"

    @property
    def length(self) -> float:
        return self.x1 - self.x0


@dataclass
class RailLayout:
    rail: str                 # "die" | "punch"
    profile_id: str
    integral: bool
    pieces: List[Piece]
    seams: List[float]        # bed coordinates of the joints
    offset: float             # x0 of the first piece
    covered: Zone             # span covered by the assembly
    required: Zone            # bend line + end margins
    bend_span: Zone           # the bend line itself


@dataclass
class LayoutFailure:
    """Why no rail layout exists: shortage profile/length or first
    conflicting interval."""
    code: str                 # segment_shortage | bed_width |
                              # clamp_forbidden | seam_keepout | no_placement
    profile_id: str
    required_mm: float
    available_mm: float
    conflict: Optional[Zone]
    detail: str


@dataclass
class StepLayout:
    die: RailLayout
    punch: RailLayout


# ---------------------------------------------------------------- solver

class LayoutSolver:
    """Deterministic per-rail layout enumeration on one machine."""

    def __init__(self, segments: Sequence[Segment], *, bed_width_mm: float,
                 clamp_forbidden_zones: Sequence[Zone] = (),
                 seam_keepout_zones: Sequence[Zone] = (),
                 punch_clamp_system: str = "STD",
                 die_clamp_system: str = "STD",
                 end_margin_mm: float = 10.0):
        self.segments = list(segments)
        self.bed_width = float(bed_width_mm)
        self.forbidden: List[Zone] = sorted(
            (float(a), float(b)) for a, b in clamp_forbidden_zones)
        self.keepouts: List[Zone] = sorted(
            (float(a), float(b)) for a, b in seam_keepout_zones)
        self.punch_clamp = punch_clamp_system
        self.die_clamp = die_clamp_system
        self.end_margin_mm = float(end_margin_mm)
        self._cache: Dict[Tuple, Tuple[List[RailLayout],
                                       Optional[LayoutFailure]]] = {}

    # ---------------------------------------------------------- spans

    def required_span(self, bend_length_mm: float) -> Zone:
        """Bed interval the tooling must cover: bend centred, plus the
        end margin on both sides."""
        half = bend_length_mm / 2.0 + self.end_margin_mm
        return (-half, half)

    def _clamp(self, rail: str) -> str:
        return self.die_clamp if rail == "die" else self.punch_clamp

    def _pool(self, rail: str, profile_id: str) -> List[Segment]:
        clamp = self._clamp(rail)
        return sorted(
            (s for s in self.segments
             if not s.retired and s.kind == rail
             and s.profile_id == profile_id and s.clamp_system == clamp),
            key=lambda s: (-s.length_mm, s.id))

    # ---------------------------------------------------------- intervals

    @staticmethod
    def _cut(intervals: List[Zone], c0: float, c1: float) -> List[Zone]:
        """Remove the open interval (c0, c1) from every closed interval."""
        out: List[Zone] = []
        for a, b in intervals:
            if c1 <= a or c0 >= b:
                out.append((a, b))
                continue
            if a <= c0:
                out.append((a, min(c0, b)))
            if c1 <= b:
                out.append((max(c1, a), b))
        return [(a, b) for a, b in out if b >= a - EPS]

    def _offsets(self, total: float, seams_rel: Sequence[float],
                 bc0: float, bc1: float) -> List[float]:
        """Feasible assembly start positions x0, discretised.

        The assembly [x0, x0+total] must cover [bc0, bc1], stay on the bed,
        avoid the clamp forbidden zones, and keep every seam (given
        relative to x0) out of the keep-out zones.
        """
        half_bed = self.bed_width / 2.0
        lo = max(bc1 - total, -half_bed)
        hi = min(bc0, half_bed - total)
        if lo > hi + EPS:
            return []
        intervals: List[Zone] = [(lo, hi)]
        for f0, f1 in self.forbidden:
            intervals = self._cut(intervals, f0 - total, f1)
        for s in seams_rel:
            for z0, z1 in self.keepouts:
                intervals = self._cut(intervals, z0 - s, z1 - s)
        pref = (bc0 + bc1 - total) / 2.0   # centred on the required span
        cand = set()
        for a, b in intervals:
            cand.add(round(a, 6))
            cand.add(round(b, 6))
            cand.add(round(min(max(pref, a), b), 6))
        ordered = sorted(cand)
        if len(ordered) > MAX_OFFSETS:
            keep = MAX_OFFSETS // 2
            ordered = ordered[:keep] + ordered[-keep:]
        return ordered

    # ---------------------------------------------------------- assemblies

    def _enumerate(self, pool: Sequence[Segment], required: float
                   ) -> List[Tuple[Segment, ...]]:
        """Ordered segment trains whose total length reaches *required*.

        Handedness: a 'left' segment may only be first, a 'right' segment
        only last, 'any' anywhere (a single-piece train is unconstrained).
        """
        results: List[Tuple[Segment, ...]] = []
        nodes = [0]

        def dfs(chosen: List[Segment], used: List[bool], total: float):
            if len(results) >= MAX_ASSEMBLIES or nodes[0] > MAX_ASSEMBLY_NODES:
                return
            nodes[0] += 1
            if total >= required - EPS:
                results.append(tuple(chosen))
                return
            for i, s in enumerate(pool):
                if used[i]:
                    continue
                new_total = total + s.length_mm
                reaches = new_total >= required - EPS
                if not chosen:
                    ok = True if reaches else s.handedness in ("any", "left")
                elif reaches:
                    ok = s.handedness in ("any", "right")
                else:
                    ok = s.handedness == "any"
                if not ok:
                    continue
                used[i] = True
                dfs(chosen + [s], used, new_total)
                used[i] = False

        dfs([], [False] * len(pool), 0.0)
        uniq = sorted(
            set(results),
            key=lambda a: (len(a),
                           round(sum(s.length_mm for s in a), 3),
                           tuple(s.id for s in a)))
        return uniq

    # ---------------------------------------------------------- rail layouts

    def rail_layouts(self, rail: str, profile_id: str,
                     integral_length_mm: float, bend_length_mm: float
                     ) -> Tuple[List[RailLayout], Optional[LayoutFailure]]:
        """All candidate layouts for one rail, best first; or the failure."""
        key = (rail, profile_id, round(integral_length_mm, 3),
               round(bend_length_mm, 3))
        if key in self._cache:
            return self._cache[key]
        result = self._rail_layouts(rail, profile_id, integral_length_mm,
                                    bend_length_mm)
        self._cache[key] = result
        return result

    def _rail_layouts(self, rail: str, profile_id: str,
                      integral_length: float, bend_length: float
                      ) -> Tuple[List[RailLayout], Optional[LayoutFailure]]:
        bc0, bc1 = self.required_span(bend_length)
        bend_span = (-bend_length / 2.0, bend_length / 2.0)
        required = bc1 - bc0
        candidates: List[RailLayout] = []
        pre_keepout_feasible = False
        totals: List[float] = []

        # 1) integral catalog tool
        if integral_length + EPS >= required:
            totals.append(integral_length)
            offsets = self._offsets(integral_length, [], bc0, bc1)
            if offsets:
                pre_keepout_feasible = True
            for x0 in offsets:
                candidates.append(RailLayout(
                    rail, profile_id, True,
                    [Piece(profile_id, "integral", profile_id,
                           x0, x0 + integral_length)],
                    [], x0, (x0, x0 + integral_length),
                    (bc0, bc1), bend_span))

        # 2) segment trains
        pool = self._pool(rail, profile_id)
        assemblies = self._enumerate(pool, required)
        for train in assemblies:
            total = sum(s.length_mm for s in train)
            totals.append(total)
            seams_rel: List[float] = []
            acc = 0.0
            for s in train[:-1]:
                acc += s.length_mm
                seams_rel.append(acc)
            offsets = self._offsets(total, seams_rel, bc0, bc1)
            if offsets:
                pre_keepout_feasible = True
            for x0 in offsets:
                pieces: List[Piece] = []
                x = x0
                for s in train:
                    pieces.append(Piece(s.id, "segment", profile_id,
                                        x, x + s.length_mm, s.handedness))
                    x += s.length_mm
                candidates.append(RailLayout(
                    rail, profile_id, False, pieces,
                    [x0 + p for p in seams_rel], x0, (x0, x0 + total),
                    (bc0, bc1), bend_span))

        def centredness(L: RailLayout) -> float:
            total = L.covered[1] - L.covered[0]
            pref = (L.required[0] + L.required[1] - total) / 2.0
            return abs(L.offset - pref)

        candidates.sort(key=lambda L: (
            not L.integral, len(L.pieces),
            round(L.covered[1] - L.covered[0], 3),
            tuple(p.ref for p in L.pieces),
            round(centredness(L), 3), round(L.offset, 3)))
        candidates = candidates[:MAX_RAIL_CANDIDATES]
        if candidates:
            return candidates, None
        return [], self._diagnose(rail, profile_id, pool, integral_length,
                                  bc0, bc1, totals, pre_keepout_feasible)

    # ---------------------------------------------------------- diagnostics

    def _diagnose(self, rail: str, profile_id: str,
                  pool: Sequence[Segment], integral_length: float,
                  bc0: float, bc1: float, totals: Sequence[float],
                  pre_keepout_feasible: bool) -> LayoutFailure:
        required = bc1 - bc0
        seg_total = sum(s.length_mm for s in pool)
        available = max(seg_total, integral_length)
        if available + EPS < required:
            return LayoutFailure(
                "segment_shortage", profile_id, required, available, None,
                f"profile {profile_id}: required span {required:g} mm "
                f"exceeds inventory (integral {integral_length:g} mm, "
                f"segments {seg_total:g} mm); short "
                f"{required - available:g} mm")
        if required > self.bed_width + EPS:
            return LayoutFailure(
                "bed_width", profile_id, required, available,
                (-self.bed_width / 2.0, self.bed_width / 2.0),
                f"required span {required:g} mm exceeds the bed width "
                f"{self.bed_width:g} mm")
        max_total = max(totals) if totals else integral_length
        hull = (bc1 - max_total, bc0 + max_total)
        for f0, f1 in self.forbidden:
            if f0 < bc1 - EPS and f1 > bc0 + EPS:
                return LayoutFailure(
                    "clamp_forbidden", profile_id, required, available,
                    (f0, f1),
                    f"clamp forbidden zone [{f0:g}, {f1:g}] blocks the "
                    f"required span [{bc0:g}, {bc1:g}]")
        if pre_keepout_feasible:
            for z0, z1 in self.keepouts:
                if z0 < hull[1] - EPS and z1 > hull[0] + EPS:
                    return LayoutFailure(
                        "seam_keepout", profile_id, required, available,
                        (z0, z1),
                        f"every placement puts a seam inside the keep-out "
                        f"zone [{z0:g}, {z1:g}]")
        for f0, f1 in self.forbidden:
            if f0 < hull[1] - EPS and f1 > hull[0] + EPS:
                return LayoutFailure(
                    "clamp_forbidden", profile_id, required, available,
                    (f0, f1),
                    f"clamp forbidden zone [{f0:g}, {f1:g}] blocks every "
                    f"placement of a {required:g} mm span")
        return LayoutFailure(
            "no_placement", profile_id, required, available, (bc0, bc1),
            f"no placement of a {required:g} mm span for profile "
            f"{profile_id} satisfies the bed constraints")


# ---------------------------------------------------------------- sequencing

def _refs(layout: RailLayout) -> List[str]:
    return [p.ref for p in layout.pieces]


def _transition_cost(a: StepLayout, b: StepLayout) -> Tuple[int, int, int, float]:
    """(-reused, added, removed, lateral shift) from layout a to b."""
    reused = added = removed = 0
    shift = 0.0
    for rail in ("die", "punch"):
        la, lb = getattr(a, rail), getattr(b, rail)
        ra, rb = set(_refs(la)), set(_refs(lb))
        common = len(ra & rb)
        reused += common
        added += len(rb) - common
        removed += len(ra) - common
        if common:
            shift += abs(lb.offset - la.offset)
    return (-reused, added, removed, shift)


def _initial_cost(c: StepLayout) -> Tuple[int, int, int, float]:
    pieces = len(c.die.pieces) + len(c.punch.pieces)
    return (0, pieces, 0, 0.0)


def _add(c1: Tuple, c2: Tuple) -> Tuple:
    return (c1[0] + c2[0], c1[1] + c2[1], c1[2] + c2[2], c1[3] + c2[3])


def choose_step_layouts(per_step: Sequence[Tuple[List[RailLayout],
                                                 List[RailLayout]]]
                        ) -> Optional[List[StepLayout]]:
    """Pick one (die, punch) layout per step.

    Adjacent steps prefer reusing mounted segments, then minimise mounts,
    removals and lateral shift — lexicographic and stable, so the result
    is deterministic.
    """
    combos: List[List[StepLayout]] = []
    for die_cands, punch_cands in per_step:
        if not die_cands or not punch_cands:
            return None
        step_combos = [StepLayout(d, p)
                       for d in die_cands for p in punch_cands]
        combos.append(step_combos[:MAX_STEP_CANDIDATES])
    if not combos:
        return []

    # dp rows: (cost, combo_index, parent_index)
    rows: List[List[Tuple[Tuple, int, int]]] = [
        [(_initial_cost(c), j, -1) for j, c in enumerate(combos[0])]]
    for i in range(1, len(combos)):
        cur = []
        for j, c in enumerate(combos[i]):
            best_cost, best_k = None, -1
            for k, (cost, _, _) in enumerate(rows[i - 1]):
                t = _add(cost, _transition_cost(combos[i - 1][k], c))
                if best_cost is None or t < best_cost:
                    best_cost, best_k = t, k
            cur.append((best_cost, j, best_k))
        rows.append(cur)
    best = min(range(len(rows[-1])), key=lambda k: rows[-1][k][0])
    chosen: List[StepLayout] = []
    node = rows[-1][best]
    for i in range(len(combos) - 1, -1, -1):
        _, j, parent = node
        chosen.append(combos[i][j])
        if i > 0:
            node = rows[i - 1][parent]
    return list(reversed(chosen))


def tool_changes(prev: Optional[StepLayout], cur: StepLayout) -> List[dict]:
    """Tool-change actions turning the previous setup into *cur*.

    Kept pieces stay mounted (a 'shift' action is emitted when the rail
    moved laterally); everything else is mounted or removed.
    """
    actions: List[dict] = []
    for rail in ("die", "punch"):
        cur_layout = getattr(cur, rail)
        prev_layout = getattr(prev, rail) if prev is not None else None
        prev_refs = _refs(prev_layout) if prev_layout else []
        cur_refs = _refs(cur_layout)
        removed = [r for r in prev_refs if r not in cur_refs]
        kept = [r for r in cur_refs if r in prev_refs]
        for r in removed:
            actions.append({"rail": rail, "action": "remove", "ref": r})
        if kept and prev_layout is not None and \
                abs(prev_layout.offset - cur_layout.offset) > 1e-6:
            actions.append({"rail": rail, "action": "shift",
                            "delta_mm": round(cur_layout.offset
                                              - prev_layout.offset, 3)})
        for p in cur_layout.pieces:
            actions.append({
                "rail": rail,
                "action": "keep" if p.ref in kept else "mount",
                "ref": p.ref,
                "x0_mm": round(p.x0, 3), "x1_mm": round(p.x1, 3)})
    rank = {"remove": 0, "shift": 1, "mount": 2, "keep": 3}
    actions.sort(key=lambda a: (a["rail"], rank[a["action"]], a["ref"]))
    return actions


# ---------------------------------------------------------------- manual check

def validate_manual_layout(solver: LayoutSolver, *, bend_length_mm: float,
                           die_profile: str, punch_profile: str,
                           die_pieces: Sequence[dict],
                           punch_pieces: Sequence[dict],
                           segments_by_id: Dict[str, Segment],
                           candidate_dies: Sequence[str],
                           candidate_punches: Sequence[str]
                           ) -> Tuple[List[dict], dict]:
    """Validate one technician-specified layout against the layout rules.

    Returns (violations, rail_info); each violation is {code, detail}.
    """
    violations: List[dict] = []

    def vio(code: str, detail: str):
        violations.append({"code": code, "detail": detail})

    if die_profile not in candidate_dies:
        vio("profile_not_candidate",
            f"die profile {die_profile} is not among the part's candidate "
            f"dies {sorted(candidate_dies)}")
    if punch_profile not in candidate_punches:
        vio("profile_not_candidate",
            f"punch profile {punch_profile} is not among the part's "
            f"candidate punches {sorted(candidate_punches)}")

    seen: set = set()
    info: Dict[str, dict] = {}
    for rail, profile, pieces in (("die", die_profile, die_pieces),
                                  ("punch", punch_profile, punch_pieces)):
        rv, rail_info = _validate_rail(
            solver, rail, profile, pieces, bend_length_mm,
            segments_by_id, seen)
        violations.extend(rv)
        if rail_info is not None:
            info[rail] = rail_info
    return violations, info


def _validate_rail(solver: LayoutSolver, rail: str, profile: str,
                   pieces: Sequence[dict], bend_length_mm: float,
                   segments_by_id: Dict[str, Segment], seen: set
                   ) -> Tuple[List[dict], Optional[dict]]:
    out: List[dict] = []

    def vio(code: str, detail: str):
        out.append({"code": code, "detail": detail})

    if not pieces:
        vio("coverage_short", f"{rail} rail has no segments at all")
        return out, None

    clamp = solver._clamp(rail)
    placed: List[Tuple[float, Segment]] = []
    identity_ok = True
    for p in pieces:
        sid = p["segment_id"]
        s = segments_by_id.get(sid)
        if s is None:
            vio("unknown_segment", f"segment {sid} is not in the catalog")
            identity_ok = False
            continue
        if sid in seen:
            vio("duplicate_segment",
                f"physical segment {sid} is placed twice in the same step")
            identity_ok = False
        seen.add(sid)
        if s.retired:
            vio("segment_retired", f"segment {sid} is retired (停用)")
            identity_ok = False
        if s.kind != rail:
            vio("kind_mismatch",
                f"segment {sid} is a {s.kind} segment and cannot mount on "
                f"the {rail} rail")
            identity_ok = False
        if s.profile_id != profile:
            vio("profile_mismatch",
                f"segment {sid} has profile {s.profile_id}, incompatible "
                f"with the {rail} profile {profile}")
            identity_ok = False
        if s.clamp_system != clamp:
            vio("clamp_incompatible",
                f"segment {sid} uses clamp system {s.clamp_system}, the "
                f"machine {rail} rail takes {clamp}")
            identity_ok = False
        placed.append((float(p["x0_mm"]), s))
    if not identity_ok:
        return out, None

    placed.sort(key=lambda t: t[0])
    half_bed = solver.bed_width / 2.0
    n = len(placed)
    spans: List[Tuple[float, float, Segment]] = []
    for i, (x0, s) in enumerate(placed):
        x1 = x0 + s.length_mm
        spans.append((x0, x1, s))
        if n > 1:
            if i == 0 and s.handedness not in ("any", "left"):
                vio("handedness",
                    f"segment {s.id} is {s.handedness}-handed and cannot "
                    f"start the train")
            elif i == n - 1 and s.handedness not in ("any", "right"):
                vio("handedness",
                    f"segment {s.id} is {s.handedness}-handed and cannot "
                    f"end the train")
            elif 0 < i < n - 1 and s.handedness != "any":
                vio("handedness",
                    f"segment {s.id} is {s.handedness}-handed and cannot "
                    f"sit inside the train")
        if x0 < -half_bed - EPS or x1 > half_bed + EPS:
            vio("out_of_bed",
                f"segment {s.id} spans [{x0:g}, {x1:g}], outside the bed "
                f"[{-half_bed:g}, {half_bed:g}]")
        for f0, f1 in solver.forbidden:
            if x0 < f1 - EPS and x1 > f0 + EPS:
                vio("forbidden_zone",
                    f"segment {s.id} spans [{x0:g}, {x1:g}] and overlaps "
                    f"the clamp forbidden zone [{f0:g}, {f1:g}]")

    seams: List[float] = []
    for (x0, x1, s), (nx0, _, ns) in zip(spans, spans[1:]):
        gap = nx0 - x1
        if abs(gap) > CONTIGUITY_TOL:
            vio("not_contiguous",
                f"gap/overlap of {gap:g} mm between segments {s.id} and "
                f"{ns.id}")
        seam = (x1 + nx0) / 2.0
        seams.append(seam)
        for z0, z1 in solver.keepouts:
            if z0 + EPS < seam < z1 - EPS:
                vio("seam_keepout",
                    f"seam at {seam:g} mm falls inside the keep-out zone "
                    f"[{z0:g}, {z1:g}]")

    cov0, cov1 = spans[0][0], spans[-1][1]
    bc0, bc1 = solver.required_span(bend_length_mm)
    if cov0 > bc0 + EPS or cov1 < bc1 - EPS:
        vio("coverage_short",
            f"{rail} rail covers [{cov0:g}, {cov1:g}] but the bend line "
            f"plus end margins requires [{bc0:g}, {bc1:g}]")
    info = {"covered_mm": [round(cov0, 3), round(cov1, 3)],
            "seams_mm": [round(s, 3) for s in seams]}
    return out, info
