"""FastAPI app. Run: .venv/bin/uvicorn app.main:app --reload

A case is one client submission: its source files, the ACORD forms selected for it, and one fact ledger.
Forms are views of the ledger; editing a fact anywhere updates every form that uses it.
"""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import List

import fitz
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import ai, db, forms, sources

app = FastAPI(title="ACORD AI Filler")
db.DATA.mkdir(parents=True, exist_ok=True)
db.init()


def _secret():
    p = db.DATA / "secret.key"
    if not p.exists():
        p.write_text(secrets.token_hex(32))
    return p.read_text()


AUTH_ON = os.environ.get("ACORD_AUTH", "off").lower() == "on"  # off during development; on before launch
SIGNUP_ON = os.environ.get("ACORD_SIGNUP", "off").lower() == "on"  # self-service agency sign-up; off while one agency uses the app
app.add_middleware(SessionMiddleware, secret_key=_secret(), same_site="lax", max_age=60 * 60 * 12,
                   https_only=os.environ.get("ACORD_HTTPS", "off") == "on")  # set on when hosted behind HTTPS
app.mount("/static", StaticFiles(directory=str(db.BASE / "app" / "static")), name="static")
tpl = Jinja2Templates(directory=str(db.BASE / "app" / "views"))
tpl.env.filters["short_source"] = sources.short_source

UPLOADS = db.DATA / "uploads"
MAX_UPLOAD = 25 * 1024 * 1024
ALLOWED = {".pdf", ".xlsx", ".xlsm", ".png", ".jpg", ".jpeg", ".txt", ".csv", ".md", ".eml"}
# Filled by the agent or agency settings, never by the AI
# Filled by the agent, agency settings or the app, never by the AI. Policy_* number and dates describe the NEW
# policy: the carrier assigns the number and the agency's rule sets the dates (see _effective_facts). The client's
# current policy belongs in PriorCoverage_*.
AGENT_ONLY = re.compile(r"^(Producer|Insurer|Form)_|Signature|^Policy_(PolicyNumberIdentifier|EffectiveDate|ExpirationDate)_")
EFFECTIVE_AFTER_DAYS = int(os.environ.get("ACORD_EFFECTIVE_AFTER_DAYS", "10"))  # proposed policy starts N days after the client is created
TIMEZONE = os.environ.get("ACORD_TZ", "America/Chicago")  # Dallas, TX
SETTINGS_KEYS = re.compile(r"^Producer_(?!.*(Signature|CustomerIdentifier)).*_A$")
ASSET_V = str(int(time.time()))  # cache-busting stamp for /static URLs, new on each server start
STATE_NAMES = {"AL": "Alabama", "AR": "Arkansas", "AZ": "Arizona", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
               "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "IA": "Iowa", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
               "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "MA": "Massachusetts", "MD": "Maryland", "ME": "Maine",
               "MI": "Michigan", "MN": "Minnesota", "MO": "Missouri", "MS": "Mississippi", "MT": "Montana", "NC": "North Carolina",
               "ND": "North Dakota", "NE": "Nebraska", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
               "NV": "Nevada", "NY": "New York", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
               "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
               "VA": "Virginia", "VT": "Vermont", "WA": "Washington", "WI": "Wisconsin", "WV": "West Virginia", "WY": "Wyoming"}
# Agency settings page: (section, hint, [(ACORD key, label, width, placeholder, autocomplete)])
SETTINGS_LAYOUT = [
    ("Agency", "How your agency is named and licensed.", [
        ("Producer_FullName_A", "Agency name", "full", "Mir Insurance Agency LLC", "organization"),
        ("Producer_NationalIdentifier_A", "National producer number (NPN)", "half", "e.g. 12345678", "off"),
        ("Producer_StateLicenseIdentifier_A", "State license number", "half", "", "off"),
    ]),
    ("Office address", "Your mailing address, as it should print on applications.", [
        ("Producer_MailingAddress_LineOne_A", "Street address", "full", "100 Main St", "address-line1"),
        ("Producer_MailingAddress_LineTwo_A", "Suite, unit or floor", "full", "Suite 200", "address-line2"),
        ("Producer_MailingAddress_CityName_A", "City", "half", "Houston", "address-level2"),
        ("Producer_MailingAddress_StateOrProvinceCode_A", "State", "quarter", "TX", "address-level1"),
        ("Producer_MailingAddress_PostalCode_A", "ZIP", "quarter", "77002", "postal-code"),
    ]),
    ("Main contact", "Who carriers should call or email about a submission.", [
        ("Producer_ContactPerson_FullName_A", "Contact name", "half", "", "name"),
        ("Producer_ContactPerson_EmailAddress_A", "Email", "half", "you@agency.com", "email"),
        ("Producer_ContactPerson_PhoneNumber_A", "Office phone", "third", "(713) 555-0100", "tel"),
        ("Producer_ContactPerson_CellPhoneNumber_A", "Cell phone", "third", "", "tel"),
        ("Producer_FaxNumber_A", "Fax", "third", "", "tel"),
    ]),
    ("People on the forms", "Printed next to the signature lines.", [
        ("Producer_AuthorizedRepresentative_FullName_A", "Authorized representative", "half", "", "name"),
        ("Producer_CustomerServiceRepresentative_FullName_A", "Customer service representative", "half", "", "name"),
    ]),
]
STATUS_LABEL = {db.VERIFIED: "Matches source", db.REVIEW: "Check source", db.CONFIRM: "Client to confirm",
                db.APPROVED: "Approved", db.EDITED: "Entered by you", db.REJECTED: "Removed by you"}


# ---------- auth ----------
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000).hex()}"


def check_pw(pw, stored):
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_pw(pw, salt), stored)


