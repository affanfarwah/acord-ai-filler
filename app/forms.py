"""Form template registry, field extraction, page rendering and PDF filling/verification."""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

import fitz

from . import db

TEMPLATES_DIR = db.BASE / "templates"
CACHE = db.DATA / "cache"

def _load_manifest():
    """templates/manifest.json: every built-in form with its number, state, edition, category and file."""
    f = TEMPLATES_DIR / "manifest.json"
    return json.loads(f.read_text()) if f.exists() else {}


MANIFEST = _load_manifest()
BUILTIN = {k: v["title"] for k, v in MANIFEST.items()}
CATEGORIES = ["Applications", "Certificates & evidence", "Loss notices", "Policy changes & binders"]
# Applications agents pick most; shown first, the rest sit under "More forms"
COMMON = ["125", "126", "127", "130", "131", "137", "140"]


def builtin_path(key):
    m = MANIFEST.get(key)
    return TEMPLATES_DIR / m["file"] if m else TEMPLATES_DIR / f"acord_{key}.pdf"


def library():
    """Built-in templates present on disk, with the details the form picker needs."""
    out = []
    for k, m in MANIFEST.items():
        if builtin_path(k).exists():
            out.append({"key": k, "title": m["title"], "number": m["number"], "name": m["name"], "state": m["state"],
                        "edition": m["edition"], "category": m["category"], "common": k in COMMON})
    return out


def template_path(c, key, agency_id):
    if key in BUILTIN:
        p = builtin_path(key)
        return p if p.exists() else None
    r = c.execute("SELECT path FROM templates WHERE key=? AND agency_id=?", (key, agency_id)).fetchone()
    return Path(r["path"]) if r else None


def short_name(name):
    """'F[0].P1[0].Producer_FullName_A[0]' -> 'Producer_FullName_A'."""
    last = name.split(".")[-1]
    return re.sub(r"\[\d+\]$", "", last)


@lru_cache(maxsize=512)  # every built-in form fits; parsing a PDF is the slow part
def fields_of(path_str):
    """Fields grouped by PDF field name: [{name, short, type, page, tooltip, rects, options, maxlen}]."""
    doc = fitz.open(path_str)
    by_name = {}
    order = []
    for pno, page in enumerate(doc, 1):
        for w in page.widgets():
            t = {fitz.PDF_WIDGET_TYPE_TEXT: "text", fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
                 fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio", fitz.PDF_WIDGET_TYPE_COMBOBOX: "choice",
                 fitz.PDF_WIDGET_TYPE_LISTBOX: "choice"}.get(w.field_type)
            if not t:
                continue
            f = by_name.get(w.field_name)
            if f is None:
                f = {"name": w.field_name, "short": short_name(w.field_name), "type": t, "page": pno,
                     "tooltip": (w.field_label or "").strip(), "rects": [], "options": [],
                     "maxlen": w.text_maxlen or None}
                by_name[w.field_name] = f
                order.append(f)
            r = [round(v, 1) for v in w.rect]
            opt = None
            if t in ("checkbox", "radio"):
                opt = w.on_state()
            f["rects"].append({"page": pno, "rect": r, "on": opt})
            if t == "radio" and opt not in f["options"]:
                f["options"].append(opt)
            if t == "choice":
                f["options"] = [str(o) for o in (w.choice_values or [])]
    return order


def page_sizes(path_str):
    doc = fitz.open(path_str)
    return [[round(p.rect.width, 1), round(p.rect.height, 1)] for p in doc]


