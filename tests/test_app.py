"""End-to-end tests with the offline AI stand-in (ACORD_AI=fake) and jobs run inline."""
import importlib
import re
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "tests" / "fixtures" / "questionnaire.xlsx"  # fictional client, same layout as a real questionnaire


def make_client(tmp_path, monkeypatch, auth="off", signup="on"):
    monkeypatch.setenv("ACORD_DATA", str(tmp_path))
    monkeypatch.setenv("ACORD_SIGNUP", signup)
    monkeypatch.setenv("ACORD_AI", "fake")
    monkeypatch.setenv("ACORD_AUTH", auth)
    monkeypatch.setenv("ACORD_SYNC_JOBS", "1")
    import app.db, app.forms, app.sources, app.ai, app.main
    for m in (app.db, app.forms, app.sources, app.ai, app.main):
        importlib.reload(m)
    return TestClient(app.main.app), app


def csrf(c):
    r = c.get("/help")
    return re.search(r'window.CSRF = "([^"]+)"', r.text if r.status_code == 200 and "CSRF" in r.text else c.get("/login").text).group(1)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return make_client(tmp_path, monkeypatch)


def new_case(c, forms=("125", "127", "137"), with_xlsx=True):
    files = [("files", ("q.xlsx", XLSX.read_bytes(), "application/octet-stream"))] if with_xlsx else []
    data = {"name": "Starlight ProCare Services LLC", "csrf": csrf(c), "forms": list(forms)}
    r = c.post("/cases", data=data, files=files)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def facts(app, cid):
    with app.db.conn() as c:
        return app.db.facts_of(c, cid)


def pdf_values(content):
    doc = fitz.open(stream=content, filetype="pdf")
    return {w.field_name.split(".")[-1].split("[")[0]: w.field_value for p in doc for w in p.widgets()}


def test_excel_status_rules_and_citation_check(env):
    c, app = env
    cid = new_case(c)
    f = facts(app, cid)
    assert f["NamedInsured_FullName_A"]["status"] == "verified"          # value is in the cited cell
    assert f["NamedInsured_MailingAddress_CityName_A"]["status"] == "verified"
    assert f["Driver_BirthDate_A"]["status"] == "confirm"                # row marked Confirm in the questionnaire
    assert f["NamedInsured_Primary_PhoneNumber_A"]["status"] == "review"  # cited cell doesn't hold that value
    assert "Policy_PolicyNumberIdentifier_A" not in f                     # new policy's number: never from the AI


def test_needed_rows_never_reach_the_model(env):
    _, app = env
    text, _, idx = app.sources.read_all([(str(XLSX), "q.xlsx")])
    assert "Questionnaire!B7=DBA" not in text           # Needed row dropped in code
    assert "Questionnaire!B6=Legal business name" in text
    assert idx.row_status.get("Questionnaire!23") == "confirm"


def test_only_usable_facts_go_into_pdf_and_one_fact_fills_every_form(env):
    c, app = env
    cid = new_case(c)
    for key in ("125", "127", "137"):
        v = pdf_values(c.get(f"/cases/{cid}/forms/{key}/pdf").content)
        assert v.get("NamedInsured_FullName_A") == "Starlight ProCare Services LLC", key  # same fact on all three
    v127 = pdf_values(c.get(f"/cases/{cid}/forms/127/pdf").content)
    assert not v127.get("Driver_BirthDate_A")              # 'confirm' fact held back
    # approving releases it; editing changes it everywhere
    c.post(f"/cases/{cid}/facts", json={"approve": ["Driver_BirthDate_A"], "csrf": csrf(c)})
    c.post(f"/cases/{cid}/facts", json={"edits": {"NamedInsured_FullName_A": "Starlight ProCare LLC"}, "csrf": csrf(c)})
    v127 = pdf_values(c.get(f"/cases/{cid}/forms/127/pdf").content)
    assert v127["Driver_BirthDate_A"] == "04/12/1981"
    for key in ("125", "137"):
        assert pdf_values(c.get(f"/cases/{cid}/forms/{key}/pdf").content)["NamedInsured_FullName_A"] == "Starlight ProCare LLC"
    assert facts(app, cid)["NamedInsured_FullName_A"]["status"] == "edited"


