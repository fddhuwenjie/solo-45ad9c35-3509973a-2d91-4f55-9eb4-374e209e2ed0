"""Part model: blank polygon + bend lines -> evolving set of planar faces.

The blank is cut by every bend line into constant-sign *cells* (faces).  Each
fold rotates the faces on the flange side of the bend about the *current* 3D
position of the bend axis; flanges folded earlier on the same side (nested
flanges / hems) are carried along rigidly.  The resulting intrinsic folded
geometry is independent of manufacturing sequence and is the single source
shared by the JSON process card and the SVG side views.
"""
from __future__ import annotations

import copy as _c
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import geometry as G

Pt2 = Tuple[float, float]


@dataclass
class Bend:
    id: str
    p0: Pt2
    p1: Pt2
    target_angle: float          # included angle between flanges, deg
    radius: float                # inside bend radius, mm
    flange_side: int             # +1: cross>0 side rises; -1: cross<0 rises
    fold_angle: float = 0.0      # press angle incl. overbend, deg
    axis_angle_to_grain: float = 0.0

    def length(self) -> float:
        return math.hypot(self.p1[0] - self.p0[0],
                          self.p1[1] - self.p0[1])

    def axis_dir_2d(self) -> Tuple[float, float]:
        d = G.vunit([self.p1[0] - self.p0[0],
                     self.p1[1] - self.p0[1], 0.0])
        return (d[0], d[1])


@dataclass
class FoldRecord:
    bend_id: str
    theta: float
    axis_p: G.Vec3          # world position of p0 when the fold was applied
    axis_u: G.Vec3          # world unit axis, oriented p0->p1
    moved: List[int]        # face indices carried by this fold


