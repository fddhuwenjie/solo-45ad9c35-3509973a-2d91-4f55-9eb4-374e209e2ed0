"""First-piece springback correction: measurement intake, robust sample
matching, gating, derived draft cards and read-only guarantees."""
import copy
import json
import os

import pytest

EX = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")


def _load(rel):
    with open(os.path.join(EX, rel)) as fh:
        return json.load(fh)


def _sealed_card(client, payload):
    pid = client.post("/parts", json=payload).json()["part_id"]
    card = client.post(f"/parts/{pid}/solve").json()
    r = client.post(f"/cards/{card['card_id']}/seal")
    assert r.status_code == 200, r.text
    return card


def _fp_request(card, angle_by_bend=None, lot="LOT", key=None,
                thickness=2.0, instrument=True, measured_at=None,
                thresholds=None, note=""):
    """Angle lookup may be a constant or a {bend_id: angle} dict."""
    ms = []
    for s in card["result"]["steps"]:
        a = angle_by_bend if isinstance(angle_by_bend, (int, float)) \
            else angle_by_bend[s["bend_id"]]
        ms.append({"step_no": s["step_no"], "bend_id": s["bend_id"],
                   "die_id": s["die_id"], "punch_id": s["punch_id"],
                   "flip": s["flip"],
                   "measured_included_angle_deg": a})
    body = {"idempotency_key": key or f"key-{lot}",
            "measurements": ms, "material_lot": lot,
            "measured_thickness_mm": thickness, "note": note}
    if instrument:
        body["instrument"] = {"type": "protractor",
                              "resolution_deg": 0.1}
    if measured_at:
        body["measured_at"] = measured_at
    if thresholds:
        body["thresholds"] = thresholds
    return body


@pytest.fixture()
def ucard(client):
    return _sealed_card(client, _load("feasible/u_channel.json"))


# ----------------------------------------------------------- measurement intake

def test_first_piece_requires_sealed_card(client):
    payload = _load("feasible/u_channel.json")
    pid = client.post("/parts", json=payload).json()["part_id"]
    card = client.post(f"/parts/{pid}/solve").json()
    r = client.post(f"/cards/{card['card_id']}/first-piece",
                    json=_fp_request(card, 89.0))
    assert r.status_code == 400
    assert "sealed" in r.json()["detail"]


def test_unknown_card_404(client, ucard):
    body = _fp_request(ucard, 89.0)
    r = client.post("/cards/9999/first-piece", json=body)
    assert r.status_code == 404


@pytest.mark.parametrize("mut", ["die", "punch", "flip", "bend", "step"])
def test_measurement_must_match_step_tooling_and_direction(client, ucard, mut):
    body = _fp_request(ucard, 89.0)
    m = body["measurements"][0]
    if mut == "die":
        m["die_id"] = "V16" if m["die_id"] == "V12" else "V12"
    elif mut == "punch":
        m["punch_id"] = "R3-STD" if m["punch_id"] == "R2-GOOSE" \
            else "R2-GOOSE"
    elif mut == "flip":
        m["flip"] = not m["flip"]
    elif mut == "bend":
        m["bend_id"] = "B2" if m["bend_id"] == "B1" else "B1"
    elif mut == "step":
        m["step_no"] = 5
    r = client.post(f"/cards/{ucard['card_id']}/first-piece", json=body)
    assert r.status_code == 400, r.text


def test_measurements_must_cover_every_step_once(client, ucard):
    body = _fp_request(ucard, 89.0)
    body["measurements"].pop()
    r = client.post(f"/cards/{ucard['card_id']}/first-piece", json=body)
    assert r.status_code == 400 and "one measurement per step" in r.json()["detail"]

    body = _fp_request(ucard, 89.0)
    body["measurements"][1]["step_no"] = body["measurements"][0]["step_no"]
    r = client.post(f"/cards/{ucard['card_id']}/first-piece", json=body)
    assert r.status_code == 400 and "duplicate" in r.json()["detail"]


def test_invalid_angle_and_thickness_rejected(client, ucard):
    body = _fp_request(ucard, 89.0)
    body["measurements"][0]["measured_included_angle_deg"] = 200
    r = client.post(f"/cards/{ucard['card_id']}/first-piece", json=body)
    assert r.status_code == 422
    body = _fp_request(ucard, 89.0)
    body["measured_thickness_mm"] = 0
    r = client.post(f"/cards/{ucard['card_id']}/first-piece", json=body)
    assert r.status_code == 422


# ----------------------------------------------------------- idempotency

