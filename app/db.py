"""SQLite persistence for parts, process cards and sealed versions.

Cards are immutable once sealed; changing dimensions or equipment creates a
branched card whose ``parent_card_id`` points at the sealed original.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import (BranchRequest, Die, Material, PartCreate, PressBrake,
                     Punch)

_LOCK = threading.Lock()
DEFAULT_DB = os.environ.get("BENDPLAN_DB", "/tmp/bendplan.db")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_DB
        self._init()

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _init(self):
        with self.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS parts(
                  part_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  input_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cards(
                  card_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  part_id INTEGER NOT NULL REFERENCES parts(part_id),
                  parent_card_id INTEGER REFERENCES cards(card_id),
                  version INTEGER NOT NULL,
                  status TEXT NOT NULL DEFAULT 'draft',
                  created_at TEXT NOT NULL,
                  sealed_at TEXT,
                  input_snapshot TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  svg_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS catalogs(
                  kind TEXT NOT NULL, id TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  PRIMARY KEY(kind, id)
                );
                CREATE TABLE IF NOT EXISTS first_piece_runs(
                  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  card_id INTEGER NOT NULL REFERENCES cards(card_id),
                  idempotency_key TEXT NOT NULL UNIQUE,
                  material_lot TEXT NOT NULL,
                  measured_thickness_mm REAL NOT NULL,
                  measured_at TEXT NOT NULL,
                  instrument_json TEXT NOT NULL DEFAULT '{}',
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  report_json TEXT NOT NULL DEFAULT '{}',
                  draft_card_id INTEGER REFERENCES cards(card_id)
                );
                CREATE TABLE IF NOT EXISTS first_piece_measurements(
                  measurement_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id INTEGER NOT NULL
                    REFERENCES first_piece_runs(run_id),
                  step_no INTEGER NOT NULL,
                  bend_id TEXT NOT NULL,
                  die_id TEXT NOT NULL,
                  punch_id TEXT NOT NULL,
                  flip INTEGER NOT NULL,
                  planned_overbend_deg REAL NOT NULL,
                  measured_included_angle_deg REAL NOT NULL,
                  target_included_angle_deg REAL NOT NULL,
                  observed_springback_deg REAL NOT NULL,
                  nominal_thickness_mm REAL NOT NULL,
                  material_id TEXT NOT NULL,
                  grain_relation TEXT NOT NULL,
                  UNIQUE(run_id, step_no),
                  UNIQUE(run_id, bend_id)
                );
                """)
            self._migrate(c)
            self._seed(c)

    def _migrate(self, c):
        cols = {r["name"] for r in c.execute(
            "PRAGMA table_info(cards)").fetchall()}
        if "correction_json" not in cols:
            c.execute("ALTER TABLE cards ADD COLUMN correction_json "
                      "TEXT NOT NULL DEFAULT '{}'")

    # ----------------------------------------------------------- catalog

    def _seed(self, c):
        seeds = {
            ("material", "DC04"): Material(
                id="DC04", name="cold rolled DC04 (1.0338)",
                tensile_strength_mpa=330, yield_strength_mpa=210,
                k_factor=0.33, min_inside_radius_t=0.8,
                min_inside_radius_t_parallel=1.5).model_dump(),
            ("material", "SUS304"): Material(
                id="SUS304", name="stainless 1.4301",
                tensile_strength_mpa=600, yield_strength_mpa=290,
                k_factor=0.35, min_inside_radius_t=1.2,
                min_inside_radius_t_parallel=2.0).model_dump(),
            ("die", "V12"): Die(id="V12", v_width=12, v_angle_deg=88,
                                die_height=45, die_length=2000).model_dump(),
            ("die", "V16"): Die(id="V16", v_width=16, v_angle_deg=88,
                                die_height=50, die_length=2500).model_dump(),
            ("die", "V24"): Die(id="V24", v_width=24, v_angle_deg=86,
                                die_height=60, die_length=3000).model_dump(),
            ("punch", "R2-GOOSE"): Punch(
                id="R2-GOOSE", tip_radius=2, tip_angle_deg=86,
                punch_height=120, punch_length=2000, nose_width=6,
                relief_height=70, body_width=28,
                goose_neck_open_side=1).model_dump(),
            ("punch", "R3-STD"): Punch(
                id="R3-STD", tip_radius=3, tip_angle_deg=88,
                punch_height=110, punch_length=2500, nose_width=10,
                relief_height=40, body_width=26,
                goose_neck_open_side=0).model_dump(),
            ("punch", "R5-STD"): Punch(
                id="R5-STD", tip_radius=5, tip_angle_deg=86,
                punch_height=130, punch_length=3000, nose_width=14,
                relief_height=45, body_width=30,
                goose_neck_open_side=0).model_dump(),
            ("press", "AMADA-HFE-1003"): PressBrake(
                id="AMADA-HFE-1003", name="Amada HFE 100-3",
                tonnage_kn=1000, stroke_mm=200, open_height_mm=470,
                throat_depth_mm=400, backgauge_min_mm=5,
                backgauge_max_mm=650, backgauge_height_tolerance_mm=6,
                frame_clearance_side_mm=400,
                frame_clearance_height_mm=520).model_dump(),
            ("press", "SMALL-50T"): PressBrake(
                id="SMALL-50T", name="50 t workshop brake",
                tonnage_kn=500, stroke_mm=120, open_height_mm=360,
                throat_depth_mm=250, backgauge_min_mm=8,
                backgauge_max_mm=400, backgauge_height_tolerance_mm=5,
                frame_clearance_side_mm=250,
                frame_clearance_height_mm=400).model_dump(),
        }
        for (kind, cid), payload in seeds.items():
            c.execute(
                "INSERT OR IGNORE INTO catalogs(kind,id,payload) VALUES(?,?,?)",
                (kind, cid, json.dumps(payload)))

    def catalog(self, kind: str, cid: str) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT payload FROM catalogs WHERE kind=? AND id=?",
                            (kind, cid)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_catalog(self, kind: str) -> List[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT payload FROM catalogs WHERE kind=? "
                             "ORDER BY id", (kind,)).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def upsert_catalog(self, kind: str, payload: dict):
        with _LOCK, self.conn() as c:
            c.execute("INSERT INTO catalogs(kind,id,payload) VALUES(?,?,?) "
                      "ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload",
                      (kind, payload["id"], json.dumps(payload)))

    # ----------------------------------------------------------- parts

    def create_part(self, data: PartCreate) -> int:
        with _LOCK, self.conn() as c:
            cur = c.execute(
                "INSERT INTO parts(name,created_at,input_json) VALUES(?,?,?)",
                (data.name, now(), data.model_dump_json()))
            return cur.lastrowid

    def get_part(self, part_id: int) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM parts WHERE part_id=?",
                            (part_id,)).fetchone()
        return dict(row) if row else None

    def list_parts(self) -> List[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT part_id,name,created_at FROM parts "
                "ORDER BY part_id").fetchall()
        return [dict(r) for r in rows]

    def part_input(self, part_id: int) -> Optional[PartCreate]:
        row = self.get_part(part_id)
        return PartCreate(**json.loads(row["input_json"])) if row else None

    # ----------------------------------------------------------- cards

    def create_card(self, part_id: int, result: dict, svgs: List[str],
                    parent_card_id: Optional[int] = None,
                    input_snapshot: Optional[dict] = None,
                    correction: Optional[dict] = None) -> int:
        with _LOCK, self.conn() as c:
            ver = 1
            if parent_card_id is not None:
                prow = c.execute("SELECT version FROM cards WHERE card_id=?",
                                 (parent_card_id,)).fetchone()
                if prow is None:
                    raise ValueError("unknown parent card")
                ver = int(prow["version"]) + 1
            cur = c.execute(
                "INSERT INTO cards(part_id,parent_card_id,version,status,"
                "created_at,input_snapshot,result_json,svg_json,"
                "correction_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (part_id, parent_card_id, ver, "draft", now(),
                 json.dumps(input_snapshot or {}, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False),
                 json.dumps(svgs, ensure_ascii=False),
                 json.dumps(correction or {}, ensure_ascii=False)))
            return cur.lastrowid

    def get_card(self, card_id: int) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM cards WHERE card_id=?",
                            (card_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json"))
        d["svg"] = json.loads(d.pop("svg_json"))
        d["input_snapshot"] = json.loads(d["input_snapshot"])
        d["correction"] = json.loads(d["correction_json"])
        return d

    def list_cards(self, part_id: Optional[int] = None) -> List[dict]:
        q = ("SELECT card_id,part_id,parent_card_id,version,status,created_at,"
             "sealed_at FROM cards")
        args: tuple = ()
        if part_id is not None:
            q += " WHERE part_id=?"
            args = (part_id,)
        q += " ORDER BY card_id"
        with self.conn() as c:
            rows = c.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def seal_card(self, card_id: int) -> bool:
        with _LOCK, self.conn() as c:
            row = c.execute("SELECT status FROM cards WHERE card_id=?",
                            (card_id,)).fetchone()
            if row is None or row["status"] != "draft":
                return False
            c.execute("UPDATE cards SET status='sealed', sealed_at=? "
                      "WHERE card_id=?", (now(), card_id))
            return True

    def card_lineage(self, card_id: int) -> List[dict]:
        out: List[dict] = []
        cur = card_id
        seen = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            d = self.get_card(cur)
            if d is None:
                break
            node = {k: d[k] for k in (
                "card_id", "part_id", "parent_card_id", "version", "status",
                "created_at", "sealed_at")}
            corr = d.get("correction") or {}
            if corr:
                node["correction_kind"] = corr.get("kind")
                node["first_piece_run_id"] = corr.get("first_piece_run_id")
            out.append(node)
            cur = d["parent_card_id"]
        return out

    # ----------------------------------------------------- first-piece runs

    def create_first_piece_run(self, card_id: int, key: str,
                               material_lot: str,
                               measured_thickness_mm: float,
                               measured_at: str, instrument: dict,
                               note: str) -> int:
        with _LOCK, self.conn() as c:
            cur = c.execute(
                "INSERT INTO first_piece_runs(card_id,idempotency_key,"
                "material_lot,measured_thickness_mm,measured_at,"
                "instrument_json,note,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (card_id, key, material_lot, measured_thickness_mm,
                 measured_at, json.dumps(instrument, ensure_ascii=False),
                 note, now()))
            return cur.lastrowid

    def get_run_by_key(self, key: str) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM first_piece_runs "
                            "WHERE idempotency_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: int) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM first_piece_runs WHERE run_id=?",
                            (run_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["instrument"] = json.loads(d.pop("instrument_json"))
            d["report"] = json.loads(d.pop("report_json"))
            meas = c.execute(
                "SELECT * FROM first_piece_measurements WHERE run_id=? "
                "ORDER BY step_no", (run_id,)).fetchall()
            d["measurements"] = [dict(m) for m in meas]
        return d

    def add_measurement(self, run_id: int, m: dict) -> None:
        with _LOCK, self.conn() as c:
            c.execute(
                "INSERT INTO first_piece_measurements(run_id,step_no,"
                "bend_id,die_id,punch_id,flip,planned_overbend_deg,"
                "measured_included_angle_deg,target_included_angle_deg,"
                "observed_springback_deg,nominal_thickness_mm,"
                "material_id,grain_relation) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, m["step_no"], m["bend_id"], m["die_id"],
                 m["punch_id"], 1 if m["flip"] else 0,
                 m["planned_overbend_deg"],
                 m["measured_included_angle_deg"],
                 m["target_included_angle_deg"],
                 m["observed_springback_deg"],
                 m["nominal_thickness_mm"], m["material_id"],
                 m["grain_relation"]))

    def list_runs(self, card_id: Optional[int] = None) -> List[dict]:
        q = ("SELECT run_id,card_id,idempotency_key,material_lot,"
             "measured_thickness_mm,measured_at,note,created_at,"
             "draft_card_id FROM first_piece_runs")
        args: tuple = ()
        if card_id is not None:
            q += " WHERE card_id=?"
            args = (card_id,)
        q += " ORDER BY run_id"
        with self.conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def all_measurements(self) -> List[dict]:
        """Flat measurement rows joined with run context (sample pool)."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT m.*, r.card_id, r.run_id, r.material_lot,"
                "r.measured_thickness_mm, r.measured_at "
                "FROM first_piece_measurements m "
                "JOIN first_piece_runs r ON r.run_id = m.run_id "
                "ORDER BY m.measurement_id").fetchall()
        return [dict(r) for r in rows]

    def finish_first_piece_run(self, run_id: int, report: dict,
                               draft_card_id: Optional[int]) -> None:
        with _LOCK, self.conn() as c:
            c.execute(
                "UPDATE first_piece_runs SET report_json=?, "
                "draft_card_id=? WHERE run_id=?",
                (json.dumps(report, ensure_ascii=False), draft_card_id,
                 run_id))
