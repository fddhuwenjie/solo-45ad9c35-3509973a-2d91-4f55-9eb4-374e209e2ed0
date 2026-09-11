"""Machine setup simulation and collision / backgauge verification.

Machine-side-view coordinates (s, z):

    s>0        behind the V centre, toward the rear frame / backgauge
    s<0        toward the operator
    z=0        die shoulder plane (sheet rests here before the stroke)
    z>0        toward the ram / punch

The intrinsic folded part (``partmodel``) is placed so the bend axis lies at
s=0 and its largest nearly-horizontal leg lies on the die shoulders; the
other end of that leg is pushed against the backgauge at +s.  Two workpiece
orientations are tried: direct and 180 deg flipped about the bend axis.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import geometry as G
from .models import Die, PressBrake, Punch
from .partmodel import Bend, Part

Seg2 = Tuple[Tuple[float, float], Tuple[float, float]]


# ---------------------------------------------------------------- setup

@dataclass
class SetupResult:
    flip: bool
    orientation: str
    feed_direction: str
    support_face: int
    axis_q: G.Vec3
    axis_u: G.Vec3
    translate: G.Vec3
    flip_rot: Optional[Tuple[G.Vec3, float]] = None
    section: List[Tuple[float, float, float, float, int]] = field(
        default_factory=list)
    gauge: Optional["GaugeInfo"] = None
    checks: List["Check"] = field(default_factory=list)


@dataclass
class GaugeInfo:
    distance: float
    contact: Tuple[float, float]
    contact_z: float
    edge_bend_id: Optional[str]
    occluded: bool
    occluder_face: Optional[int]


@dataclass
class Check:
    code: str
    ok: bool
    detail: str
    value: Optional[float] = None
    limit: Optional[float] = None




def n_axis(u: G.Vec3) -> G.Vec3:
    n = G.vunit(G.vcross(u, [0.0, 0.0, 1.0]))
    if G.vnorm(n) < 1e-6:
        n = [1.0, 0.0, 0.0]
    return n


def _rotate_vec(v: G.Vec3, axis: G.Vec3, theta: float) -> G.Vec3:
    A = G.axis_angle_matrix(G.vunit(axis), theta)
    return G.mdot(A, v)


def simulate_setup(part: Part, bend: Bend, *, flip: bool,
                   die: Die, punch: Punch, machine: PressBrake,
                   thickness: float, applied_ids: Sequence[str],
                   axis_q: Optional[G.Vec3] = None,
                   axis_u: Optional[G.Vec3] = None
                   ) -> SetupResult:
    """Place the folded part on the press for bending *bend* next.

    Machine side view (s,z): s>0 toward the rear frame/backgauge, z>0 toward
    the ram.  The stationary support leg is laid on the die shoulders with
    its free end toward +s (the gauge); the moving flange rises toward +z.
    ``flip`` turns the part 180 deg about the bend axis, which swaps which
    physical leg is at the rear.
    """
    if axis_q is None:
        moved = part.moving_face_ids(bend)
        axis_q, axis_u = part.current_axis(bend, moved)
    u = G.vunit(axis_u)
    n = n_axis(u)
    movedset = set(part.moving_face_ids(bend))

    # intrinsic cross section in (s_along_n, z); choose the stationary face
    # with the largest extent along the axis-normal as the support leg
    intr = part.section(axis_q, u, samples=5)
    best_face, best_extent = None, -1.0
    for fi, f in enumerate(part.faces):
        if fi in movedset:
            continue
        pts3 = [f.point3d(p2) for p2 in f.poly2d]
        ss = [G.vdot(G.vsub(p, axis_q), n) for p in pts3]
        extent = max(ss) - min(ss)
        if extent > best_extent:
            best_face, best_extent = fi, extent
    checks: List[Check] = []
    if best_face is None:
        checks.append(Check("support_leg", False,
                            "no stationary face at the bend axis to rest on "
                            "the die (floating bend)"))
        return SetupResult(flip, "undefined", "undefined", -1,
                           axis_q, u, [0, 0, 0], None, [], None, checks)

    # Map a 3D point to intrinsic (s_i, z_i) about the axis, then orient:
    # support free end must point to +s; flange must rise toward +z.
    sup_face = part.faces[best_face]
    sup_s = [G.vdot(G.vsub(sup_face.point3d(p2), axis_q), n)
             for p2 in sup_face.poly2d]
    support_sign = 1.0 if max(sup_s) >= abs(min(sup_s)) else -1.0

    def to_machine(p: G.Vec3) -> Tuple[float, float]:
        w = G.vsub(p, axis_q)
        si = G.vdot(w, n)
        zi = w[2]
        # orient support leg toward +s
        sm = si * support_sign
        zm = zi
        if flip:
            sm = -sm
        return sm, zm

    sec_m: List[Tuple[float, float, float, float, int]] = []
    for s1, z1, s2, z2, fi in intr:
        A = to_machine(G.vadd(axis_q, G.vadd(G.vmul(n, s1),
                                             [0.0, 0.0, z1])))
        B = to_machine(G.vadd(axis_q, G.vadd(G.vmul(n, s2),
                                             [0.0, 0.0, z2])))
        sec_m.append((A[0], A[1], B[0], B[1], fi))

    psi = 0.0
    trans = G.vmul(axis_q, -1.0)

    # ------------------------------------------------------------- backgauge
    gauge = _evaluate_backgauge(part, bend, best_face, to_machine, sec_m,
                                die, machine, movedset)

    # ------------------------------------------------------------- obstacles
    checks.extend(_collision_checks(part, bend, to_machine, sec_m, die,
                                    punch, machine, movedset, best_face,
                                    thickness, flip))
    if gauge is not None:
        if machine.backgauge_min_mm - 1e-6 <= gauge.distance <= \
                machine.backgauge_max_mm + 1e-6:
            checks.append(Check("backgauge_reach", True,
                                f"gauge distance {gauge.distance:.1f} mm in "
                                f"[{machine.backgauge_min_mm:g}, "
                                f"{machine.backgauge_max_mm:g}]",
                                gauge.distance, machine.backgauge_max_mm))
        else:
            checks.append(Check("backgauge_reach", False,
                                f"gauge distance {gauge.distance:.1f} mm "
                                f"outside [{machine.backgauge_min_mm:g}, "
                                f"{machine.backgauge_max_mm:g}]",
                                gauge.distance, machine.backgauge_max_mm))
        zlim = machine.backgauge_height_tolerance_mm
        if abs(gauge.contact_z) <= zlim + 1e-6:
            checks.append(Check("backgauge_edge_flat", True,
                                f"contact edge at z={gauge.contact_z:.2f} "
                                f"mm on the die plane",
                                abs(gauge.contact_z), zlim))
        else:
            checks.append(Check("backgauge_edge_flat", False,
                                f"contact edge at z={gauge.contact_z:.1f} "
                                f"mm, off the die plane by more than "
                                f"{zlim:g} mm (no flat reference)",
                                abs(gauge.contact_z), zlim))
        if gauge.occluded:
            checks.append(Check("backgauge_occlusion", False,
                                f"formed flange of face "
                                f"#{gauge.occluder_face} blocks the path "
                                f"from the gauge finger to the reference "
                                f"edge"))
        else:
            checks.append(Check("backgauge_occlusion", True,
                                "reference edge visible to the gauge finger"))
    else:
        checks.append(Check("backgauge_reach", False,
                            "no support edge reaches the rear (+s) gauge in "
                            "this orientation; flip the part"))

    orient = ("axis-aligned, flange down" if not flip
              else "180° flipped about bend axis")
    feed = ("operator pushes toward rear (+s)" if not flip
            else "operator pushes flipped part toward rear (+s)")
    return SetupResult(flip=flip, orientation=orient, feed_direction=feed,
                       support_face=best_face, axis_q=axis_q, axis_u=u,
                       translate=trans,
                       flip_rot=(u, psi) if abs(psi) > 1e-9 else None,
                       section=sec_m, gauge=gauge, checks=checks)


# ---------------------------------------------------------------- backgauge

def _evaluate_backgauge(part: Part, bend: Bend, support_face: int,
                        to_machine, sec_m, die: Die, machine: PressBrake,
                        movedset) -> Optional[GaugeInfo]:
    """The gauge contacts the furthest +s vertex of the support leg."""
    f = part.faces[support_face]
    cand = []
    for p2 in f.poly2d:
        s, z = to_machine(f.point3d(p2))
        cand.append((s, z))
    s_far, z_far = max(cand)
    if s_far <= 0:
        return None
    # which bend (if any) forms that far edge? edge of support face whose
    # machine-s is maximal; identify with a bend if its blank edge coincides
    edge_bend = None
    n3 = len(f.poly2d)
    far_edges = []
    for i in range(n3):
        a = to_machine(f.point3d(f.poly2d[i]))
        bb = to_machine(f.point3d(f.poly2d[(i + 1) % n3]))
        if a[0] > s_far - 1e-5 and bb[0] > s_far - 1e-5:
            far_edges.append((i, abs(a[1] - bb[1])))
    if far_edges:
        ei = max(far_edges, key=lambda x: x[1])[0]
        A2, B2 = f.poly2d[ei], f.poly2d[(ei + 1) % n3]
        for bid, b in part.bends.items():
            d = G.distance_point_seg(A2, b.p0, b.p1) + \
                G.distance_point_seg(B2, b.p0, b.p1)
            if d < 1e-4:
                edge_bend = bid
    # occlusion: any other section segment crossing s=s_far with |z| above
    # the sheet band, between gauge finger and die
    occluded, occluder = False, None
    for s1, z1, s2, z2, fi in sec_m:
        if fi == support_face:
            continue
        lo, hi = sorted((s1, s2))
        if lo + 1e-6 < s_far and s_far < hi - 1e-6:
            zlo, zhi = sorted((z1, z2))
            if zhi > part.thickness * 1.5:
                occluded, occluder = True, fi
                break
    return GaugeInfo(distance=s_far, contact=(s_far, z_far),
                     contact_z=z_far, edge_bend_id=edge_bend,
                     occluded=occluded, occluder_face=occluder)


# ---------------------------------------------------------------- collisions

def _tool_polygons(die: Die, punch: Punch, machine: PressBrake
                   ) -> Dict[str, List[Tuple[float, float]]]:
    """Side-view obstacle polygons in machine (s,z) coords.

    Goose-neck punches (``goose_neck_open_side`` = +/-1) have an asymmetric
    body: the body sits on the *closed* side so that a tall rising flange on
    the open side clears it.  Symmetric punches keep the body centred.
    """
    v_half = die.v_width / 2.0
    floor = -v_half / math.tan(math.radians(die.v_angle_deg) / 2)
    dw = max(die.v_width * 3.0, die.die_height * 1.2)
    die_poly = [(-dw, -die.die_height - 30), (dw, -die.die_height - 30),
                (dw, 0.0), (v_half, 0.0), (0.0, floor),
                (-v_half, 0.0)]
    ph = punch.punch_height
    nose = punch.nose_width / 2
    body = punch.body_width / 2 if punch.body_width else nose * 2
    rh = punch.relief_height or ph * 0.35
    open_side = punch.goose_neck_open_side
    if open_side == 0:
        b_in, b_out = -body, body
        n_in, n_out = -nose, nose
    elif open_side > 0:
        # open on +s: body occupies -body .. +nose
        b_in, b_out = -body, nose
        n_in, n_out = -nose, nose
    else:
        b_in, b_out = -nose, body
        n_in, n_out = -nose, nose
    punch_poly = [
        (n_in, 0.0), (n_in, rh), (b_in, rh), (b_in, ph),
        (b_out, ph), (b_out, rh), (n_out, rh), (n_out, 0.0)]
    body_poly = [(b_in, rh), (b_out, rh), (b_out, ph), (b_in, ph)]
    td = machine.throat_depth_mm
    th = machine.frame_clearance_height_mm
    frame_poly = [(td, -50.0), (td + 80.0, -50.0),
                  (td + 80.0, th + 100), (td, th + 100)]
    return {"die": die_poly, "punch": punch_poly,
            "punch_body": body_poly, "frame": frame_poly}




def _poly_contains(poly, p) -> bool:
    return G.point_in_poly(p, poly)


def _collision_checks(part: Part, bend: Bend, to_machine, sec_m,
                      die: Die, punch: Punch, machine: PressBrake,
                      movedset, support_face: int, thickness: float,
                      flip: bool) -> List[Check]:
    """Collisions at bottom of stroke.

    The moving flange is allowed inside the V corridor and along the open
    side of a goose-neck punch; a symmetric punch only protects it up to the
    relief height.  Already-formed flanges are tested against the full punch
    outline — a tall formed flange needs a tall, correctly oriented
    goose-neck, otherwise this check fails (that is the classic collision
    that decides bend order).
    """
    out: List[Check] = []
    obs = _tool_polygons(die, punch, machine)
    half_t = thickness / 2.0 + 0.8
    v_half = die.v_width / 2.0
    nose = punch.nose_width / 2.0
    body = punch.body_width / 2.0 if punch.body_width else max(nose * 2, 10.0)
    rh = punch.relief_height or punch.punch_height * 0.35
    open_side = punch.goose_neck_open_side

    def seg_poly_clearance(seg, poly) -> float:
        (s1, z1), (s2, z2) = seg
        for i in range(len(poly)):
            c, d = poly[i], poly[(i + 1) % len(poly)]
            if G.seg_intersect_open((s1, z1), (s2, z2), c, d) is not None:
                return 0.0
        pts = [(s1 + (s2 - s1) * m, z1 + (z2 - z1) * m)
               for m in (0.0, 0.25, 0.5, 0.75, 1.0)]
        if any(G.point_in_poly(p, poly) for p in pts):
            return 0.0
        return min(min(G.distance_point_seg(p, poly[i],
                                            poly[(i + 1) % len(poly)])
                       for i in range(len(poly))) for p in pts)

    dw = max(die.v_width * 3.0, die.die_height * 1.2)
    db = -die.die_height - 30.0
    die_blocks = [
        [(-dw, db), (-v_half, db), (-v_half, 0.0), (-dw, 0.0)],
        [(v_half, db), (dw, db), (dw, 0.0), (v_half, 0.0)],
    ]

    def seg_die_clearance(seg) -> float:
        return min(seg_poly_clearance(seg, b) for b in die_blocks)

    # machine-s side on which the goose-neck is open after the part flip
    open_machine_side = open_side * (-1 if flip else 1)
    # A face whose chord lies on the die plane (z ~ 0) is flat stock that has
    # not been lifted by any applied bend: it is feeding/supporting material,
    # never an upright formed flange.
    frame_clash, body_clash, die_clash = [], [], []
    for s1, z1, s2, z2, fi in sec_m:
        if fi == support_face:
            continue
        seg = ((s1, z1), (s2, z2))
        zmax = max(abs(z1), abs(z2))
        flat = zmax <= max(half_t, thickness)
        moving = fi in movedset
        if flat:
            # flat stock on the die plane: only the frame/throat can clash
            if seg_poly_clearance(seg, obs["frame"]) < half_t:
                frame_clash.append((fi, "flat stock"))
            continue
        tag = "moving flange" if moving else "formed flange"
        if seg_poly_clearance(seg, obs["frame"]) < half_t:
            frame_clash.append((fi, tag))
        pts = [(s1 + (s2 - s1) * m, z1 + (z2 - z1) * m)
               for m in (0.0, 0.5, 1.0)]
        mean_s = (s1 + s2) / 2.0
        if moving:
            def point_free(s, z):
                if z <= half_t and abs(s) <= v_half + half_t:
                    return True  # V corridor / below die plane
                # the flange rises hugging the punch tip straight wall,
                # which is open to its full height within the nose width
                if abs(s) <= nose + half_t:
                    return True
                # relief band between nose and body
                if z <= rh + half_t and abs(s) <= body + half_t:
                    return True
                # goose-neck open side above the relief
                if open_machine_side != 0 and \
                        s * open_machine_side >= nose - half_t:
                    return True
                return False
            allowed = all(point_free(s, z) for s, z in pts)
            if not allowed:
                if seg_poly_clearance(seg, obs["punch_body"]) < half_t:
                    body_clash.append((fi, tag))
                if seg_die_clearance(seg) < half_t:
                    die_clash.append((fi, tag, "die"))
        else:
            # Already-formed flange.  A symmetric punch only shelters it to
            # the nose width/relief height; a correctly oriented goose-neck
            # shelters it on the open side to full punch height.
            def formed_free(s, z):
                if z <= half_t and abs(s) <= v_half + half_t:
                    return True
                if open_machine_side != 0 and \
                        s * open_machine_side >= nose - half_t:
                    return z <= punch.punch_height + half_t
                if abs(s) <= nose + half_t:
                    return z <= rh + half_t
                return z <= rh + half_t and abs(s) <= body + half_t
            if not all(formed_free(s, z) for s, z in pts):
                if seg_poly_clearance(seg, obs["punch"]) < half_t:
                    body_clash.append((fi, tag))
            if seg_die_clearance(seg) < half_t:
                die_clash.append((fi, tag, "die"))

    # self-collision between different folded faces (away from the hinge)
    self_clash = []
    msegs = [((s1, z1), (s2, z2), fi)
             for s1, z1, s2, z2, fi in sec_m]
    for i in range(len(msegs)):
        for j in range(i + 1, len(msegs)):
            A, B, fi = msegs[i]
            C, D, fj = msegs[j]
            if fi == fj:
                continue
            ip = G.seg_intersect_open(A, B, C, D)
            if ip is not None and (abs(ip[1]) > half_t or
                                   abs(ip[0]) > 1.0):
                self_clash.append((fi, fj))

    if frame_clash:
        out.append(Check("frame_collision", False,
                         "workpiece hits the rear frame / exceeds the throat "
                         f"depth {machine.throat_depth_mm:g} mm: "
                         f"{frame_clash}"))
    else:
        out.append(Check("frame_collision", True,
                         "workpiece clears the side frame and throat"))
    if body_clash or die_clash:
        out.append(Check("tool_collision", False,
                         "workpiece collides with tooling outside the "
                         f"relief/V: punch {body_clash}, die {die_clash}"))
    else:
        out.append(Check("tool_collision", True,
                         "workpiece clears punch and die"))
    if self_clash:
        out.append(Check("self_collision", False,
                         f"folded flanges intersect each other: {self_clash}"))
    else:
        out.append(Check("self_collision", True,
                         "no folded flange intersects another"))
    return out


# ---------------------------------------------------------------- swing sim

def swing_sections(part0: Part, bend: Bend, steps: int = 5
                   ) -> List[Part]:
    """Return part clones at intermediate fold fractions (ram descending).

    ``part0`` is the state just before this bend; clones are folded through
    fractions 1/steps..1 of the fold angle (static tooling check is done at
    full closure by the caller)."""
    stages = []
    total = bend.fold_angle
    for k in range(1, steps + 1):
        p = part0.clone()
        bend_clone = p.bends[bend.id]
        bend_clone.fold_angle = total * k / steps
        p.apply_fold(bend.id)
        stages.append(p)
    return stages