def test_idempotent_replay_records_run_once(client, ucard):
    body = _fp_request(ucard, 89.0, key="once")
    r1 = client.post(f"/cards/{ucard['card_id']}/first-piece", json=body)
    assert r1.status_code == 200 and r1.json()["replay"] is False
    run_id = r1.json()["run_id"]
    r2 = client.post(f"/cards/{ucard['card_id']}/first-piece", json=body)
    assert r2.status_code == 200
    j2 = r2.json()
    assert j2["replay"] is True and j2["run_id"] == run_id
    runs = client.get(f"/cards/{ucard['card_id']}/first-piece").json()
    assert len(runs) == 1
    meas = client.get(f"/first-piece/{run_id}").json()["measurements"]
    assert len(meas) == 2


def test_idempotency_key_reuse_on_other_card_conflicts(client, ucard):
    payload = _load("feasible/u_channel.json")
    other = _sealed_card(client, payload)
    r = client.post(f"/cards/{ucard['card_id']}/first-piece",
                    json=_fp_request(ucard, 89.0, key="shared"))
    assert r.status_code == 200
    r = client.post(f"/cards/{other['card_id']}/first-piece",
                    json=_fp_request(other, 89.0, key="shared"))
    assert r.status_code == 409


# ----------------------------------------------------------- sample gating

def test_first_run_too_few_samples_is_suggestion_only(client, ucard):
    r = client.post(f"/cards/{ucard['card_id']}/first-piece",
                    json=_fp_request(ucard, 89.0, lot="A"))
    j = r.json()
    assert j["decision"] == "suggestion_only"
    assert j["draft_card_id"] is None
    for b in j["report"]["bends"]:
        assert b["statistics"]["sample_count"] == 2  # own two bends
        assert "insufficient_samples" in b["rejected_reasons"]
        assert b["suggested_overbend_deg"] is not None  # suggestion given


def test_third_comparable_run_creates_draft(client):
    payload = _load("feasible/u_channel.json")
    cards = [_sealed_card(client, copy.deepcopy(payload)) for _ in range(3)]
    # observed springback: measured 89.1 -> 89 - 90 + 2 = 1.1 deg
    outs = []
    for i, card in enumerate(cards):
        r = client.post(
            f"/cards/{card['card_id']}/first-piece",
            json=_fp_request(card, 89.0 + 0.1 * i, lot=f"L{i}"))
        outs.append(r.json())
    assert outs[0]["decision"] == "suggestion_only"
    final = outs[-1]
    assert final["decision"] == "draft_created", final["report"]["rejections"]
    draft_id = final["draft_card_id"]
    assert draft_id is not None

    b = final["report"]["bends"][0]
    assert b["statistics"]["sample_count"] == 6
    assert b["statistics"]["median_springback_deg"] == pytest.approx(1.1)
    assert b["rejected_reasons"] == []
    assert b["angles"]["overbend_before_deg"] == 2.0
    assert b["angles"]["overbend_after_deg"] == pytest.approx(1.1)
    assert b["angles"]["ram_included_after_deg"] == pytest.approx(88.9)
    assert len(b["samples_used"]) == 6
    assert {s["run_id"] for s in b["samples_used"]}  # runs identified

    # derived draft: version lineage, never sealed
    draft = client.get(f"/cards/{draft_id}").json()
    assert draft["status"] == "draft"
    assert draft["version"] == 2
    assert draft["parent_card_id"] == cards[-1]["card_id"]
    assert draft["input_snapshot"]["springback_overrides"] == {
        "B1": pytest.approx(1.1), "B2": pytest.approx(1.1)}
    for step in draft["result"]["steps"]:
        assert step["springback_used_deg"] == pytest.approx(1.1)
        assert all(c["ok"] for c in step["checks"])
        # punch angle, stroke and daylight were re-checked with the
        # corrected overbend
        codes = {c["code"] for c in step["checks"]}
        assert {"punch_fit", "stroke", "daylight", "frame_collision",
                "tool_collision"} <= codes

    # card JSON enumerates adopted samples and before/after angles
    meta = draft["correction"]
    assert meta["kind"] == "first_piece_correction"
    assert meta["source_card_id"] == cards[-1]["card_id"]
    assert meta["material_lot"] == "L2"
    assert all(len(x["samples_used"]) == 6 for x in meta["bends"])
    assert meta["bends"][0]["angles"]["overbend_after_deg"]

    lin = client.get(f"/cards/{draft_id}/lineage").json()
    assert [x["card_id"] for x in lin] == [draft_id, cards[-1]["card_id"]]
    assert lin[0]["version"] == 2 and lin[1]["version"] == 1
    assert lin[0]["correction_kind"] == "first_piece_correction"
    assert lin[0]["first_piece_run_id"] == final["run_id"]
    assert "correction_kind" not in lin[1]

    # the sealed original is untouched
    sealed = client.get(f"/cards/{cards[-1]['card_id']}").json()
    assert sealed["status"] == "sealed"
    assert sealed["result"]["steps"][0]["springback_used_deg"] == 2.0
    assert sealed["correction"] == {}