@dataclass
class Part:
    contour: List[Pt2]
    bends: Dict[str, Bend]
    thickness: float
    grain_angle_deg: float
    faces: List[G.Face] = field(default_factory=list)
    fold_history: List[FoldRecord] = field(default_factory=list)
    applied: List[str] = field(default_factory=list)

    # ------------------------------------------------------------- build

    @classmethod
    def build(cls, contour: Sequence[Pt2], bends: Sequence[Bend],
              thickness: float, grain_angle_deg: float = 0.0) -> "Part":
        poly = [tuple(p) for p in contour]
        if G.signed_area(poly) < 0:
            poly = list(reversed(poly))
        bdict: Dict[str, Bend] = {b.id: b for b in bends}

        cells: List[List[Pt2]] = [poly]
        for b in bends:
            nxt: List[List[Pt2]] = []
            for cell in cells:
                sides = [G.point_side(p, b.p0, b.p1) for p in cell]
                if all(v >= -1e-7 for v in sides) or \
                        all(v <= 1e-7 for v in sides):
                    nxt.append(cell)
                    continue
                nxt.extend(G.split_polygon(cell, b.p0, b.p1))
            cells = nxt

        faces: List[G.Face] = []
        for cell in cells:
            cc: List[Pt2] = []
            for p in cell:
                if not cc or math.hypot(p[0] - cc[-1][0],
                                        p[1] - cc[-1][1]) > 1e-6:
                    cc.append(p)
            if len(cc) >= 3 and abs(G.signed_area(cc)) > 1e-9:
                tris = G.triangulate(cc)
                if tris:
                    faces.append(G.Face(poly2d=cc, triangles=tris))

        part = cls(contour=poly, bends=bdict, thickness=thickness,
                   grain_angle_deg=grain_angle_deg % 180.0, faces=faces)
        for b in bends:
            ax = math.degrees(math.atan2(
                -(b.p1[0] - b.p0[0]), b.p1[1] - b.p0[1])) % 180.0
            b.axis_angle_to_grain = min(
                abs(ax - part.grain_angle_deg),
                180 - abs(ax - part.grain_angle_deg))
        return part

    def clone(self) -> "Part":
        return _c.deepcopy(self)

    # ------------------------------------------------------------- folds

    def centroid(self, f: G.Face) -> Pt2:
        x = sum(p[0] for p in f.poly2d) / len(f.poly2d)
        y = sum(p[1] for p in f.poly2d) / len(f.poly2d)
        return x, y

    def side(self, p: Pt2, b: Bend) -> int:
        v = G.point_side(p, b.p0, b.p1)
        return 1 if v > 1e-7 else (-1 if v < -1e-7 else 0)

    def moving_face_ids(self, b: Bend) -> List[int]:
        return [i for i, f in enumerate(self.faces)
                if self.side(self.centroid(f), b) == b.flange_side]

    def current_axis(self, b: Bend, moved: Sequence[int]) -> Tuple[G.Vec3, G.Vec3]:
        """World position/orientation of a bend axis just before folding it.

        Uses a stationary face adjacent to the axis; if none qualifies, falls
        back to the raw blank direction (axis already on z=0 for first fold).
        """
        movedset = set(moved)
        anchor: Optional[G.Face] = None
        best_d = 1e-3
        for i, f in enumerate(self.faces):
            if i in movedset:
                continue
            d = min(G.distance_point_seg(self.centroid(f), b.p0, b.p1),
                    min(G.distance_point_seg(p, b.p0, b.p1)
                        for p in f.poly2d))
            if d <= best_d:
                best_d = d
                anchor = f
        if anchor is None:
            # all faces move: use any moving face and accept blank-frame axis
            anchor = self.faces[moved[0]] if moved else None
        if anchor is None:
            return ([b.p0[0], b.p0[1], 0.0],
                    G.vunit([b.p1[0] - b.p0[0], b.p1[1] - b.p0[1], 0.0]))
        q = anchor.point3d(b.p0)
        q2 = anchor.point3d(b.p1)
        u = G.vunit(G.vsub(q2, q))
        return q, u

    def apply_fold(self, bend_id: str, theta: Optional[float] = None,
                   record: bool = True) -> FoldRecord:
        b = self.bends[bend_id]
        ang = b.fold_angle if theta is None else theta
        moved = self.moving_face_ids(b)
        q, u = self.current_axis(b, moved)
        # Flange rises toward +z.  With axis oriented p0->p1, rotating the
        # flange_side (+1 = cross>0, i.e. right side) by +theta about +u
        # lifts it; the other side needs -theta about +u (equiv +theta/-u).
        signed = b.flange_side * ang
        for i in moved:
            f = self.faces[i]
            f.R, f.t = G.rot_about(f.R, f.t, q, u, math.radians(signed))
        rec = FoldRecord(bend_id=bend_id, theta=ang, axis_p=q, axis_u=u,
                         moved=moved)
        if record:
            self.fold_history.append(rec)
            self.applied.append(bend_id)
        return rec

    # ------------------------------------------------------------- queries

    def all_triangles3d(self) -> List[Tuple[G.Vec3, G.Vec3, G.Vec3, int]]:
        out = []
        for fi, f in enumerate(self.faces):
            for (i, j, k) in f.triangles:
                out.append((f.point3d(f.poly2d[i]),
                            f.point3d(f.poly2d[j]),
                            f.point3d(f.poly2d[k]), fi))
        return out

    def section(self, q: G.Vec3, u: G.Vec3, samples: int = 3,
                tol: float = 1e-6
                ) -> List[Tuple[float, float, float, float, int]]:
        """Cut the folded part by planes perpendicular to axis (q, u).

        Returns segments in the machine-view plane: first coordinate measured
        along ``n`` (in-plane normal to the axis), second along z.  Each item
        is (s1,z1,s2,z2,face_index); multiple cut planes are merged into one
        polyline-free set (axisymmetric sections coincide)."""
        n = G.vunit(G.vcross(u, [0.0, 0.0, 1.0]))
        if G.vnorm(n) < 1e-6:
            n = [1.0, 0.0, 0.0]
        L = 0.0
        for (A, B, C, _f) in self.all_triangles3d():
            for P in (A, B, C):
                L = max(L, abs(G.vdot(G.vsub(P, q), u)))
        cut_positions = [L * f for f in (0.25, 0.5, 0.75)[:samples]]
        merged: Dict[int, List[Tuple[float, float]]] = {}
        for lam in cut_positions:
            plane_q = G.vadd(q, [u[0] * lam, u[1] * lam, u[2] * lam])
            for (A, B, C, fi) in self.all_triangles3d():
                V = [A, B, C]
                d = [G.vdot(G.vsub(v, plane_q), u) for v in V]
                hits: List[Tuple[float, float]] = []
                for a, bb in ((0, 1), (1, 2), (2, 0)):
                    if (d[a] > tol) != (d[bb] > tol) and \
                            abs(d[a] - d[bb]) > tol:
                        t = d[a] / (d[a] - d[bb])
                        p = G.vadd(V[a], G.vmul(G.vsub(V[bb], V[a]), t))
                        w = G.vsub(p, plane_q)
                        hits.append((G.vdot(w, n), p[2]))
                if len(hits) == 2:
                    merged.setdefault(fi, []).extend(hits)
        out = []
        for fi, pts in merged.items():
            pts.sort()
            a, bb = pts[0], pts[-1]
            if math.hypot(bb[0] - a[0], bb[1] - a[1]) > tol:
                out.append((a[0], a[1], bb[0], bb[1], fi))
        return out


def build_stage(contour, bends, thickness, grain_angle_deg,
                bend_ids: Sequence[str]) -> "Part":
    """Construct part geometry folded through *bend_ids* (given order)."""
    part = Part.build(contour, bends, thickness, grain_angle_deg)
    for bid in bend_ids:
        part.apply_fold(bid)
    return part