def render_page(path_str, pno, scale=1.5):
    """PNG of one page *without* form-field widgets drawn (we overlay our own inputs)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha1(f"{path_str}:{Path(path_str).stat().st_mtime}:{scale}".encode()).hexdigest()[:12]
    out = CACHE / f"{Path(path_str).stem}_{tag}_p{pno}.png"
    if not out.exists():
        doc = fitz.open(path_str)
        page = doc[pno - 1]
        for w in list(page.widgets()):
            page.delete_widget(w)
        page.get_pixmap(matrix=fitz.Matrix(scale, scale)).save(out)
    return out


def fill_pdf(template, values, out_path, flatten=False):
    """Write `values` (field name -> str/bool) into a copy of the template."""
    doc = fitz.open(str(template))
    for page in doc:
        for w in page.widgets():
            if w.field_name not in values:
                continue
            v = values[w.field_name]
            if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                w.field_value = w.on_state() if v in (True, "true", "True", "Yes", "yes", "on", "On", 1) else "Off"
            elif w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
                w.field_value = True if str(v) == str(w.on_state()) else False
            else:
                s = "" if v is None else str(v)
                if w.text_maxlen:
                    s = s[: w.text_maxlen]
                w.field_value = s
            w.update()
    if flatten:
        doc.bake()
    doc.save(str(out_path), garbage=3, deflate=True)
    doc.close()
    return out_path


def verify_pdf(pdf_path, values, template):
    """Re-read the filled PDF and check every requested value landed. Returns dict."""
    fields = {f["name"]: f for f in fields_of(str(template))}
    doc = fitz.open(str(pdf_path))
    got = {}
    for page in doc:
        for w in page.widgets():
            if w.field_name not in got or (w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON and w.field_value not in (False, "Off", "", None)):
                got[w.field_name] = w.field_value
    checked, bad = 0, []
    for name, want in values.items():
        f = fields.get(name)
        if not f or want in (None, "", False):
            continue
        checked += 1
        have = got.get(name)
        if f["type"] == "checkbox":
            ok = have not in (False, "Off", "", None)
        elif f["type"] == "radio":
            ok = have not in (False, "Off", "", None)
        else:
            s = str(want)[: f["maxlen"]] if f["maxlen"] else str(want)
            ok = (have or "") == s
        if not ok:
            bad.append({"field": name, "wanted": str(want)[:60], "found": str(have)[:60]})
    return {"checked": checked, "ok": checked - len(bad), "mismatches": bad}


def clean_label(tooltip, short):
    """'Enter text: The full name of the producer / agency.' -> 'Full name of the producer / agency'."""
    t = re.sub(r"^(Enter [a-z ]+|Select[^:]*|Check[^:]*|Sign here|Indicate[^:]*):\s*", "", tooltip or "").strip()
    t = re.sub(r"^(The|Indicates)\s+", "", t).strip().rstrip(".")
    t = t.split(". ")[0]
    return (t[:1].upper() + t[1:])[:140] if t else short


DROP_WORDS = {"Identifier", "Indicator", "Question"}


def human_label(short):
    """'NamedInsured_MailingAddress_CityName_A' -> 'Mailing address city name'. Short and scannable."""
    parts = short.split("_")
    body = parts[1:-1] if re.fullmatch(r"[A-Z]{1,2}", parts[-1]) else parts[1:]
    words = []
    for p in body or parts[:1]:
        words += [w for w in re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+", p) if w not in DROP_WORDS]
    txt = " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in words).strip() or short
    return txt[:1].upper() + txt[1:]


QUESTION_RE = re.compile(r'question,?\s*["\u201c]([^"\u201d]+)["\u201d]', re.I)


def field_label(short, tooltip):
    """Label shown to agents. Yes/No fields ('..._Question_AAHCode_A') get their actual question,
    'Are any drivers not covered by workers compensation?', instead of a code."""
    m = QUESTION_RE.search(tooltip or "")
    if m and "Question" in short:
        q = m.group(1).strip()
        return q[:1].upper() + q[1:].lower() if q.isupper() else q
    return human_label(short)


SECTION_NAMES = {  # ACORD data-group names -> what agents call them
    "NamedInsured": "Applicant", "CommercialVehicleLineOfBusiness": "Business auto questions",
    "CommercialPolicy": "Policy questions", "CommercialStructure": "Buildings", "BusinessInformation": "Business details",
    "PriorCoverage": "Prior insurance", "LossHistory": "Losses", "AdditionalInterest": "Lienholders & additional interests",
    "Producer": "Agency", "BuildingOccupancy": "Building use", "CancelNonRenew": "Cancellations & non-renewals",
    "AccidentConviction": "Accidents & violations", "OtherPolicy": "Other policies",
}


def section_of(short):
    """'NamedInsured_MailingAddress_LineOne_A' -> 'Applicant'; unknown groups are split into words."""
    head = short.split("_")[0]
    return SECTION_NAMES.get(head) or re.sub(r"(?<!^)(?=[A-Z])", " ", head)


def instance_of(short):
    """Trailing instance letter: _A, _B... (driver 1, driver 2...)."""
    m = re.search(r"_([A-Z]{1,2})$", short)
    return m.group(1) if m else "A"


def catalog(paths):
    """Fact catalog for a set of templates: short name -> {label, section, type, options, maxlen, forms}."""
    cat = {}
    for key, path in paths:
        for f in fields_of(str(path)):
            e = cat.get(f["short"])
            if e is None:
                q = QUESTION_RE.search(f["tooltip"] or "")
                cat[f["short"]] = {"key": f["short"], "label": field_label(f["short"], f["tooltip"]),
                                   "help": q.group(1).strip() if q else clean_label(f["tooltip"], f["short"]),
                                   "tooltip": f["tooltip"], "section": section_of(f["short"]),
                                   "instance": instance_of(f["short"]), "type": f["type"],
                                   "options": list(f["options"]), "maxlen": f["maxlen"], "forms": [key]}
            else:
                if key not in e["forms"]:
                    e["forms"].append(key)
                for o in f["options"]:
                    if o not in e["options"]:
                        e["options"].append(o)
                if f["maxlen"] and (not e["maxlen"] or f["maxlen"] < e["maxlen"]):
                    e["maxlen"] = f["maxlen"]
    return cat


def values_for(fields, facts, usable):
    """PDF field name -> value, using only facts whose status is usable."""
    out = {}
    for f in fields:
        fact = facts.get(f["short"])
        if fact and fact["status"] in usable:
            out[f["name"]] = fact["value"]
    return out