def test_edited_facts_survive_a_reread(env):
    c, app = env
    cid = new_case(c)
    c.post(f"/cases/{cid}/facts", json={"edits": {"NamedInsured_FullName_A": "Kept"}, "csrf": csrf(c)})
    r = c.post(f"/cases/{cid}/extract", data={"csrf": csrf(c)})
    assert r.status_code == 200
    assert facts(app, cid)["NamedInsured_FullName_A"]["value"] == "Kept"


def test_producer_settings_fill_every_form(env):
    c, app = env
    c.post("/settings", data={"csrf": csrf(c), "Producer_FullName_A": "Mir Insurance Agency"})
    cid = new_case(c, with_xlsx=False)
    for key in ("125", "127"):
        assert pdf_values(c.get(f"/cases/{cid}/forms/{key}/pdf").content)["Producer_FullName_A"] == "Mir Insurance Agency"


def test_pages_package_verify_missing(env):
    c, app = env
    cid = new_case(c)
    for tab in ("facts", "forms", "sources", "missing"):
        assert c.get(f"/cases/{cid}?tab={tab}").status_code == 200
    assert c.get(f"/cases/{cid}?tab=facts&show=all").status_code == 200
    assert c.get(f"/cases/{cid}/forms/127").status_code == 200
    assert c.get(f"/cases/{cid}/forms/127/page/1.png").headers["content-type"] == "image/png"
    v = c.get(f"/cases/{cid}/forms/125/verify").json()
    assert v["checked"] >= 2 and not v["mismatches"]
    assert c.get(f"/cases/{cid}/package?mode=zip").content[:2] == b"PK"
    merged = fitz.open(stream=c.get(f"/cases/{cid}/package?mode=pdf").content, filetype="pdf")
    assert len(merged) == 4 + 4 + 3
    m = c.get(f"/cases/{cid}/missing").json()
    assert m["total"] > 0 and "Producer" not in m["groups"]
    assert "we still need" in c.post(f"/cases/{cid}/missing/email", json={"groups": m["groups"], "csrf": csrf(c)}).json()["email"]


def test_duplicate_and_delete_keep_files_separate(env, tmp_path):
    c, app = env
    cid = new_case(c)
    r = c.post(f"/cases/{cid}/duplicate", data={"csrf": csrf(c)}, follow_redirects=False)
    nid = int(r.headers["location"].split("/")[-1])
    c.post(f"/cases/{cid}/delete", data={"csrf": csrf(c)})
    assert c.get(f"/cases/{cid}").status_code == 404
    with app.db.conn() as db:
        paths = [r["path"] for r in db.execute("SELECT path FROM case_docs WHERE case_id=?", (nid,))]
    assert paths and all(Path(p).exists() for p in paths)    # the copy kept its own files
    assert facts(app, nid)["NamedInsured_FullName_A"]["value"] == "Starlight ProCare Services LLC"


def test_upload_and_input_rules(env):
    c, _ = env
    r = c.post("/cases", data={"name": "X", "forms": ["125"], "csrf": csrf(c)},
               files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))])
    assert r.status_code == 400
    assert c.post("/cases", data={"name": "X", "csrf": csrf(c)}).status_code == 400          # no forms
    assert c.post("/cases", data={"name": "X", "forms": ["125"], "csrf": "nope"}).status_code == 403
    r = c.post("/templates/import", data={"title": "x", "csrf": csrf(c)}, files=[("file", ("n.pdf", b"nope", "application/pdf"))])
    assert r.status_code == 400


