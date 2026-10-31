"""Build the demo: a fictional client questionnaire (input) and the filled ACORD package (output).

Usage: .venv/bin/python tools/make_demo.py

The AI step is replaced by DEMO_ANSWERS, a fixed set of answers with cell citations, so the demo runs
without an API key. Everything after that is the app's real code: Excel rules, citation checking,
fact statuses, agency settings and PDF filling. All people, numbers and the VIN are made up.
"""
import csv
import importlib
import os
import sys
import tempfile
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "demo"
XLSX = OUT / "input" / "Demo_Questionnaire_Bluebonnet_Care.xlsx"

# (question, response, source, status, note). Status drives what the app may use.
SECTIONS = [
    ("BUSINESS INFORMATION", [
        ("Legal business name", "Bluebonnet Care Transport LLC", "Client email", "Provided", ""),
        ("DBA (if any)", None, "", "Needed", "Ask if they trade under another name"),
        ("Business mailing address", "4820 Bluebonnet Ln, Austin, TX 78745", "Client email", "Provided", ""),
        ("Business phone number", "(512) 555-0142", "Client email", "Provided", ""),
        ("Business email", "office@bluebonnetcare.example", "Client email", "Provided", ""),
        ("Website", "www.bluebonnetcare.example", "Client email", "Provided", ""),
        ("Federal Tax ID (FEIN)", "12-3456789", "W-9", "Provided", ""),
        ("Entity type", "LLC", "Business name", "Provided", ""),
        ("Date operations started", "03/15/2019", "Client reply", "Provided", ""),
        ("Description of operations", "Non-emergency medical transportation (NEMT) for Medicaid and private-pay riders in Travis County", "Client reply", "Provided", ""),
        ("Annual revenue", None, "", "Needed", "Ask for last full year"),
    ]),
    ("DRIVER INFORMATION", [
        ("Driver 1 - Full name", "Dana Whitfield", "Client reply", "Provided", "Owner-operator"),
        ("Driver 1 - Date of birth", "04/09/1984", "Client reply", "Confirm", "Read off a photo, confirm with client"),
        ("Driver 1 - DL number", "12345678", "Client reply", "Provided", ""),
        ("Driver 1 - DL state", "TX", "Client reply", "Provided", ""),
        ("Driver 1 - Date of hire", "03/15/2019", "Client reply", "Provided", ""),
        ("Driver 1 - Years of driving experience", "14", "Client reply", "Provided", ""),
    ]),
    ("VEHICLE INFORMATION", [
        ("Vehicle 1 - Year", "2022", "Registration", "Provided", ""),
        ("Vehicle 1 - Make", "Toyota", "Registration", "Provided", ""),
        ("Vehicle 1 - Model", "Sienna", "Registration", "Provided", ""),
        ("Vehicle 1 - VIN", "DEMO0VIN000000001", "Registration", "Provided", "Fictional VIN"),
        ("Vehicle 1 - Cost new", "$38,500", "Bill of sale", "Provided", ""),
        ("Vehicle 1 - Seating capacity", "6", "Client reply", "Provided", ""),
        ("Radius of operation (miles)", "100", "Client reply", "Provided", ""),
    ]),
    ("INSURANCE HISTORY", [
        ("Current auto carrier", "Example Mutual Insurance Co", "Dec page", "Provided", ""),
        ("Current policy number", "EX-4471902", "Dec page", "Provided", ""),
        ("Current policy effective date", "12/01/2025", "Dec page", "Provided", ""),
        ("Current policy expiration date", "12/01/2026", "Dec page", "Provided", ""),
    ]),
    ("REQUESTED COVERAGE", [
        ("Requested effective date", "12/01/2026", "Client reply", "Provided", ""),
        ("Bodily injury liability", "$250,000 each person / $500,000 each accident", "Client reply", "Provided", ""),
        ("Property damage liability", "$100,000 each accident", "Client reply", "Provided", ""),
        ("Uninsured / underinsured motorist", "$100,000 each person / $300,000 each accident", "Client reply", "Provided", ""),
        ("Medical payments", "$5,000 per person", "Client reply", "Confirm", "Client said 'about 5k', confirm"),
        ("Comprehensive deductible", "$500", "Client reply", "Provided", ""),
        ("Collision deductible", "$1,000", "Client reply", "Provided", ""),
    ]),
]


