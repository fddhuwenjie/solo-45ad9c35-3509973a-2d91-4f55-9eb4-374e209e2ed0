"""Pydantic request/response models for the bend planning API."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------- catalogs

class Material(BaseModel):
    id: str
    name: str
    tensile_strength_mpa: float = Field(gt=0)
    yield_strength_mpa: float = Field(default=220.0, gt=0)
    k_factor: float = Field(default=0.33, gt=0, lt=1)
    min_inside_radius_t: float = Field(
        default=0.8, gt=0,
        description="minimum inside radius in thickness multiples, "
                    "perpendicular to grain")
    min_inside_radius_t_parallel: float = Field(
        default=1.5, gt=0,
        description="minimum inside radius when the bend axis is parallel "
                    "to the grain (cracking risk)")


class Die(BaseModel):
    id: str
    v_width: float = Field(gt=0, description="V opening width, mm")
    v_angle_deg: float = Field(default=88.0, gt=0, lt=180)
    die_height: float = Field(gt=0)
    die_length: float = Field(gt=0)
    min_tonnage_kn: float = Field(default=0.0, ge=0)


class Punch(BaseModel):
    id: str
    tip_radius: float = Field(ge=0)
    tip_angle_deg: float = Field(gt=0, lt=180)
    punch_height: float = Field(gt=0)
    punch_length: float = Field(gt=0)
    nose_width: float = Field(gt=0,
                              description="relieved nose section width, mm")
    relief_height: float = Field(
        default=0.0, ge=0,
        description="height of the relieved (narrow) nose section, mm")
    body_width: float = Field(
        default=0.0, gt=0,
        description="punch body width above the relieved nose, mm")
    goose_neck_open_side: int = Field(
        default=0, ge=-1, le=1,
        description="0 = symmetric punch; +1/-1 = goose-neck whose body "
                    "stands back on that machine-s side so a tall rising "
                    "flange on the other side clears it")


class PressBrake(BaseModel):
    id: str
    name: str = ""
    tonnage_kn: float = Field(gt=0, description="maximum bending force")
    stroke_mm: float = Field(gt=0, description="ram travel")
    open_height_mm: float = Field(gt=0, description="daylight at ram top")
    throat_depth_mm: float = Field(gt=0)
    backgauge_min_mm: float = Field(
        default=0.0, ge=0,
        description="minimum reachable gauge distance from the die V centre")
    backgauge_max_mm: float = Field(gt=0)
    backgauge_height_tolerance_mm: float = Field(
        default=5.0, gt=0,
        description="how far the contacting edge may lie off the die plane")
    frame_clearance_side_mm: float = Field(
        default=150.0, gt=0,
        description="clear horizontal distance from V centre to side frame")
    frame_clearance_height_mm: float = Field(
        default=400.0, gt=0,
        description="clear vertical height at the side frame")


# ---------------------------------------------------------------- part input

Point = List[float]


class BendLine(BaseModel):
    id: str
    p0: Point = Field(min_length=2, max_length=2)
    p1: Point = Field(min_length=2, max_length=2)
    target_angle_deg: float = Field(gt=0, lt=180)
    inside_radius: float = Field(ge=0)
    flange_side: Literal[1, -1] = Field(
        default=1,
        description="which side of p0->p1 (cross-product sign) rises up")

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("bend id must not be blank")
        return v


class PartCreate(BaseModel):
    name: str = Field(min_length=1)
    contour: List[Point] = Field(min_length=4)
    bends: List[BendLine] = Field(min_length=1)
    thickness: float = Field(gt=0)
    material_id: str
    grain_direction_deg: float = Field(
        default=0.0,
        description="grain direction measured from +x of the blank frame")
    machine_id: str
    candidate_dies: List[str]
    candidate_punches: List[str]
    springback_deg: float = Field(
        default=2.0, ge=0,
        description="extra overbend applied uniformly (air bending)")
    springback_overrides: Optional[Dict[str, float]] = Field(
        default=None,
        description="per-bend overbend allowance, deg (first-piece "
                    "correction); falls back to springback_deg when absent")

    @field_validator("springback_overrides")
    @classmethod
    def _overrides_nonneg(cls, v):
        if v is not None:
            for bid, val in v.items():
                if val < 0:
                    raise ValueError(
                        f"springback override for bend {bid} must be >= 0")
        return v

    @field_validator("contour")
    @classmethod
    def _contour_valid(cls, v):
        if len(v) < 3:
            raise ValueError("contour needs at least 3 points")
        for p in v:
            if len(p) != 2:
                raise ValueError("contour points must be 2D")
        return v


# ---------------------------------------------------------------- planning

class TechnicianSequence(BaseModel):
    bend_ids: List[str]
    note: Optional[str] = None


class BranchRequest(BaseModel):
    note: str = ""
    part_changes: Optional[PartCreate] = Field(
        default=None,
        description="if given, create a branched part from these new inputs; "
                    "otherwise the same part is replanned on a changed machine")
    machine_id: Optional[str] = None
    candidate_dies: Optional[List[str]] = None
    candidate_punches: Optional[List[str]] = None


# ------------------------------------------------------- first-piece springback

class Instrument(BaseModel):
    id: Optional[str] = Field(default=None, description="gauge serial no.")
    type: Optional[str] = Field(default=None,
                               description="e.g. protractor, bevel gauge, CMM")
    resolution_deg: Optional[float] = Field(default=None, gt=0)


class FirstPieceMeasurement(BaseModel):
    step_no: int = Field(gt=0, description="step in the sealed card")
    bend_id: str
    die_id: str = Field(description="must match the die used on that step")
    punch_id: str = Field(description="must match the punch used on that step")
    flip: bool = Field(description="must match the bend direction/orientation "
                                   "recorded on that step")
    measured_included_angle_deg: float = Field(
        gt=0, lt=180,
        description="measured angle between the flanges after unloading")

    @field_validator("bend_id")
    @classmethod
    def _bend_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("bend id must not be blank")
        return v


class FirstPieceRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    measurements: List[FirstPieceMeasurement] = Field(min_length=1)
    measured_at: Optional[datetime] = Field(
        default=None,
        description="measurement time; server UTC now when omitted")
    instrument: Optional[Instrument] = None
    material_lot: str = Field(min_length=1)
    measured_thickness_mm: float = Field(gt=0)
    note: str = ""
    thresholds: Optional["CorrectionThresholds"] = Field(
        default=None,
        description="override the configurable acceptance thresholds for "
                    "this correction run only")


class CorrectionThresholds(BaseModel):
    min_samples: int = Field(default=3, ge=1)
    thickness_band: float = Field(
        default=0.15, gt=0, le=1,
        description="comparable sample thickness within +/- this fraction")
    thickness_incompatible: float = Field(
        default=0.20, gt=0, le=1,
        description="measured thickness beyond +/- this fraction of the "
                    "nominal thickness is an incompatible working condition")
    target_angle_tol_deg: float = Field(default=5.0, gt=0)
    max_robust_sigma_deg: float = Field(
        default=1.5, gt=0,
        description="maximum dispersion (robust sigma of observed "
                    "springback) for an automatic draft")
    max_compensation_deg: float = Field(
        default=6.0, gt=0,
        description="maximum |per-bend compensation| allowed for an "
                    "automatic draft")
    mad_k: float = Field(
        default=1.4826, gt=0,
        description="MAD -> robust sigma consistency factor for normal data")


FirstPieceRequest.model_rebuild()


# ---------------------------------------------------------------- output DTO

class GaugeContact(BaseModel):
    edge_bend_id: Optional[str] = None
    distance_mm: float
    contact_z: float


class StepReport(BaseModel):
    step_no: int
    bend_id: str
    workpiece_orientation: str
    feed_direction: str
    flip: bool
    die_id: str
    punch_id: str
    springback_used_deg: float = 0.0
    backgauge_distance_mm: float
    backgauge_contact: GaugeContact
    tonnage_kn: float
    bend_angle_deg: float
    fold_angle_deg: float
    checks: List["CheckResult"]
    section_svg: Optional[str] = None


class CheckResult(BaseModel):
    code: str
    ok: bool
    detail: str
    value: Optional[float] = None
    limit: Optional[float] = None


class FailureReport(BaseModel):
    earliest_step: int
    bend_id: str
    joint_constraints: List[CheckResult]
    all_orders_exhausted: int
    message: str


class PlanResult(BaseModel):
    feasible: bool
    order: List[str]
    steps: List[StepReport]
    failure: Optional[FailureReport] = None
    geometry_version: int = 1


class PartOut(BaseModel):
    part_id: int
    name: str
    created_at: str


class CardVersionOut(BaseModel):
    card_id: int
    part_id: int
    version: int
    status: str
    parent_card_id: Optional[int]
    created_at: str
    result: PlanResult
    input_snapshot: dict
    svg: List[str]


StepReport.model_rebuild()
