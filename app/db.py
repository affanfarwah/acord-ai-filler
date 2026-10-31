"""SQLite storage. A case (one client submission) owns its source files, selected forms and one fact ledger.

Facts are keyed by the ACORD data-element name (e.g. NamedInsured_FullName_A). ACORD uses one data
dictionary across forms, so the same key means the same thing on 125, 127, 137... and one fact fills
every form that has that field.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("ACORD_DATA", BASE / "out"))
DB_PATH = DATA / "app.db"

# Fact statuses. Only USABLE ones are written into PDFs; the rest wait for the agent.
VERIFIED = "verified"   # AI value whose cited source cell/page was checked in code
REVIEW = "review"       # AI value whose source could not be checked
CONFIRM = "confirm"     # came from a row the questionnaire marks Confirm
APPROVED = "approved"   # agent approved an AI value
EDITED = "edited"       # agent typed it
REJECTED = "rejected"   # agent removed a wrong AI value: kept out of PDFs and never re-proposed
USABLE = {VERIFIED, APPROVED, EDITED}
PENDING = {REVIEW, CONFIRM}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agencies (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, invite_code TEXT NOT NULL,
    producer TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL, name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY, agency_id INTEGER, key TEXT NOT NULL, title TEXT NOT NULL, path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY, agency_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '[]',
    archived INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS case_docs (
    id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL, added REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS case_forms (
    id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, template_key TEXT NOT NULL, UNIQUE(case_id, template_key)
);
CREATE TABLE IF NOT EXISTS facts (
    case_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY (case_id, key)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, status TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, agency_id INTEGER, user_id INTEGER, action TEXT NOT NULL, detail TEXT
);
"""


def conn():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")


def audit(agency_id, user_id, action, detail=""):
    """Log who did what. Never put field values in `detail`."""
    with conn() as c:
        c.execute("INSERT INTO audit (ts, agency_id, user_id, action, detail) VALUES (?,?,?,?,?)",
                  (time.time(), agency_id, user_id, action, detail))


def case_row(c, case_id, agency_id):
    """Fetch a case only if it belongs to the agency (tenant isolation)."""
    r = c.execute("SELECT * FROM cases WHERE id=? AND agency_id=?", (case_id, agency_id)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["notes"] = json.loads(d["notes"])
    return d


def facts_of(c, case_id):
    return {r["key"]: {"value": json.loads(r["value"]), "source": r["source"], "status": r["status"]}
            for r in c.execute("SELECT * FROM facts WHERE case_id=?", (case_id,))}


def set_fact(c, case_id, key, value, status, source=""):
    if value in (None, "", False):
        c.execute("DELETE FROM facts WHERE case_id=? AND key=?", (case_id, key))
        return
    c.execute("INSERT INTO facts (case_id,key,value,source,status,updated) VALUES (?,?,?,?,?,?) "
              "ON CONFLICT(case_id,key) DO UPDATE SET value=excluded.value, source=excluded.source, "
              "status=excluded.status, updated=excluded.updated",
              (case_id, key, json.dumps(value), source, status, time.time()))


def case_forms(c, case_id):
    return [r["template_key"] for r in c.execute("SELECT template_key FROM case_forms WHERE case_id=? ORDER BY id", (case_id,))]


def touch(c, case_id):
    c.execute("UPDATE cases SET updated=? WHERE id=?", (time.time(), case_id))


def latest_job(c, case_id):
    r = c.execute("SELECT * FROM jobs WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
    return dict(r) if r else None