def test_dispersion_exceeded_blocks_draft(client):
    payload = _load("feasible/u_channel.json")
    cards = [_sealed_card(client, copy.deepcopy(payload)) for _ in range(4)]
    # observations 1.0, 1.0, 8.0, 8.0 deg on both bends -> robust sigma
    # 1.4826*3.5 ~= 5.2 deg, median 4.5 stays under the 6 deg cap
    angles = [89.0, 89.0, 82.0, 82.0]
    last = None
    for i, (card, ang) in enumerate(zip(cards, angles)):
        last = client.post(
            f"/cards/{card['card_id']}/first-piece",
            json=_fp_request(card, ang, lot=f"D{i}", key=f"disp{i}")).json()
    assert last["decision"] == "suggestion_only"
    b1 = next(b for b in last["report"]["bends"] if b["bend_id"] == "B1")
    assert "dispersion_exceeded" in b1["rejected_reasons"]
    assert b1["statistics"]["robust_sigma_deg"] > 1.5
    assert last["draft_card_id"] is None
    assert any(r.get("bend_id") == "B1" for r in last["report"]["rejections"])


def test_compensation_cap_blocks_draft(client):
    payload = _load("feasible/u_channel.json")
    cards = [_sealed_card(client, copy.deepcopy(payload)) for _ in range(3)]
    for i, card in enumerate(cards):
        j = client.post(
            f"/cards/{card['card_id']}/first-piece",
            json=_fp_request(card, 100.0, lot=f"CAP{i}")).json()
    assert j["decision"] == "suggestion_only"
    for b in j["report"]["bends"]:
        assert "compensation_cap" in b["rejected_reasons"]
        # median 12 deg is suggested but clamped in the recommendation
        assert b["statistics"]["median_springback_deg"] == pytest.approx(12.0)
        assert b["suggested_overbend_deg"] == pytest.approx(6.0)
        assert b["clamped_to_cap"] is True


def test_incompatible_measured_thickness_blocks_draft(client):
    payload = _load("feasible/u_channel.json")
    cards = [_sealed_card(client, copy.deepcopy(payload)) for _ in range(3)]
    for i, card in enumerate(cards[:-1]):
        client.post(f"/cards/{card['card_id']}/first-piece",
                    json=_fp_request(card, 89.0, lot=f"T{i}"))
    # nominal 2.0 mm; 2.5 mm is 25% off, beyond the 20% incompatibility
    j = client.post(
        f"/cards/{cards[-1]['card_id']}/first-piece",
        json=_fp_request(cards[-1], 89.0, lot="THICK",
                         thickness=2.5)).json()
    assert j["decision"] == "suggestion_only"
    globals_ = [r for r in j["report"]["rejections"] if "global" in r]
    assert any(r["reasons"] == ["incompatible_condition"] for r in globals_)


def test_threshold_overrides_are_listed_and_effective(client, ucard):
    # with min_samples=1 the very first run is statistically acceptable
    r = client.post(
        f"/cards/{ucard['card_id']}/first-piece",
        json=_fp_request(ucard, 89.0, lot="SOLO",
                         thresholds={"min_samples": 1,
                                     "max_compensation_deg": 4.0})).json()
    assert r["report"]["thresholds"]["min_samples"] == 1
    assert r["report"]["thresholds"]["max_compensation_deg"] == 4.0
    assert r["decision"] == "draft_created"
    for b in r["report"]["bends"]:
        assert b["statistics"]["min_samples"] == 1


def test_replanning_failure_is_reported_not_sealed(client):
    payload = _load("feasible/u_channel.json")
    cards = [_sealed_card(client, copy.deepcopy(payload)) for _ in range(3)]
    last = None
    for i, card in enumerate(cards):
        # observed springback ~12 deg; allow it past the cap gate so the
        # derived plan is actually attempted
        last = client.post(
            f"/cards/{card['card_id']}/first-piece",
            json=_fp_request(card, 100.0, lot=f"R{i}",
                             thresholds={"max_compensation_deg": 12.0,
                                         "max_robust_sigma_deg": 3.0})
        ).json()
    assert last["decision"] == "suggestion_only"
    rej = [r for r in last["report"]["rejections"] if "global" in r]
    assert any("replanning_failed" in r["reasons"] for r in rej)
    failure = next(r["failure"] for r in rej if "failure" in r and r["failure"])
    assert failure["bend_id"] in ("B1", "B2")
    codes = {c["code"] for c in failure["joint_constraints"]}
    # punch tip 86 deg cannot reach a 78 deg ram included angle
    assert "punch_fit" in codes