def test_auth_and_tenant_isolation(tmp_path, monkeypatch):
    c1, app = make_client(tmp_path, monkeypatch, auth="on")
    assert c1.get("/", follow_redirects=False).status_code == 303
    c1.post("/register", data={"name": "A", "email": "a@x.com", "password": "password1", "agency": "One", "csrf": csrf(c1)})
    cid = new_case(c1, with_xlsx=False)
    c2 = TestClient(app.main.app)
    c2.post("/register", data={"name": "B", "email": "b@x.com", "password": "password2", "agency": "Two", "csrf": csrf(c2)})
    for url in (f"/cases/{cid}", f"/cases/{cid}/forms/125/pdf", f"/cases/{cid}/package", f"/cases/{cid}/missing"):
        assert c2.get(url).status_code == 404, url
    r = c2.post(f"/cases/{cid}/facts", json={"edits": {"NamedInsured_FullName_A": "x"}, "csrf": csrf(c2)})
    assert r.status_code == 403
    assert "password don" in c2.post("/login", data={"email": "a@x.com", "password": "wrongpass1", "csrf": csrf(c2)}).text


def test_shared_agency_login(tmp_path, monkeypatch):
    """Hosted for one agency: one login from env vars, no public sign-up, password changes on redeploy."""
    monkeypatch.setenv("ACORD_LOGIN_EMAIL", "Office@Agency.com")
    monkeypatch.setenv("ACORD_LOGIN_PASSWORD", "correct horse 1")
    monkeypatch.setenv("ACORD_AGENCY_NAME", "Test Agency")
    c, app = make_client(tmp_path, monkeypatch, auth="on", signup="off")
    assert c.get("/healthz").json() == {"ok": True}
    assert "Create an account" not in c.get("/login").text
    assert c.post("/register", data={"name": "X", "email": "x@x.com", "password": "password1", "agency": "X", "csrf": csrf(c)}).status_code == 404
    r = c.post("/login", data={"email": "office@agency.com", "password": "correct horse 1", "csrf": csrf(c)}, follow_redirects=False)
    assert r.status_code == 303 and c.get("/").status_code == 200 and "Test Agency" in c.get("/").text
    monkeypatch.setenv("ACORD_LOGIN_PASSWORD", "a new password 2")
    c2, _ = make_client(tmp_path, monkeypatch, auth="on", signup="off")
    assert "password don" in c2.post("/login", data={"email": "office@agency.com", "password": "correct horse 1", "csrf": csrf(c2)}).text
    assert c2.post("/login", data={"email": "office@agency.com", "password": "a new password 2", "csrf": csrf(c2)}, follow_redirects=False).status_code == 303


def test_citation_formats_and_duplicate_instances(env):
    _, app = env
    found = lambda t: [(m.group(1), m.group(2) + m.group(3)) for m in app.sources.CELL_RE.finditer(t)]
    assert found("Questionnaire!C18") == [("Questionnaire", "C18")]
    assert found("q.xlsx / sheet 'Questionnaire'!C18") == [("Questionnaire", "C18")]
    assert found("'My Sheet'!$C$8") == [("My Sheet", "C8")]
    assert found("Auto.pdf page 2") == []
    # one cell can't be two drivers: the copy in slot E is dropped, a different driver in slot B is kept
    vals = {"Driver_GivenName_A": "Dana", "Driver_GivenName_E": "Dana", "Driver_GivenName_B": "Sam"}
    src = {"Driver_GivenName_A": "Questionnaire!C18", "Driver_GivenName_E": "q.xlsx / sheet 'Questionnaire'!C18",
           "Driver_GivenName_B": "Questionnaire!C40"}
    assert sorted(app.ai.drop_duplicate_instances(vals, src)) == ["Driver_GivenName_A", "Driver_GivenName_B"]


def test_question_must_match_field(env):
    _, app = env
    rel = lambda key, q: app.sources.related(key, "", {"B1": q, "C1": "x"}, "C1")
    assert not rel("Vehicle_GrossVehicleWeight_A", "Vehicle 1 - Seating capacity")   # right value, wrong field
    assert not rel("Vehicle_FarthestZoneCode_A", "Radius of operation (miles)")
    for key, q in [("Vehicle_SeatingCapacityCount_A", "Vehicle 1 - Seating capacity"), ("Vehicle_ManufacturersName_A", "Vehicle 1 - Make"),
                   ("Vehicle_VINIdentifier_A", "Vehicle 1 - VIN"), ("NamedInsured_Contact_PrimaryEmailAddress_A", "Business email"),
                   ("PriorCoverage_Automobile_InsurerFullName_A", "Current auto carrier"), ("Driver_LicensedStateOrProvinceCode_A", "Driver 1 - DL state")]:
        assert rel(key, q), key