def _dev_user():
    """Auth off: everyone is one local dev user in one dev agency."""
    with db.conn() as c:
        r = c.execute("SELECT * FROM users WHERE email='dev@local'").fetchone()
        if not r:
            cur = c.execute("INSERT INTO agencies (name, invite_code) VALUES ('Dev Agency', ?)", (secrets.token_urlsafe(8),))
            c.execute("INSERT INTO users (agency_id, name, email, pw_hash, is_admin) VALUES (?,?,?,?,1)",
                      (cur.lastrowid, "Dev User", "dev@local", "disabled$disabled"))
            r = c.execute("SELECT * FROM users WHERE email='dev@local'").fetchone()
        return dict(r)


def _shared_login():
    """The agency's one login, from ACORD_LOGIN_EMAIL / ACORD_LOGIN_PASSWORD (set in Railway, shared by its staff).
    Created on first start; changing the password variable and redeploying changes the password."""
    email = os.environ.get("ACORD_LOGIN_EMAIL", "").strip().lower()
    pw = os.environ.get("ACORD_LOGIN_PASSWORD", "")
    if not email or not pw:
        return
    if len(pw) < 12:
        raise RuntimeError("ACORD_LOGIN_PASSWORD must be at least 12 characters")
    with db.conn() as c:
        r = c.execute("SELECT id, pw_hash FROM users WHERE email=?", (email,)).fetchone()
        if r:
            if not check_pw(pw, r["pw_hash"]):
                c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(pw), r["id"]))
            return
        aid = c.execute("INSERT INTO agencies (name, invite_code) VALUES (?,?)",
                        (os.environ.get("ACORD_AGENCY_NAME", "My Agency").strip(), secrets.token_urlsafe(8))).lastrowid
        c.execute("INSERT INTO users (agency_id, name, email, pw_hash, is_admin) VALUES (?,?,?,?,1)",
                  (aid, "Agency login", email, hash_pw(pw)))


if AUTH_ON:
    _shared_login()


def current_user(request):
    if not AUTH_ON:
        return _dev_user()
    uid = request.session.get("uid")
    if not uid:
        return None
    with db.conn() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return dict(r) if r else None


def csrf_token(request):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_hex(16)
    return request.session["csrf"]


def csrf_ok(request, token):
    return bool(token) and hmac.compare_digest(str(token), request.session.get("csrf", ""))


def _agency_name(user):
    """Name shown in the header: the agency name from settings, else the account's name."""
    if not user:
        return ""
    with db.conn() as c:
        r = c.execute("SELECT name, producer FROM agencies WHERE id=?", (user["agency_id"],)).fetchone()
    return (json.loads(r["producer"]).get("Producer_FullName_A") or r["name"]) if r else ""


def page(request, name, **ctx):
    u = current_user(request)
    ctx.setdefault("agency_name", _agency_name(u))
    ctx.update(user=u, csrf=csrf_token(request), ai_ready=ai.is_ready(),
               auth_on=AUTH_ON, signup_on=SIGNUP_ON, status_label=STATUS_LABEL, v=ASSET_V)
    return tpl.TemplateResponse(request, name, ctx)


@app.get("/healthz")
def healthz():
    """Railway's health check: the app is up and the database answers."""
    with db.conn() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not AUTH_ON:
        return RedirectResponse("/", 303)
    return page(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form("")):
    if not csrf_ok(request, csrf):
        return page(request, "login.html", error="Your session expired. Sign in again.")
    with db.conn() as c:
        r = c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    if not r or not check_pw(password, r["pw_hash"]):
        time.sleep(0.5)  # slows password guessing
        return page(request, "login.html", error="That email and password don't match.")
    request.session["uid"] = r["id"]
    db.audit(r["agency_id"], r["id"], "login")
    return RedirectResponse("/", 303)


