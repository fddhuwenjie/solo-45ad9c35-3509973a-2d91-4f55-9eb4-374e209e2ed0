"""Sheet-metal geometry kernel.

The part is a set of planar *faces* (triangulated polygons), each carrying a
rigid transform (R, t) from its own flat blank frame to the current (partially
folded) world frame.  A fold rotates every face on the moving side of a bend
about the bend axis in its *current* position; previously formed flanges on
the moving side are carried rigidly.

All coordinates are sheet mid-surface coordinates; thickness is handled as
clearance by the machine/collision layer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

Vec3 = List[float]
Mat3 = List[List[float]]
EPS = 1e-7


# ---------------------------------------------------------------- vectors

def vadd(a: Vec3, b: Vec3) -> Vec3:
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def vsub(a: Vec3, b: Vec3) -> Vec3:
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def vmul(a: Vec3, s: float) -> Vec3:
    return [a[0] * s, a[1] * s, a[2] * s]


def vdot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vcross(a: Vec3, b: Vec3) -> Vec3:
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def vnorm(a: Vec3) -> float:
    return math.sqrt(vdot(a, a))


def vunit(a: Vec3) -> Vec3:
    n = vnorm(a)
    return [a[0] / n, a[1] / n, a[2] / n] if n > EPS else [0.0, 0.0, 0.0]


# ---------------------------------------------------------------- matrices

def I3() -> Mat3:
    return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def mdot(M: Mat3, v: Vec3) -> Vec3:
    return [
        M[0][0] * v[0] + M[0][1] * v[1] + M[0][2] * v[2],
        M[1][0] * v[0] + M[1][1] * v[1] + M[1][2] * v[2],
        M[2][0] * v[0] + M[2][1] * v[1] + M[2][2] * v[2],
    ]


def mmul(A: Mat3, B: Mat3) -> Mat3:
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def axis_angle_matrix(u: Vec3, theta: float) -> Mat3:
    """Rodrigues' formula."""
    c, s = math.cos(theta), math.sin(theta)
    x, y, z = u
    return [
        [c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
        [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
        [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)],
    ]


def rot_about(M: Mat3, t: Vec3, q: Vec3, u: Vec3, theta: float
              ) -> Tuple[Mat3, Vec3]:
    """Update rigid transform (M,t) after rotating the world by theta about
    axis (q, u): p' = q + A(p - q)."""
    A = axis_angle_matrix(u, theta)
    return mmul(A, M), vadd(q, mdot(A, vsub(t, q)))


# ---------------------------------------------------------------- polygon 2d

def signed_area(poly: Sequence[Tuple[float, float]]) -> float:
    a = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return 0.5 * a


def point_side(p: Tuple[float, float], a: Tuple[float, float],
               b: Tuple[float, float]) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def seg_intersect(p1: Tuple[float, float], p2: Tuple[float, float],
                  p3: Tuple[float, float], p4: Tuple[float, float]
                  ) -> Optional[Tuple[float, float]]:
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < EPS:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / den
    if -EPS < t < 1 + EPS and -EPS < u < 1 + EPS:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def distance_point_seg(p, a, b):
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < EPS:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def seg_intersect_open(p1, p2, p3, p4):
    """Like seg_intersect but only proper crossings (used by clipper)."""
    ip = seg_intersect(p1, p2, p3, p4)
    if ip is None:
        return None
    for p in (p1, p2):
        if abs(p[0] - ip[0]) <= EPS and abs(p[1] - ip[1]) <= EPS:
            return None
    return ip


def distance_poly_poly(A: Sequence[Tuple[float, float]],
                       B: Sequence[Tuple[float, float]]) -> float:
    """Minimum gap between two polygon outlines; 0 if they cross."""
    best = float("inf")
    for i in range(len(A)):
        a1, a2 = A[i], A[(i + 1) % len(A)]
        for j in range(len(B)):
            b1, b2 = B[j], B[(j + 1) % len(B)]
            if seg_intersect_open(a1, a2, b1, b2) is not None:
                return 0.0
            for p in (a1, a2):
                d = distance_point_seg(p, b1, b2)
                if d < best:
                    best = d
            for p in (b1, b2):
                d = distance_point_seg(p, a1, a2)
                if d < best:
                    best = d
    return best


def point_in_poly(p: Tuple[float, float],
                  poly: Sequence[Tuple[float, float]]) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (x2 - x1) * (y - y1) / (y2 - y1)
            if xint > x:
                inside = not inside
    return inside


def rect_poly(cx: float, cy: float, w: float, h: float
              ) -> List[Tuple[float, float]]:
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
            (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]


# ---------------------------------------------------------------- triangulate

def triangulate(poly: Sequence[Tuple[float, float]]
                ) -> List[Tuple[int, int, int]]:
    """Ear clipping; indices are local to *poly* (CCW expected)."""
    n = len(poly)
    if n < 3:
        return []
    if signed_area(poly) < 0:
        poly = list(reversed(poly))
    idx = list(range(n))
    tris: List[Tuple[int, int, int]] = []
    guard = 0
    while len(idx) > 2 and guard < 10 * n:
        guard += 1
        m = len(idx)
        ear_found = False
        for k in range(m):
            ia, ib, ic = idx[(k - 1) % m], idx[k], idx[(k + 1) % m]
            A, B, C = poly[ia], poly[ib], poly[ic]
            if point_side(B, A, C) <= EPS:
                continue
            ear = True
            for j in idx:
                if j in (ia, ib, ic):
                    continue
                if point_in_poly(poly[j], (A, B, C)):
                    ear = False
                    break
            if ear:
                tris.append((ia, ib, ic))
                del idx[k]
                ear_found = True
                break
        if not ear_found:  # degenerate: fan and let area filter drop junk
            for k in range(1, m - 1):
                tris.append((idx[0], idx[k], idx[k + 1]))
            break
    return tris


# ---------------------------------------------------------------- clipping

def clip_half_plane(poly: Sequence[Tuple[float, float]],
                    q1: Tuple[float, float], q2: Tuple[float, float],
                    positive: bool) -> List[Tuple[float, float]]:
    """Sutherland-Hodgman clip keeping cross>0 (or cross<=0); points on the
    line are kept in *both* halves so subsequent cuts share the boundary."""
    out: List[Tuple[float, float]] = []
    n = len(poly)

    def inside(v: float) -> bool:
        return v >= -EPS if positive else v <= EPS

    for i in range(n):
        s, e = poly[i], poly[(i + 1) % n]
        ds, de = point_side(s, q1, q2), point_side(e, q1, q2)
        sin_, ein_ = inside(ds), inside(de)
        on_s, on_e = abs(ds) <= EPS, abs(de) <= EPS
        if sin_ and not on_s:
            out.append(s)
        if on_s:
            out.append(s)
        # crossing with a strictly interior segment
        if not (on_s or on_e) and sin_ != ein_:
            ip = seg_intersect(s, e, q1, q2)
            if ip is not None:
                out.append(ip)
        if ein_ and not sin_ and not on_e:
            out.append(e)
    # remove consecutive duplicates (including wrap)
    ded: List[Tuple[float, float]] = []
    for p in out:
        if not ded or math.hypot(p[0] - ded[-1][0], p[1] - ded[-1][1]) > EPS:
            ded.append(p)
    if len(ded) >= 2 and math.hypot(ded[0][0] - ded[-1][0],
                                    ded[0][1] - ded[-1][1]) <= EPS:
        ded.pop()
    return ded


def split_polygon(poly: Sequence[Tuple[float, float]],
                  q1: Tuple[float, float], q2: Tuple[float, float]
                  ) -> List[List[Tuple[float, float]]]:
    side = [point_side(p, q1, q2) for p in poly]
    if not any(s > EPS for s in side) or not any(s < -EPS for s in side):
        return [list(poly)]
    return [
        p for p in (
            clip_half_plane(poly, q1, q2, True),
            clip_half_plane(poly, q1, q2, False),
        ) if len(p) >= 3
    ]


# ---------------------------------------------------------------- face model

@dataclass
class Face:
    """Planar polygon in its own flat blank frame plus current world pose."""

    poly2d: List[Tuple[float, float]]
    triangles: List[Tuple[int, int, int]]
    R: Mat3 = field(default_factory=I3)
    t: Vec3 = field(default_factory=lambda: [0.0, 0.0, 0.0])

    def point3d(self, local: Tuple[float, float]) -> Vec3:
        return vadd(mdot(self.R, [local[0], local[1], 0.0]), self.t)

    def normal(self) -> Vec3:
        return mdot(self.R, [0.0, 0.0, 1.0])