def test_spaced_citations(env):
    _, app = env
    s = "Starlight_ProCare_Questionnaire.xlsx / sheet 'Questionnaire' ! C8"
    assert [(m.group(1), m.group(2) + m.group(3)) for m in app.sources.CELL_RE.finditer(s)] == [("Questionnaire", "C8")]
    assert app.sources.short_source(s) == "Questionnaire!C8"
    assert app.sources.short_source("Auto.pdf page 2") == "Auto.pdf p.2"
    assert app.sources.short_source("q.xlsx / sheet 'Questionnaire' Cell C68") == "Questionnaire!C68"
    text, _, idx = app.sources.read_all([(str(XLSX), "q.xlsx")])
    assert app.sources.check("TX", s, idx, "NamedInsured_MailingAddress_StateOrProvinceCode_A") == "verified"


def test_remove_keeps_wrong_answers_out_and_not_reproposed(env):
    c, app = env
    cid = new_case(c)
    key = "NamedInsured_FullName_A"
    c.post(f"/cases/{cid}/facts", json={"reject": [key], "csrf": csrf(c)})
    assert facts(app, cid)[key]["status"] == "rejected"
    assert not pdf_values(c.get(f"/cases/{cid}/forms/125/pdf").content).get(key)     # out of the PDF
    c.post(f"/cases/{cid}/extract", data={"csrf": csrf(c)})                             # read again
    assert facts(app, cid)[key]["status"] == "rejected"                               # not re-proposed
    assert "Full name" in str(c.get(f"/cases/{cid}/missing").json()["groups"])         # still counts as missing
    c.post(f"/cases/{cid}/facts", json={"approve": [key], "csrf": csrf(c)})            # restore
    assert pdf_values(c.get(f"/cases/{cid}/forms/125/pdf").content)[key] == "Starlight ProCare Services LLC"
    # clearing an AI value by hand also counts as removing it
    c.post(f"/cases/{cid}/facts", json={"edits": {"NamedInsured_MailingAddress_CityName_A": ""}, "csrf": csrf(c)})
    assert facts(app, cid)["NamedInsured_MailingAddress_CityName_A"]["status"] == "rejected"


def test_sources_shown_as_question_and_place(env):
    c, app = env
    idx = app.sources.index_for([(str(XLSX), XLSX.name)])
    d = app.sources.describe("Questionnaire!C8", idx)
    assert (d["quote"], d["where"], d["row"], d["file"]) == ("Business mailing address", "Questionnaire, row 8", "8", XLSX.name)
    d = app.sources.describe("Auto.pdf page 1", idx)
    assert (d["quote"], d["where"], d["page"]) == ("", "Auto.pdf, page 1", "1")
    cid = new_case(c)
    html = c.get(f"/cases/{cid}?tab=facts&show=all").text
    assert "“Legal business name”" in html and "Questionnaire, row 6" in html


def test_files_that_never_name_the_applicant(env):
    _, app = env
    text, _, idx = app.sources.read_all([(str(XLSX), "q.xlsx")])
    assert app.sources.name_words("Starlight ProCare Services LLC") == ["starlight", "procare"]
    assert app.sources.files_about(idx, "Starlight ProCare") == {"q.xlsx"}
    assert app.sources.files_about(idx, "Bluebonnet Care Transport LLC") == set()   # wrong client's file
    assert app.sources.cited_files("Questionnaire!C8", idx) == {"q.xlsx"}


def test_wrong_client_file_facts_need_review(env):
    c, app = env
    files = [("files", ("q.xlsx", XLSX.read_bytes(), "application/octet-stream"))]
    r = c.post("/cases", data={"name": "Someone Else Trucking", "csrf": csrf(c), "forms": ["125"]}, files=files)
    f = facts(app, r.json()["id"])
    assert f["NamedInsured_FullName_A"]["status"] == "review"   # matches its cell, but the file is about Starlight