@app.post("/register")
def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...),
             agency: str = Form(""), invite: str = Form(""), csrf: str = Form("")):
    if not SIGNUP_ON:
        raise HTTPException(404)
    if not csrf_ok(request, csrf):
        return page(request, "login.html", error="Your session expired. Try again.")
    email = email.strip().lower()
    if len(password) < 8 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return page(request, "login.html", error="Use a valid email and a password of at least 8 characters.")
    with db.conn() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return page(request, "login.html", error="That email already has an account. Sign in instead.")
        if invite.strip():
            a = c.execute("SELECT id FROM agencies WHERE invite_code=?", (invite.strip(),)).fetchone()
            if not a:
                return page(request, "login.html", error="That invite code isn't valid. Ask your agency admin for a new one.")
            aid, admin = a["id"], 0
        else:
            if not agency.strip():
                return page(request, "login.html", error="Enter your agency's name, or an invite code to join one.")
            aid = c.execute("INSERT INTO agencies (name, invite_code) VALUES (?,?)",
                            (agency.strip(), secrets.token_urlsafe(8))).lastrowid
            admin = 1
        uid = c.execute("INSERT INTO users (agency_id, name, email, pw_hash, is_admin) VALUES (?,?,?,?,?)",
                        (aid, name.strip(), email, hash_pw(password), admin)).lastrowid
    request.session["uid"] = uid
    db.audit(aid, uid, "register")
    return RedirectResponse("/", 303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    if csrf_ok(request, csrf):
        request.session.clear()
    return RedirectResponse("/login", 303)


# ---------- helpers ----------
def _library(c, agency_id):
    lib = forms.library()
    lib += [{"key": r["key"], "title": r["title"], "number": "", "name": r["title"], "state": None, "edition": "",
             "category": "Your agency's forms", "common": True}
            for r in c.execute("SELECT key, title FROM templates WHERE agency_id=?", (agency_id,))]
    return lib


def _form_number(key, lib):
    x = next((f for f in lib if f["key"] == key), None)
    return (x["number"] + (f" {x['state']}" if x["state"] else "")) if x and x["number"] else "—"


def _title(c, key, agency_id):
    return next((x["title"] for x in _library(c, agency_id) if x["key"] == key), key)


def _case_catalog(c, case_id, agency_id):
    paths = [(k, forms.template_path(c, k, agency_id)) for k in db.case_forms(c, case_id)]
    return forms.catalog([(k, p) for k, p in paths if p])


def _producer(c, agency_id):
    r = c.execute("SELECT producer FROM agencies WHERE id=?", (agency_id,)).fetchone()
    return json.loads(r["producer"]) if r else {}


def _local_date(ts):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(ts, ZoneInfo(TIMEZONE)).date()


def _plus_one_year(d):
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 Feb -> 28 Feb
        return d.replace(year=d.year + 1, day=28)


def _automatic_facts(created_ts):
    """Dates the agency's rules decide, from the day the client was created (Dallas, TX time):
    form date = that day, proposed effective = +10 days, proposed expiration = effective + 1 year."""
    from datetime import timedelta
    made = _local_date(created_ts)
    eff = made + timedelta(days=EFFECTIVE_AFTER_DAYS)
    fmt = lambda d: d.strftime("%m/%d/%Y")
    return {
        "Form_CompletionDate_A": (fmt(made), "Date the client was created (Dallas, TX time)"),
        "Policy_EffectiveDate_A": (fmt(eff), f"Automatic: {EFFECTIVE_AFTER_DAYS} days after the client was created"),
        "Policy_ExpirationDate_A": (fmt(_plus_one_year(eff)), "Automatic: 1 year after the proposed effective date"),
    }


def _effective_facts(c, case_id, agency_id):
    """Case facts, plus agency producer settings and the automatic dates for anything the case doesn't set."""
    facts = db.facts_of(c, case_id)
    for k, v in _producer(c, agency_id).items():
        if v and k not in facts:
            facts[k] = {"value": v, "source": "Agency settings", "status": db.EDITED}
    row = c.execute("SELECT created FROM cases WHERE id=?", (case_id,)).fetchone()
    if row:
        for k, (v, why) in _automatic_facts(row["created"]).items():
            if k not in facts or facts[k]["status"] == db.REJECTED:
                facts[k] = {"value": v, "source": why, "status": db.EDITED}
    return facts


def _save_uploads(files, case_id, agency_id):
    d = UPLOADS / str(agency_id) / str(case_id)
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        if not f.filename:
            continue
        if Path(f.filename).suffix.lower() not in ALLOWED:
            raise ValueError(f"{f.filename}: this file type isn't supported. Use Excel, PDF, image or text files.")
        data = f.file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise ValueError(f"{f.filename} is over 25 MB.")
        p = d / f"{secrets.token_hex(4)}_{re.sub(r'[^A-Za-z0-9._-]', '_', Path(f.filename).name)}"
        p.write_bytes(data)
        out.append((p, f.filename))
    return out


def _add_docs(c, case_id, saved):
    for p, fn in saved:
        c.execute("INSERT INTO case_docs (case_id, filename, path, added) VALUES (?,?,?,?)", (case_id, fn, str(p), time.time()))


def _job_update(job_id, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    with db.conn() as c:
        c.execute(f"UPDATE jobs SET {sets}, updated=? WHERE id=?", (*kw.values(), time.time(), job_id))


def _extract(case_id, agency_id, job_id, instructions):
    """Background job: read every source once, propose facts for every selected form, check citations."""
    try:
        with db.conn() as c:
            cat = _case_catalog(c, case_id, agency_id)
            docs = [(r["path"], r["filename"]) for r in c.execute("SELECT * FROM case_docs WHERE case_id=?", (case_id,))]
            existing = db.facts_of(c, case_id)
        text, images, idx = sources.read_all(docs)
        # Agent-entered, approved and removed facts are never re-extracted or overwritten
        locked = {k for k, f in existing.items() if f["status"] in (db.EDITED, db.APPROVED, db.REJECTED)}
        entries = [e for e in cat.values() if e["key"] not in locked and not AGENT_ONLY.search(e["key"])]
        res = ai.extract(entries, text, images, instructions,
                         on_progress=lambda d, t: _job_update(job_id, done=d, total=t))
        with db.conn() as c:
            applicant = c.execute("SELECT name FROM cases WHERE id=?", (case_id,)).fetchone()["name"]
            about = sources.files_about(idx, applicant)
            elsewhere = set()
            for k, v in res["values"].items():
                src = res["sources"].get(k, "")
                e = cat.get(k, {})
                status = sources.check(v, src, idx, k, e.get("help", ""))
                # Right value, wrong person: a file that never names the applicant may be someone else's
                files = sources.cited_files(src, idx)
                if status == db.VERIFIED and files and not (files & about):
                    status = db.REVIEW
                    elsewhere |= files
                db.set_fact(c, case_id, k, v, status, src)
            notes = [f"{f} never mentions {applicant}. Facts taken from it are marked Check source: make sure they "
                     "belong to this applicant, or remove the file." for f in sorted(elsewhere)] + res["notes"]
            c.execute("UPDATE cases SET notes=?, updated=? WHERE id=?", (json.dumps(notes[:30]), time.time(), case_id))
        msg = f"Found {len(res['values'])} facts"
        if res.get("failed"):
            msg += f". {res['failed']} of {res['total']} sections failed; read the documents again to retry them"
        _job_update(job_id, status="done", message=msg)
    except (ai.AIUnavailable, ai.AIError) as e:
        _job_update(job_id, status="failed", message=str(e))
    except Exception as e:  # keep the case; the agent can run it again
        _job_update(job_id, status="failed", message=f"Reading the documents failed ({type(e).__name__}). Try again.")


def _start_extract(c, case_id, agency_id, instructions):
    running = c.execute("SELECT 1 FROM jobs WHERE case_id=? AND status='running'", (case_id,)).fetchone()
    if running:
        return None
    now = time.time()
    job_id = c.execute("INSERT INTO jobs (case_id, status, created, updated) VALUES (?, 'running', ?, ?)",
                       (case_id, now, now)).lastrowid
    c.commit()
    if os.environ.get("ACORD_SYNC_JOBS") == "1":  # tests
        _extract(case_id, agency_id, job_id, instructions)
    else:
        threading.Thread(target=_extract, args=(case_id, agency_id, job_id, instructions), daemon=True).start()
    return job_id


def _case_or_404(request, case_id):
    u = current_user(request)
    if not u:
        return None, None, None
    c = db.conn()
    case = db.case_row(c, case_id, u["agency_id"])
    if not case:
        c.close()
        return u, None, None
    return u, case, c


def _stats(cat, facts):
    used = [k for k in cat if k in facts and facts[k]["status"] != db.REJECTED]
    pending = [k for k in used if facts[k]["status"] in db.PENDING]
    return {"total": len(cat), "filled": len(used), "pending": len(pending)}


# ---------- dashboard ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = "", archived: int = 0):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", 303)
    with db.conn() as c:
        rows = c.execute("SELECT c.*, u.name AS owner FROM cases c JOIN users u ON u.id=c.user_id "
                         "WHERE c.agency_id=? AND c.archived=? AND c.name LIKE ? ORDER BY c.updated DESC",
                         (u["agency_id"], 1 if archived else 0, f"%{q}%")).fetchall()
        cases, lib = [], _library(c, u["agency_id"])
        for r in rows:
            cat = _case_catalog(c, r["id"], u["agency_id"])
            facts = db.facts_of(c, r["id"])
            job = db.latest_job(c, r["id"])
            cases.append({"id": r["id"], "name": r["name"], "owner": r["owner"],
                          "forms": [_form_number(k, lib) for k in db.case_forms(c, r["id"])],
                          "updated": time.strftime("%b %-d", time.localtime(r["updated"])),
                          "stats": _stats(cat, facts), "job": job["status"] if job else None})
    return page(request, "home.html", cases=cases, q=q, archived=archived)


@app.get("/cases/new", response_class=HTMLResponse)
def new_case_page(request: Request):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", 303)
    with db.conn() as c:
        lib = _library(c, u["agency_id"])
        has_producer = bool(_producer(c, u["agency_id"]))
    states = sorted({(f["state"], STATE_NAMES.get(f["state"], f["state"])) for f in lib if f["state"]}, key=lambda s: s[1])
    cats = forms.CATEGORIES + ["Your agency's forms"]
    return page(request, "new_case.html", library=lib, has_producer=has_producer, states=states, categories=cats)


@app.post("/cases")
async def create_case(request: Request, name: str = Form(...), instructions: str = Form(""), csrf: str = Form(""),
                      forms_: List[str] = Form([], alias="forms"), files: List[UploadFile] = File(default=[])):
    u = current_user(request)
    if not u or not csrf_ok(request, csrf):
        return JSONResponse({"error": "Your session expired. Reload the page."}, 403)
    if not name.strip():
        return JSONResponse({"error": "Enter the client's business name."}, 400)
    with db.conn() as c:
        keys = [k for k in forms_ if forms.template_path(c, k, u["agency_id"])]
        if not keys:
            return JSONResponse({"error": "Pick at least one form."}, 400)
        now = time.time()
        cid = c.execute("INSERT INTO cases (agency_id,user_id,name,instructions,created,updated) VALUES (?,?,?,?,?,?)",
                        (u["agency_id"], u["id"], name.strip()[:160], instructions.strip(), now, now)).lastrowid
        for k in keys:
            c.execute("INSERT OR IGNORE INTO case_forms (case_id, template_key) VALUES (?,?)", (cid, k))
        try:
            _add_docs(c, cid, _save_uploads(files, cid, u["agency_id"]))
        except ValueError as e:
            c.execute("DELETE FROM cases WHERE id=?", (cid,))
            c.execute("DELETE FROM case_forms WHERE case_id=?", (cid,))
            return JSONResponse({"error": str(e)}, 400)
        c.commit()
        if c.execute("SELECT 1 FROM case_docs WHERE case_id=?", (cid,)).fetchone() and ai.is_ready():
            _start_extract(c, cid, u["agency_id"], instructions)
    db.audit(u["agency_id"], u["id"], "case_created", f"case={cid} forms={','.join(keys)}")
    return {"id": cid}


# ---------- case workspace ----------
@app.get("/cases/{cid}", response_class=HTMLResponse)
def case_page(request: Request, cid: int, tab: str = "forms", show: str = "review"):
    u, case, c = _case_or_404(request, cid)
    if not u:
        return RedirectResponse("/login", 303)
    if not case:
        return HTMLResponse("Not found", 404)
    with c:
        cat = _case_catalog(c, cid, u["agency_id"])
        facts = _effective_facts(c, cid, u["agency_id"])
        keys = db.case_forms(c, cid)
        lib = _library(c, u["agency_id"])
        doc_paths = [dict(r) for r in c.execute("SELECT id, filename, path, added FROM case_docs WHERE case_id=? ORDER BY id", (cid,))]
        docs = [{k: d[k] for k in ("id", "filename", "added")} for d in doc_paths]
        job = db.latest_job(c, cid)
        form_list = []
        for k in keys:
            p = forms.template_path(c, k, u["agency_id"])
            fs = forms.fields_of(str(p))
            shorts = {f["short"] for f in fs}
            ready = sum(1 for s in shorts if s in facts and facts[s]["status"] in db.USABLE)
            pend = sum(1 for s in shorts if s in facts and facts[s]["status"] in db.PENDING)
            form_list.append({"key": k, "title": _title(c, k, u["agency_id"]), "no": _form_number(k, lib), "fields": len(shorts),
                              "ready": ready, "pending": pend, "pages": len(forms.page_sizes(str(p)))})
    c.close()
    # Facts tab: group filled facts by section + instance
    doc_ids = {d["filename"]: d["id"] for d in doc_paths}
    try:
        idx = sources.index_for([(d["path"], d["filename"]) for d in doc_paths])
    except Exception:  # an unreadable file shouldn't break the page; sources just show less detail
        idx = None
    groups = {}
    for k, e in cat.items():
        f = facts.get(k)
        if not f:
            continue
        if show == "review" and f["status"] not in db.PENDING:
            continue
        g = e["section"] + ("" if e["instance"] == "A" else f" {ord(e['instance'][0]) - 64}")
        src = sources.describe(f["source"], idx)
        src["doc_id"] = doc_ids.get(src["file"])
        groups.setdefault(g, []).append({**e, **f, "src": src})
    stats = _stats(cat, facts)
    return page(request, "case.html", case=case, tab=tab, show=show, groups=groups, stats=stats, forms=form_list,
                docs=docs, job=job, library=[x for x in lib if x["key"] not in keys])


@app.get("/cases/{cid}/job")
def job_status(request: Request, cid: int):
    u, case, c = _case_or_404(request, cid)
    if not case:
        return JSONResponse({"error": "Not found"}, 404)
    with c:
        job = db.latest_job(c, cid)
    c.close()
    return job or {"status": "none"}


@app.post("/cases/{cid}/extract")
async def extract(request: Request, cid: int, instructions: str = Form(""), csrf: str = Form(""),
                  files: List[UploadFile] = File(default=[])):
    u, case, c = _case_or_404(request, cid)
    if not case or not csrf_ok(request, csrf):
        return JSONResponse({"error": "Not allowed"}, 403)
    with c:
        try:
            _add_docs(c, cid, _save_uploads(files, cid, u["agency_id"]))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 400)
        if not ai.is_ready():
            return JSONResponse({"error": "AI is off. Add GEMINI_API_KEY or ANTHROPIC_API_KEY to the .env file."}, 503)
        if instructions.strip():
            c.execute("UPDATE cases SET instructions=? WHERE id=?", (instructions.strip(), cid))
        c.commit()
        job = _start_extract(c, cid, u["agency_id"], instructions.strip() or case["instructions"])
    c.close()
    if job is None:
        return JSONResponse({"error": "The documents are already being read."}, 409)
    db.audit(u["agency_id"], u["id"], "case_extract", f"case={cid}")
    return {"job": job}


@app.post("/cases/{cid}/facts")
async def update_facts(request: Request, cid: int):
    """Body: {edits: {key: value}, approve: [key], reject: [key], csrf}.

    Edits become 'edited'. Approve keeps the value (and restores a removed one). Reject, or clearing an
    AI value, marks it 'rejected': out of the PDFs, and not proposed again on the next read."""
    u, case, c = _case_or_404(request, cid)
    body = await request.json()
    if not case or not csrf_ok(request, body.get("csrf")):
        return JSONResponse({"error": "Not allowed"}, 403)
    with c:
        cat = _case_catalog(c, cid, u["agency_id"])
        facts = db.facts_of(c, cid)
        n = 0
        for k, v in (body.get("edits") or {}).items():
            e = cat.get(k)
            if not e:
                continue
            if e["type"] == "checkbox":
                v = bool(v)
            elif isinstance(v, str):
                v = v.strip()[: e["maxlen"]] if e["maxlen"] else v.strip()
            if v in ("", False) and k in facts and facts[k]["source"] != "Entered by agent":
                db.set_fact(c, cid, k, facts[k]["value"], db.REJECTED, facts[k]["source"])  # cleared an AI value
            else:
                db.set_fact(c, cid, k, v, db.EDITED, "Entered by agent")
            n += 1
        for k in body.get("approve") or []:
            if k in facts and facts[k]["status"] in (db.REVIEW, db.CONFIRM, db.VERIFIED, db.REJECTED):
                db.set_fact(c, cid, k, facts[k]["value"], db.APPROVED, facts[k]["source"])
                n += 1
        for k in body.get("reject") or []:
            if k in facts and facts[k]["status"] != db.EDITED:
                db.set_fact(c, cid, k, facts[k]["value"], db.REJECTED, facts[k]["source"])
                n += 1
        db.touch(c, cid)
    c.close()
    db.audit(u["agency_id"], u["id"], "facts_updated", f"case={cid} count={n}")
    return {"updated": n}


@app.post("/cases/{cid}/forms")
def add_form(request: Request, cid: int, template: str = Form(...), csrf: str = Form("")):
    u, case, c = _case_or_404(request, cid)
    if case and csrf_ok(request, csrf):
        with c:
            if forms.template_path(c, template, u["agency_id"]):
                c.execute("INSERT OR IGNORE INTO case_forms (case_id, template_key) VALUES (?,?)", (cid, template))
                db.touch(c, cid)
        c.close()
    return RedirectResponse(f"/cases/{cid}?tab=forms", 303)


@app.post("/cases/{cid}/forms/{key}/remove")
def remove_form(request: Request, cid: int, key: str, csrf: str = Form("")):
    u, case, c = _case_or_404(request, cid)
    if case and csrf_ok(request, csrf):
        with c:
            c.execute("DELETE FROM case_forms WHERE case_id=? AND template_key=?", (cid, key))
        c.close()
    return RedirectResponse(f"/cases/{cid}?tab=forms", 303)


# ---------- form view / editor ----------
def _form_ctx(request, cid, key):
    u, case, c = _case_or_404(request, cid)
    if not case:
        return None
    with c:
        if key not in db.case_forms(c, cid):
            return None
        tp = forms.template_path(c, key, u["agency_id"])
        facts = _effective_facts(c, cid, u["agency_id"])
        title = _title(c, key, u["agency_id"])
    c.close()
    return {"user": u, "case": case, "tp": tp, "fields": forms.fields_of(str(tp)), "facts": facts, "title": title}


@app.get("/cases/{cid}/forms/{key}", response_class=HTMLResponse)
def form_page(request: Request, cid: int, key: str):
    x = _form_ctx(request, cid, key)
    if not x:
        return HTMLResponse("Not found", 404)
    fields = [{"name": f["name"], "short": f["short"], "type": f["type"], "rects": f["rects"], "options": f["options"],
               "maxlen": f["maxlen"], "label": forms.field_label(f["short"], f["tooltip"]), "help": forms.clean_label(f["tooltip"], f["short"]),
               "agent_only": bool(AGENT_ONLY.search(f["short"]))} for f in x["fields"]]
    shorts = {f["short"] for f in x["fields"]}
    facts = {k: v for k, v in x["facts"].items() if k in shorts and v["status"] != db.REJECTED}
    data = {"fields": fields, "facts": facts, "sizes": forms.page_sizes(str(x["tp"])), "scale": 1.5,
            "usable": sorted(db.USABLE), "labels": STATUS_LABEL}
    return page(request, "form.html", case=x["case"], key=key, title=x["title"], data=data)


@app.get("/cases/{cid}/forms/{key}/template.pdf")
def template_pdf(request: Request, cid: int, key: str):
    """The blank form, for the browser's PDF.js to draw. Only for a case (and agency) that uses this form."""
    x = _form_ctx(request, cid, key)
    if not x:
        return Response(status_code=404)
    return FileResponse(x["tp"], media_type="application/pdf", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/cases/{cid}/forms/{key}/page/{n}.png")
def page_png(request: Request, cid: int, key: str, n: int):
    x = _form_ctx(request, cid, key)
    if not x or n < 1 or n > len(forms.page_sizes(str(x["tp"]))):
        return Response(status_code=404)
    return FileResponse(forms.render_page(str(x["tp"]), n), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})


def _render(x, flatten=False):
    """Filled PDF bytes plus verification. Only usable facts are written."""
    vals = forms.values_for(x["fields"], x["facts"], db.USABLE)
    tmp = db.DATA / "tmp"
    tmp.mkdir(exist_ok=True)
    p = tmp / f"{secrets.token_hex(6)}.pdf"
    forms.fill_pdf(x["tp"], vals, p, flatten=flatten)
    check = forms.verify_pdf(p, vals, x["tp"]) if not flatten else None
    data = p.read_bytes()
    p.unlink()
    return data, check


def _fname(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


@app.get("/cases/{cid}/forms/{key}/pdf")
def form_pdf(request: Request, cid: int, key: str, flatten: int = 0):
    x = _form_ctx(request, cid, key)
    if not x:
        return Response(status_code=404)
    data, _ = _render(x, bool(flatten))
    db.audit(x["user"]["agency_id"], x["user"]["id"], "form_downloaded", f"case={cid} form={key}")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{_fname(x["case"]["name"])}_ACORD_{key}.pdf"'})


@app.get("/cases/{cid}/forms/{key}/verify")
def form_verify(request: Request, cid: int, key: str):
    x = _form_ctx(request, cid, key)
    if not x:
        return JSONResponse({"error": "Not found"}, 404)
    return _render(x)[1]


@app.get("/cases/{cid}/package")
def package(request: Request, cid: int, mode: str = "zip"):
    u, case, c = _case_or_404(request, cid)
    if not case:
        return Response(status_code=404)
    with c:
        keys = db.case_forms(c, cid)
    c.close()
    zbuf, merged = io.BytesIO(), fitz.open()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        for k in keys:
            x = _form_ctx(request, cid, k)
            data, _ = _render(x, flatten=(mode == "pdf"))
            if mode == "zip":
                zf.writestr(f"{_fname(case['name'])}_ACORD_{k}.pdf", data)
            else:
                with fitz.open(stream=data, filetype="pdf") as d:
                    merged.insert_pdf(d)
    db.audit(u["agency_id"], u["id"], "package_downloaded", f"case={cid} mode={mode}")
    if mode == "zip":
        return Response(zbuf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{_fname(case["name"])}_ACORD.zip"'})
    return Response(merged.tobytes(garbage=3, deflate=True), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{_fname(case["name"])}_ACORD.pdf"'})


# ---------- missing info ----------
@app.get("/cases/{cid}/missing")
def missing(request: Request, cid: int):
    u, case, c = _case_or_404(request, cid)
    if not case:
        return JSONResponse({"error": "Not found"}, 404)
    with c:
        cat = _case_catalog(c, cid, u["agency_id"])
        facts = _effective_facts(c, cid, u["agency_id"])
    c.close()
    groups = {}
    for k, e in cat.items():
        # Only first instances: asking about Vehicle 4 when there's one vehicle is noise
        filled = k in facts and facts[k]["status"] != db.REJECTED  # a removed answer is still missing
        if filled or AGENT_ONLY.search(k) or e["type"] != "text" or e["instance"] != "A":
            continue
        groups.setdefault(e["section"], []).append(e["label"])
    return {"groups": {g: v[:20] for g, v in groups.items()}, "total": sum(len(v) for v in groups.values())}


@app.post("/cases/{cid}/missing/email")
async def missing_email(request: Request, cid: int):
    u, case, c = _case_or_404(request, cid)
    body = await request.json()
    if not case or not csrf_ok(request, body.get("csrf")):
        return JSONResponse({"error": "Not allowed"}, 403)
    c.close()
    groups = {str(k): [str(i) for i in v][:20] for k, v in (body.get("groups") or {}).items() if isinstance(v, list)}
    return {"email": ai.missing_email(case["name"], groups, body.get("notes", ""))}


def _recheck(c, case_id, agency_id):
    """Re-run the source checks on stored AI facts (no AI call): after a rename, or a checker improvement.
    Agent decisions (approved, entered, removed) are left alone."""
    case = c.execute("SELECT name, notes FROM cases WHERE id=?", (case_id,)).fetchone()
    docs = [(r["path"], r["filename"]) for r in c.execute("SELECT * FROM case_docs WHERE case_id=?", (case_id,))]
    idx = sources.read_all([(p, f) for p, f in docs if Path(p).exists()])[2]
    cat = _case_catalog(c, case_id, agency_id)
    about = sources.files_about(idx, case["name"])
    elsewhere = set()
    for k, f in db.facts_of(c, case_id).items():
        if f["status"] not in (db.VERIFIED, db.REVIEW, db.CONFIRM):
            continue
        if AGENT_ONLY.search(k):  # the AI should never have filled this
            c.execute("DELETE FROM facts WHERE case_id=? AND key=?", (case_id, k))
            continue
        st = sources.check(f["value"], f["source"], idx, k, cat.get(k, {}).get("help", ""))
        files = sources.cited_files(f["source"], idx)
        if st == db.VERIFIED and files and not (files & about):
            st, elsewhere = db.REVIEW, elsewhere | files
        db.set_fact(c, case_id, k, f["value"], st, f["source"])
    notes = [f"{x} never mentions {case['name']}. Facts taken from it are marked Check source: make sure they "
             "belong to this applicant, or remove the file." for x in sorted(elsewhere)]
    notes += [n for n in json.loads(case["notes"]) if "never mentions" not in n]
    c.execute("UPDATE cases SET notes=? WHERE id=?", (json.dumps(notes[:30]), case_id))


@app.post("/cases/{cid}/rename")
def rename(request: Request, cid: int, name: str = Form(...), csrf: str = Form("")):
    u, case, c = _case_or_404(request, cid)
    if case and csrf_ok(request, csrf) and name.strip():
        with c:
            c.execute("UPDATE cases SET name=?, updated=? WHERE id=?", (name.strip()[:160], time.time(), cid))
            _recheck(c, cid, u["agency_id"])
        db.audit(u["agency_id"], u["id"], "case_renamed", f"case={cid}")
    if c:
        c.close()
    return RedirectResponse(f"/cases/{cid}", 303)


# ---------- case actions ----------
@app.post("/cases/{cid}/duplicate")
def duplicate(request: Request, cid: int, csrf: str = Form("")):
    """Copy a case (e.g. for renewal): forms, facts and its own copies of the source files."""
    u, case, c = _case_or_404(request, cid)
    if not case or not csrf_ok(request, csrf):
        return RedirectResponse("/", 303)
    with c:
        now = time.time()
        nid = c.execute("INSERT INTO cases (agency_id,user_id,name,instructions,created,updated) VALUES (?,?,?,?,?,?)",
                        (u["agency_id"], u["id"], case["name"] + " (copy)", case["instructions"], now, now)).lastrowid
        for k in db.case_forms(c, cid):
            c.execute("INSERT INTO case_forms (case_id, template_key) VALUES (?,?)", (nid, k))
        for k, f in db.facts_of(c, cid).items():
            db.set_fact(c, nid, k, f["value"], f["status"], f["source"])
        d = UPLOADS / str(u["agency_id"]) / str(nid)
        d.mkdir(parents=True, exist_ok=True)
        for r in c.execute("SELECT filename, path FROM case_docs WHERE case_id=?", (cid,)).fetchall():
            if Path(r["path"]).exists():
                dst = d / Path(r["path"]).name
                shutil.copy2(r["path"], dst)
                c.execute("INSERT INTO case_docs (case_id, filename, path, added) VALUES (?,?,?,?)", (nid, r["filename"], str(dst), now))
    c.close()
    db.audit(u["agency_id"], u["id"], "case_duplicated", f"from={cid} to={nid}")
    return RedirectResponse(f"/cases/{nid}", 303)


@app.post("/cases/{cid}/archive")
def archive(request: Request, cid: int, csrf: str = Form("")):
    u, case, c = _case_or_404(request, cid)
    if case and csrf_ok(request, csrf):
        with c:
            c.execute("UPDATE cases SET archived=1-archived WHERE id=?", (cid,))
        c.close()
    return RedirectResponse("/", 303)


@app.post("/cases/{cid}/delete")
def delete(request: Request, cid: int, csrf: str = Form("")):
    """Permanently delete the case, its facts and its uploaded files."""
    u, case, c = _case_or_404(request, cid)
    if case and csrf_ok(request, csrf):
        with c:
            for t in ("case_docs", "case_forms", "facts", "jobs"):
                c.execute(f"DELETE FROM {t} WHERE case_id=?", (cid,))
            c.execute("DELETE FROM cases WHERE id=?", (cid,))
        c.close()
        shutil.rmtree(UPLOADS / str(u["agency_id"]) / str(cid), ignore_errors=True)
        db.audit(u["agency_id"], u["id"], "case_deleted", f"case={cid}")
    return RedirectResponse("/", 303)


def _doc_or_none(request, cid, doc_id):
    u, case, c = _case_or_404(request, cid)
    if not case:
        return None, None
    with c:
        r = c.execute("SELECT * FROM case_docs WHERE id=? AND case_id=?", (doc_id, cid)).fetchone()
    c.close()
    if not r or not Path(r["path"]).exists():
        return case, None
    return case, dict(r)


@app.get("/cases/{cid}/docs/{doc_id}", response_class=HTMLResponse)
def view_doc(request: Request, cid: int, doc_id: int, sheet: str = "", row: int = 0, pdf_page: int = Query(0, alias="page")):
    """See an uploaded document: Excel as a table (cited row highlighted), PDFs/images in the browser."""
    case, doc = _doc_or_none(request, cid, doc_id)
    if not doc:
        return HTMLResponse("Not found", 404)
    ext = Path(doc["filename"]).suffix.lower()
    if ext == ".pdf":
        return RedirectResponse(f"/cases/{cid}/docs/{doc_id}/file" + (f"#page={pdf_page}" if pdf_page else ""), 303)
    grid, text = None, None
    if ext in (".xlsx", ".xlsm"):
        grid = sources.sheet_grid(doc["path"])
    elif ext in sources.TEXT_EXT:
        text = Path(doc["path"]).read_text(errors="replace")[:200_000]
    return page(request, "doc.html", case=case, doc=doc, grid=grid, text=text, is_image=ext in sources.IMG_EXT,
                hl_sheet=sheet, hl_row=row)


@app.get("/cases/{cid}/docs/{doc_id}/file")
def doc_file(request: Request, cid: int, doc_id: int):
    case, doc = _doc_or_none(request, cid, doc_id)
    if not doc:
        return Response(status_code=404)
    types = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    ext = Path(doc["filename"]).suffix.lower()
    safe = re.sub(r'[^A-Za-z0-9._ -]', "_", doc["filename"])
    disp = "inline" if ext in types else "attachment"  # Excel/text download; PDFs and images open in the browser
    return FileResponse(doc["path"], media_type=types.get(ext, "application/octet-stream"),
                        headers={"Content-Disposition": f'{disp}; filename="{safe}"', "Cache-Control": "private, no-store"})


@app.post("/cases/{cid}/docs/{doc_id}/delete")
def delete_doc(request: Request, cid: int, doc_id: int, csrf: str = Form("")):
    u, case, c = _case_or_404(request, cid)
    if case and csrf_ok(request, csrf):
        with c:
            r = c.execute("SELECT path FROM case_docs WHERE id=? AND case_id=?", (doc_id, cid)).fetchone()
            if r:
                Path(r["path"]).unlink(missing_ok=True)
                c.execute("DELETE FROM case_docs WHERE id=?", (doc_id,))
        c.close()
    return RedirectResponse(f"/cases/{cid}?tab=sources", 303)


@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    if not current_user(request):
        return RedirectResponse("/login", 303)
    return page(request, "help.html")


# ---------- agency settings & templates ----------
@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, saved: int = 0):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login", 303)
    with db.conn() as c:
        lib = _library(c, u["agency_id"])
        # agency (producer) fields are the same on every ACORD form; the common applications cover them all
        pick = [x for x in lib if x["common"] or x["category"] == "Your agency's forms"]
        cat = forms.catalog([(x["key"], forms.template_path(c, x["key"], u["agency_id"])) for x in pick])
        prod = _producer(c, u["agency_id"])
        ag = dict(c.execute("SELECT * FROM agencies WHERE id=?", (u["agency_id"],)).fetchone())
        custom = [dict(r) for r in c.execute("SELECT key, title FROM templates WHERE agency_id=?", (u["agency_id"],))]
    groups = []
    seen = set()
    for title, hint, items in SETTINGS_LAYOUT:
        rows = []
        for key, label, size, ph, ac in items:
            if key in cat:
                seen.add(key)
                rows.append({"key": key, "label": label, "size": size, "placeholder": ph, "autocomplete": ac,
                             "value": prod.get(key, ""), "forms": cat[key]["forms"]})
        if rows:
            groups.append({"title": title, "hint": hint, "rows": rows})
    # producer fields from imported forms that the layout doesn't know about
    extra = [{"key": k, "label": e["label"], "size": "half", "placeholder": "", "autocomplete": "off",
              "value": prod.get(k, ""), "forms": e["forms"]} for k, e in cat.items() if SETTINGS_KEYS.match(k) and k not in seen]
    if extra:
        groups.append({"title": "Other", "hint": "Other agency fields found on your forms.", "rows": extra})
    return page(request, "settings.html", groups=groups, agency=ag, custom=custom, saved=saved, prod=prod)


@app.post("/settings")
async def save_settings(request: Request):
    u = current_user(request)
    fd = await request.form()
    if not u or not csrf_ok(request, fd.get("csrf")):
        return RedirectResponse("/settings", 303)
    prod = {k: str(v).strip()[:120] for k, v in fd.items() if SETTINGS_KEYS.match(k) and str(v).strip()}
    with db.conn() as c:
        c.execute("UPDATE agencies SET producer=? WHERE id=?", (json.dumps(prod), u["agency_id"]))
    db.audit(u["agency_id"], u["id"], "settings_saved")
    return RedirectResponse("/settings?saved=1", 303)


@app.post("/templates/import")
async def import_template(request: Request, title: str = Form(...), csrf: str = Form(""), file: UploadFile = File(...)):
    u = current_user(request)
    if not u or not csrf_ok(request, csrf):
        return RedirectResponse("/login", 303)
    data = file.file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD or not data.startswith(b"%PDF"):
        return JSONResponse({"error": "Upload a PDF under 25 MB."}, 400)
    d = db.DATA / "templates" / str(u["agency_id"])
    d.mkdir(parents=True, exist_ok=True)
    key = "x" + secrets.token_hex(4)
    p = d / f"{key}.pdf"
    p.write_bytes(data)
    if not forms.fields_of(str(p)):
        p.unlink()
        return JSONResponse({"error": "That PDF has no fillable fields."}, 400)
    with db.conn() as c:
        c.execute("INSERT INTO templates (agency_id,key,title,path) VALUES (?,?,?,?)", (u["agency_id"], key, title.strip()[:120], str(p)))
    db.audit(u["agency_id"], u["id"], "template_imported", f"key={key}")
    return RedirectResponse("/settings", 303)