def build_questionnaire():
    """Same layout as the agency's real questionnaire: #, Question, Response, Source, Status, Notes."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Questionnaire"
    ws["A1"] = "Bluebonnet Care Transport LLC - NEMT Commercial Auto Questionnaire (DEMO, fictional data)"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = "Edit column C (Response) and column E (Status). Yellow = needed from client; orange = confirm."
    ws.append([])
    ws.append(["#", "Question / Item", "Response", "Source", "Status", "Notes / What to ask client"])
    for c in ws[4]:
        c.font = Font(bold=True)
    cells = {}
    n = 0
    fill = {"Needed": PatternFill("solid", fgColor="FFF2A8"), "Confirm": PatternFill("solid", fgColor="FFD6A5")}
    for title, rows in SECTIONS:
        ws.append([title])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        for q, resp, src, status, note in rows:
            n += 1
            ws.append([n, q, resp, src, status, note])
            r = ws.max_row
            cells[q] = f"C{r}"
            if status in fill:
                for col in "ABCDEF":
                    ws[f"{col}{r}"].fill = fill[status]
    for col, w in zip("ABCDEF", (5, 44, 52, 22, 11, 40)):
        ws.column_dimensions[col].width = w
    XLSX.parent.mkdir(parents=True, exist_ok=True)
    wb.save(XLSX)
    return cells


def demo_answers(cells):
    """What a model would return: ACORD field -> (value, citation). One citation is deliberately wrong."""
    c = lambda q: f"Questionnaire!{cells[q]}"
    a = {
        "NamedInsured_FullName_A": ("Bluebonnet Care Transport LLC", c("Legal business name")),
        "NamedInsured_MailingAddress_LineOne_A": ("4820 Bluebonnet Ln", c("Business mailing address")),
        "NamedInsured_MailingAddress_CityName_A": ("Austin", c("Business mailing address")),
        "NamedInsured_MailingAddress_StateOrProvinceCode_A": ("TX", c("Business mailing address")),
        "NamedInsured_MailingAddress_PostalCode_A": ("78745", c("Business mailing address")),
        "NamedInsured_Primary_PhoneNumber_A": ("(512) 555-0142", c("Business phone number")),
        "NamedInsured_Contact_PrimaryEmailAddress_A": ("office@bluebonnetcare.example", c("Business email")),
        "NamedInsured_Primary_WebsiteAddress_A": ("www.bluebonnetcare.example", c("Website")),
        "NamedInsured_TaxIdentifier_A": ("12-3456789", c("Federal Tax ID (FEIN)")),
        "NamedInsured_LegalEntity_LimitedLiabilityCorporationIndicator_A": (True, c("Entity type")),
        "NamedInsured_BusinessStartDate_A": ("03/15/2019", c("Date operations started")),
        "CommercialPolicy_OperationsDescription_A": (
            "Non-emergency medical transportation (NEMT) for Medicaid and private-pay riders in Travis County",
            c("Description of operations")),
        "Policy_EffectiveDate_A": ("12/01/2026", c("Requested effective date")),
        "PriorCoverage_Automobile_InsurerFullName_A": ("Example Mutual Insurance Co", c("Current auto carrier")),
        "PriorCoverage_Automobile_PolicyNumberIdentifier_A": ("EX-4471902", c("Current policy number")),
        "PriorCoverage_Automobile_EffectiveDate_A": ("12/01/2025", c("Current policy effective date")),
        "PriorCoverage_Automobile_ExpirationDate_A": ("12/01/2026", c("Current policy expiration date")),
        "Driver_GivenName_A": ("Dana", c("Driver 1 - Full name")),
        "Driver_Surname_A": ("Whitfield", c("Driver 1 - Full name")),
        "Driver_BirthDate_A": ("04/09/1984", c("Driver 1 - Date of birth")),           # Confirm row -> held back
        "Driver_LicenseNumberIdentifier_A": ("12345678", c("Driver 1 - DL number")),
        "Driver_LicensedStateOrProvinceCode_A": ("TX", c("Driver 1 - DL state")),
        "Driver_HiredDate_A": ("03/15/2019", c("Driver 1 - Date of hire")),
        "Driver_ExperienceYearCount_A": ("14", c("Driver 1 - Years of driving experience")),
        "Vehicle_ModelYear_A": ("2022", c("Vehicle 1 - Year")),
        "Vehicle_ManufacturersName_A": ("Toyota", c("Vehicle 1 - Make")),
        "Vehicle_ModelName_A": ("Sienna", c("Vehicle 1 - Model")),
        "Vehicle_VINIdentifier_A": ("DEMO0VIN000000001", c("Vehicle 1 - VIN")),
        "Vehicle_CostNewAmount_A": ("38,500", c("Vehicle 1 - Seating capacity")),     # wrong cell -> 'Check source'
        "Vehicle_SeatingCapacityCount_A": ("6", c("Vehicle 1 - Seating capacity")),
        "Vehicle_RadiusOfUse_A": ("100", c("Radius of operation (miles)")),
        "Vehicle_BodilyInjury_PerPersonLimitAmount_A": ("250,000", c("Bodily injury liability")),
        "Vehicle_BodilyInjury_PerAccidentLimitAmount_A": ("500,000", c("Bodily injury liability")),
        "Vehicle_PropertyDamage_PerAccidentLimitAmount_A": ("100,000", c("Property damage liability")),
        "Vehicle_UninsuredUnderinsuredMotorists_BodilyInjuryPerPersonLimitAmount_A": ("100,000", c("Uninsured / underinsured motorist")),
        "Vehicle_UninsuredUnderinsuredMotorists_BodilyInjuryPerAccidentLimitAmount_A": ("300,000", c("Uninsured / underinsured motorist")),
        "Vehicle_MedicalPayments_PerPersonLimitAmount_A": ("5,000", c("Medical payments")),  # Confirm row
        "Vehicle_Coverage_ComprehensiveIndicator_A": (True, c("Comprehensive deductible")),
        "Vehicle_Coverage_ComprehensiveOrSpecifiedCauseOfLossDeductibleAmount_A": ("500", c("Comprehensive deductible")),
        "Vehicle_Comprehensive_DeductibleAmount_A": ("500", c("Comprehensive deductible")),
        "Vehicle_Coverage_CollisionIndicator_A": (True, c("Collision deductible")),
        "Vehicle_Collision_DeductibleAmount_A": ("1,000", c("Collision deductible")),
    }
    return a


def main():
    cells = build_questionnaire()
    answers = demo_answers(cells)
    tmp = tempfile.mkdtemp()
    os.environ.update(ACORD_DATA=tmp, ACORD_AI="fake", ACORD_AUTH="off", ACORD_SYNC_JOBS="1")
    sys.path.insert(0, str(ROOT))
    import app.ai, app.main
    importlib.reload(app.main)
    from fastapi.testclient import TestClient
    import re

    def scripted_extract(entries, text, images, instructions, on_progress=None):
        keys = {e["key"] for e in entries}
        vals = {k: v for k, (v, _) in answers.items() if k in keys}
        return {"values": app.ai.validate(entries, vals),
                "sources": {k: s for k, (_, s) in answers.items() if k in keys},
                "notes": ["Medical payments: client said 'about 5k'. Confirm the limit before binding."]}

    app.ai.extract = scripted_extract
    c = TestClient(app.main.app)
    token = re.search(r'window.CSRF = "([^"]+)"', c.get("/settings").text).group(1)
    c.post("/settings", data={"csrf": token, "Producer_FullName_A": "Demo Insurance Agency",
                              "Producer_MailingAddress_LineOne_A": "100 Example Plaza, Suite 200",
                              "Producer_MailingAddress_CityName_A": "Houston",
                              "Producer_MailingAddress_StateOrProvinceCode_A": "TX",
                              "Producer_MailingAddress_PostalCode_A": "77002",
                              "Producer_ContactPerson_FullName_A": "Sam Agent",
                              "Producer_ContactPerson_PhoneNumber_A": "(713) 555-0100",
                              "Producer_ContactPerson_EmailAddress_A": "sam@demoagency.example"})
    r = c.post("/cases", data={"name": "Bluebonnet Care Transport LLC", "csrf": token, "forms": ["125", "127", "137"]},
               files=[("files", (XLSX.name, XLSX.read_bytes(), "application/octet-stream"))])
    cid = r.json()["id"]

    out = OUT / "output"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*"):
        old.unlink()
    for k in ("125", "127", "137"):
        (out / f"Bluebonnet_Care_ACORD_{k}.pdf").write_bytes(c.get(f"/cases/{cid}/forms/{k}/pdf").content)
    (out / "Bluebonnet_Care_ACORD_package.pdf").write_bytes(c.get(f"/cases/{cid}/package?mode=pdf").content)

    from app import db, forms
    with db.conn() as conn:
        facts = app.main._effective_facts(conn, cid, 1)
        cat = app.main._case_catalog(conn, cid, 1)
    labels = {db.VERIFIED: "Matches source", db.REVIEW: "Check source", db.CONFIRM: "Client to confirm",
              db.APPROVED: "Approved", db.EDITED: "Entered by you"}
    with open(out / "facts_review.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Section", "Field", "ACORD field", "Value", "Source", "Status", "In PDF", "Forms"])
        for k in sorted(facts, key=lambda k: (cat.get(k, {}).get("section", ""), k)):
            x = facts[k]
            status = "Agency settings" if x["source"] == "Agency settings" else labels[x["status"]]
            w.writerow([cat.get(k, {}).get("section", ""), forms.human_label(k), k, x["value"], x["source"], status,
                        "yes" if x["status"] in db.USABLE else "no - waiting for review",
                        " ".join(cat.get(k, {}).get("forms", []))])
    held = [k for k, x in facts.items() if x["status"] not in db.USABLE]
    print(f"{len(facts)} facts, {len(facts) - len(held)} in PDFs, held back: {held}")
    print(f"Input:  {XLSX}\nOutput: {out}")


if __name__ == "__main__":
    main()