def test_rename_rechecks_facts(env):
    c, app = env
    files = [("files", ("q.xlsx", XLSX.read_bytes(), "application/octet-stream"))]
    cid = c.post("/cases", data={"name": "Wrong Name LLC", "csrf": csrf(c), "forms": ["125"]}, files=files).json()["id"]
    assert facts(app, cid)["NamedInsured_FullName_A"]["status"] == "review"
    c.post(f"/cases/{cid}/rename", data={"name": "Starlight ProCare Services LLC", "csrf": csrf(c)})
    assert facts(app, cid)["NamedInsured_FullName_A"]["status"] == "verified"


def test_blank_template_for_pdfjs(env):
    c, app = env
    cid = new_case(c, forms=("127",), with_xlsx=False)
    r = c.get(f"/cases/{cid}/forms/127/template.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert c.get(f"/cases/{cid}/forms/125/template.pdf").status_code == 404   # form not in this case
    assert "pdf.min.js" in c.get(f"/cases/{cid}/forms/127").text


def test_form_library_and_picker(env):
    c, app = env
    keys = {f["key"] for f in app.forms.library()}
    assert keys == {"125", "126", "127", "130", "131", "137", "140"}            # the 7 forms, Texas only
    assert app.forms.MANIFEST["137"]["state"] == "TX" and app.forms.MANIFEST["130"]["state"] is None
    html = c.get("/cases/new").text
    assert 'data-line="auto"' in html and 'data-line="garage"' not in html and 'id="state" value="TX"' in html
    cid = new_case(c, forms=("125", "137"), with_xlsx=False)
    assert c.get(f"/cases/{cid}/forms/137").status_code == 200
    assert "137 TX" in c.get("/").text


def test_automatic_dates_and_doc_viewer(env):
    c, app = env
    from datetime import date, timedelta
    cid = new_case(c, forms=("125",))
    v = pdf_values(c.get(f"/cases/{cid}/forms/125/pdf").content)
    with app.db.conn() as conn:
        made = app.main._local_date(conn.execute("SELECT created FROM cases WHERE id=?", (cid,)).fetchone()["created"])
    eff = made + timedelta(days=10)
    assert v["Form_CompletionDate_A"] == made.strftime("%m/%d/%Y")
    assert v["Policy_EffectiveDate_A"] == eff.strftime("%m/%d/%Y")
    assert v["Policy_ExpirationDate_A"] == app.main._plus_one_year(eff).strftime("%m/%d/%Y")
    assert app.main._plus_one_year(date(2028, 2, 29)) == date(2029, 2, 28)
    # agent can still override a date
    c.post(f"/cases/{cid}/facts", json={"edits": {"Policy_EffectiveDate_A": "01/01/2027"}, "csrf": csrf(c)})
    assert pdf_values(c.get(f"/cases/{cid}/forms/125/pdf").content)["Policy_EffectiveDate_A"] == "01/01/2027"
    # document viewer: Excel as a table with the cited row highlighted, and the source links to it
    with app.db.conn() as conn:
        doc_id = conn.execute("SELECT id FROM case_docs WHERE case_id=?", (cid,)).fetchone()["id"]
    page = c.get(f"/cases/{cid}/docs/{doc_id}?sheet=Questionnaire&row=6").text
    assert 'class="hl-row" id="r6"' in page and "Starlight ProCare Services LLC" in page
    assert f"/cases/{cid}/docs/{doc_id}?sheet=Questionnaire&row=6" in c.get(f"/cases/{cid}?tab=facts&show=all").text
    assert c.get(f"/cases/{cid}/docs/{doc_id}/file").status_code == 200
    assert c.get(f"/cases/{cid}/docs/99999").status_code == 404
    assert 'aria-current="page">Forms' in c.get(f"/cases/{cid}").text.replace("\n", "")          # opens on Forms
