"""Regression: the FastAPI app must import and expose working routes.

Covers the defect where importing ``app.main`` raised ModuleNotFoundError and
the service had no entry point / routes.
"""
import importlib

import pytest


def test_app_module_imports():
    mod = importlib.import_module("app.main")
    assert hasattr(mod, "app")


def test_health_and_catalog_routes(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    assert client.get("/catalog/materials").status_code == 200
    assert client.get("/catalog/dies").status_code == 200
    assert client.get("/catalog/punches").status_code == 200
    assert client.get("/catalog/presses").status_code == 200

    paths = client.get("/openapi.json").json()["paths"]
    for expected in (
        "/parts",
        "/parts/{part_id}/solve",
        "/parts/{part_id}/check-sequence",
        "/cards/{card_id}",
        "/cards/{card_id}/seal",
        "/cards/{card_id}/branch",
        "/cards/{card_id}/lineage",
        "/cards/{card_id}/svg",
        "/cards/{card_id}/first-piece",
        "/first-piece",
        "/first-piece/{run_id}",
    ):
        assert expected in paths, expected


def test_unknown_catalog_ref_rejected(client):
    body = {
        "name": "x", "contour": [[0, 0], [1, 0], [1, 1], [0, 1]],
        "bends": [{"id": "B", "p0": [0, .5], "p1": [1, .5],
                   "target_angle_deg": 90, "inside_radius": 1}],
        "thickness": 1.0, "material_id": "NOPE",
        "machine_id": "AMADA-HFE-1003",
        "candidate_dies": ["V12"], "candidate_punches": ["R2-GOOSE"]}
    r = client.post("/parts", json=body)
    assert r.status_code == 400
