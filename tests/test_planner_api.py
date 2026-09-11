"""End-to-end API/planner tests: feasible part, collisions, seal/branch."""
import json
import os

import pytest

EX = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")


def _load(rel):
    with open(os.path.join(EX, rel)) as fh:
        return json.load(fh)


def _create_solve(client, payload):
    r = client.post("/parts", json=payload)
    assert r.status_code == 201, r.text
    pid = r.json()["part_id"]
    return pid, client.post(f"/parts/{pid}/solve").json()


def test_feasible_u_channel(client):
    pid, card = _create_solve(client, _load("feasible/u_channel.json"))
    res = card["result"]
    assert res["feasible"] is True
    assert sorted(res["order"]) == ["B1", "B2"]
    assert len(res["steps"]) == 2
    for step in res["steps"]:
        assert step["die_id"] and step["punch_id"]
        assert step["tonnage_kn"] > 0
        assert step["backgauge_distance_mm"] > 0
        assert step["workpiece_orientation"]
        assert step["feed_direction"]
        assert all(c["ok"] for c in step["checks"])
        assert step["section"], "step carries the shared section geometry"
    # JSON card and SVG share one geometry result
    assert len(card["svg"]) == 2
    for svg in card["svg"]:
        assert svg.startswith("<svg")
    # per-step SVG endpoint
    r = client.get(f"/cards/{card['card_id']}/svg?step=1")
    assert r.status_code == 200 and r.text.startswith("<svg")
    r = client.get(f"/cards/{card['card_id']}/svg")
    assert r.status_code == 200 and r.text.count("<svg") == 2


def test_result_stable_across_runs(client):
    payload = _load("feasible/u_channel.json")
    _, c1 = _create_solve(client, payload)
    _, c2 = _create_solve(client, payload)
    assert c1["result"]["order"] == c2["result"]["order"]
    assert [(s["bend_id"], s["die_id"], s["punch_id"], s["flip"])
            for s in c1["result"]["steps"]] == \
           [(s["bend_id"], s["die_id"], s["punch_id"], s["flip"])
            for s in c2["result"]["steps"]]


def test_collision_deep_tray_reports_constraints(client):
    pid, card = _create_solve(
        client, _load("collision/deep_tray_small_press.json"))
    res = card["result"]
    assert res["feasible"] is False
    f = res["failure"]
    assert f["earliest_step"] >= 1
    assert f["bend_id"] in ("B1", "B2")
    codes = {c["code"] for c in f["joint_constraints"]}
    assert codes & {"backgauge_reach", "frame_collision", "tool_collision"}
    # no SVG for an infeasible card
    assert card["svg"] == []


def test_stepped_profile_no_order_but_step1_possible(client):
    payload = _load("collision/stepped_profile_no_order.json")
    pid, card = _create_solve(client, payload)
    res = card["result"]
    assert res["feasible"] is False
    # every technician permutation also fails
    import itertools
    for perm in itertools.permutations(["B1", "B2", "B3"]):
        r = client.post(f"/parts/{pid}/check-sequence",
                        json={"bend_ids": list(perm)})
        assert r.json()["feasible"] is False, perm


def test_technician_sequence_validation(client):
    pid, _ = _create_solve(client, _load("feasible/u_channel.json"))
    r = client.post(f"/parts/{pid}/check-sequence",
                    json={"bend_ids": ["B2", "B1"]})
    assert r.status_code == 200
    assert r.json()["requested_order"] == ["B2", "B1"]
    r = client.post(f"/parts/{pid}/check-sequence",
                    json={"bend_ids": ["B1"]})
    assert r.status_code == 200 and r.json()["feasible"] is False
    r = client.post(f"/parts/{pid}/check-sequence",
                    json={"bend_ids": ["B1", "B1"]})
    assert r.json()["feasible"] is False


def test_seal_freezes_and_branch_requires_change(client):
    pid, card = _create_solve(client, _load("feasible/u_channel.json"))
    cid = card["card_id"]
    assert client.post(f"/cards/{cid}/seal").status_code == 200
    # double seal rejected
    assert client.post(f"/cards/{cid}/seal").status_code == 409
    # branch without any change rejected
    payload = _load("feasible/u_channel.json")
    r = client.post(f"/cards/{cid}/branch", json={"note": "same"})
    assert r.status_code == 409
    # branch with equipment change -> new version linked to parent
    changed = dict(payload)
    changed["machine_id"] = "SMALL-50T"
    changed["candidate_dies"] = ["V16"]
    changed["candidate_punches"] = ["R3-STD"]
    r = client.post(f"/cards/{cid}/branch",
                    json={"note": "moved to 50t", "part_changes": changed})
    assert r.status_code == 201, r.text
    branched = r.json()
    assert branched["parent_card_id"] == cid
    assert branched["version"] == 2
    assert branched["status"] == "draft"
    # lineage
    lin = client.get(f"/cards/{branched['card_id']}/lineage").json()
    assert [x["card_id"] for x in lin] == [branched["card_id"], cid]
    # cannot branch from a draft
    assert client.post(
        f"/cards/{branched['card_id']}/branch",
        json={"part_changes": changed}).status_code == 409


def test_min_flange_and_tonnage_limits(client):
    # 5 mm flange shorter than V/2+2t = 6+4 = 10 mm -> min_flange failure
    # 5 mm flange is on the +side (y 55..60); cross>0 for p0->p1 along +x
    payload = {
        "name": "tiny flange",
        "contour": [[0, 0], [100, 0], [100, 60], [0, 60]],
        "bends": [{"id": "B1", "p0": [0, 55], "p1": [100, 55],
                   "target_angle_deg": 90, "inside_radius": 2,
                   "flange_side": 1}],
        "thickness": 2.0, "material_id": "DC04",
        "machine_id": "AMADA-HFE-1003",
        "candidate_dies": ["V12"], "candidate_punches": ["R2-GOOSE"]}
    pid = client.post("/parts", json=payload).json()["part_id"]
    res = client.post(f"/parts/{pid}/solve").json()["result"]
    assert res["feasible"] is False
    codes = {c["code"] for c in res["failure"]["joint_constraints"]}
    assert "min_flange" in codes


def test_tonnage_exceeds_press(client):
    # thick, strong, long bend on the 50 t press -> tonnage failure
    payload = {
        "name": "overload bend",
        "contour": [[0, 0], [2500, 0], [2500, 120], [0, 120]],
        "bends": [{"id": "B1", "p0": [0, 60], "p1": [2500, 60],
                   "target_angle_deg": 90, "inside_radius": 5,
                   "flange_side": -1}],
        "thickness": 5.0, "material_id": "SUS304",
        "machine_id": "SMALL-50T",
        "candidate_dies": ["V24"], "candidate_punches": ["R5-STD"]}
    pid = client.post("/parts", json=payload).json()["part_id"]
    res = client.post(f"/parts/{pid}/solve").json()["result"]
    assert res["feasible"] is False
    codes = {c["code"] for c in res["failure"]["joint_constraints"]}
    assert "tonnage" in codes