# ----------------------------------------------------------- sample matching

def test_grain_relation_separates_samples(client):
    across = _load("feasible/u_channel.json")  # bend axis 90 deg vs grain 0
    parallel = copy.deepcopy(across)
    parallel["grain_direction_deg"] = 90.0
    for b in parallel["bends"]:  # parallel grain needs R >= 1.5t = 3 mm
        b["inside_radius"] = 3
    c_across = _sealed_card(client, across)
    c_par = _sealed_card(client, parallel)

    client.post(f"/cards/{c_par['card_id']}/first-piece",
                json=_fp_request(c_par, 89.0, lot="P1"))
    r = client.post(
        f"/cards/{c_across['card_id']}/first-piece",
        json=_fp_request(c_across, 89.0, lot="A1",
                         thresholds={"min_samples": 1})).json()
    b1 = r["report"]["bends"][0]
    counts = b1["statistics"]["filter_counts"]
    # 4 measurements total (2 per run); both parallel-grain ones are
    # filtered out before the grain gate
    assert counts["grain"] == 2 and counts["kept"] == 2
    assert all(s["grain_relation"] == "perpendicular"
               for s in b1["samples_used"])
    # parallel run's own report labels its bends parallel
    prun = client.get(f"/cards/{c_par['card_id']}/first-piece").json()[0]
    pdetail = client.get(f"/first-piece/{prun['run_id']}").json()
    assert {m["grain_relation"] for m in pdetail["measurements"]} == \
        {"parallel"}


def test_thickness_band_and_tooling_separate_samples(client):
    base = _load("feasible/u_channel.json")
    c1 = _sealed_card(client, copy.deepcopy(base))
    client.post(f"/cards/{c1['card_id']}/first-piece",
                json=_fp_request(c1, 89.0, lot="N"))

    thick = copy.deepcopy(base)
    thick["thickness"] = 2.5
    c2 = _sealed_card(client, thick)  # still feasible
    r = client.post(
        f"/cards/{c2['card_id']}/first-piece",
        json=_fp_request(c2, 89.0, lot="TH", thickness=2.5,
                         thresholds={"min_samples": 1})).json()
    b1 = r["report"]["bends"][0]
    # the 2.0 mm samples are outside the +/-15% band around 2.5 nominal
    counts = b1["statistics"]["filter_counts"]
    assert counts["thickness"] == 2 and counts["kept"] == 2

    other_tools = copy.deepcopy(base)
    other_tools["candidate_dies"] = ["V16"]
    c3 = _sealed_card(client, other_tools)
    r = client.post(
        f"/cards/{c3['card_id']}/first-piece",
        json=_fp_request(c3, 89.0, lot="V16",
                         thresholds={"min_samples": 1})).json()
    b1 = r["report"]["bends"][0]
    counts = b1["statistics"]["filter_counts"]
    assert counts["tooling"] == 2 and counts["kept"] == 2


# ----------------------------------------------------------- read-only / output

def test_measurement_records_are_read_only_and_listed(client, ucard):
    r = client.post(f"/cards/{ucard['card_id']}/first-piece",
                    json=_fp_request(ucard, 89.0, lot="RO"))
    run_id = r.json()["run_id"]
    detail = client.get(f"/first-piece/{run_id}").json()
    assert detail["material_lot"] == "RO"
    m = detail["measurements"][0]
    assert m["die_id"] == ucard["result"]["steps"][0]["die_id"]
    assert m["observed_springback_deg"] == pytest.approx(1.0)
    assert m["planned_overbend_deg"] == pytest.approx(2.0)
    assert client.get("/first-piece/9999").status_code == 404
    assert len(client.get("/first-piece").json()) == 1
    # measured timestamp/instrument round-trip
    assert detail["measured_at"]
    assert detail["instrument"]["type"] == "protractor"


def test_draft_is_not_auto_sealed_and_starts_a_lineage(client):
    payload = _load("feasible/u_channel.json")
    cards = [_sealed_card(client, copy.deepcopy(payload)) for _ in range(3)]
    last_json = None
    for i, card in enumerate(cards):
        last_json = client.post(
            f"/cards/{card['card_id']}/first-piece",
            json=_fp_request(card, 89.0, lot=f"L{i}")).json()
    draft_id = last_json["draft_card_id"]
    # drafts cannot accept first-piece measurements
    r = client.post(f"/cards/{draft_id}/first-piece",
                    json=_fp_request(cards[-1], 89.0, lot="X"))
    assert r.status_code == 400
    # report enumerates rejection basis even when accepted
    assert last_json["report"]["rejections"] == []
    assert last_json["report"]["order_forced"] == ["B1", "B2"]
