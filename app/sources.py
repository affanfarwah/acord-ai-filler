"""Turn uploaded source files into model input, plus an index used to check the model's citations.

Excel questionnaires get special handling in code (not left to the prompt):
- a row whose Status cell says Needed/Missing/TBD is dropped entirely;
- a row whose Status says Confirm is kept but tagged, and facts cited from it start as `confirm`.
"""
import base64
import re
from pathlib import Path

import fitz
import openpyxl

MAX_IMAGE_PAGES = 6
TEXT_EXT = {".txt", ".md", ".csv", ".eml"}
IMG_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
DROP_STATUS = {"needed", "missing", "tbd", "pending", "unknown"}
CONFIRM_STATUS = {"confirm", "to confirm", "unconfirmed", "verify"}


class Index:
    """What the sources actually say, for checking citations: cells, row statuses, page texts."""

    def __init__(self):
        self.cells = {}       # 'Sheet!C8' -> str value   (also 'file::Sheet!C8')
        self.row_status = {}  # 'Sheet!8'  -> 'confirm'
        self.pages = {}       # 'file::3' -> page text
        self.files_text = {}  # file -> all text
        self.rows = {}        # 'Sheet!8' -> {'B8': 'Driver 1 - Date of birth', 'C8': ...}


def _status_col(ws):
    """Find a header cell named Status in the first 10 rows. Returns (header_row, column letter) or None."""
    for row in ws.iter_rows(min_row=1, max_row=10):
        for c in row:
            if isinstance(c.value, str) and c.value.strip().lower() == "status":
                return c.row, c.column_letter
    return None


def _excel(path, name, idx):
    lines = []
    wb = openpyxl.load_workbook(path, data_only=True)  # cached formula results
    for ws in wb:
        st = _status_col(ws)
        lines.append(f"## {name} / sheet '{ws.title}'")
        for row in ws.iter_rows():
            if st and row[0].row > st[0]:
                sc = ws[f"{st[1]}{row[0].row}"].value
                s = str(sc).strip().lower() if sc is not None else ""
                if s in DROP_STATUS:
                    continue
                if s in CONFIRM_STATUS:
                    idx.row_status[f"{ws.title}!{row[0].row}"] = "confirm"
            cells = []
            for c in row:
                if c.value is None or str(c.value).strip() == "":
                    continue
                v = str(c.value).strip()
                ref = f"{ws.title}!{c.coordinate}"
                idx.cells[ref] = v
                idx.cells[f"{name}::{ref}"] = v
                idx.rows.setdefault(f"{ws.title}!{c.row}", {})[c.coordinate] = v
                cells.append(f"{ref}={v}")
            if cells:
                tag = " [UNCONFIRMED]" if idx.row_status.get(f"{ws.title}!{row[0].row}") else ""
                lines.append(" | ".join(cells) + tag)
    text = "\n".join(lines)
    idx.files_text[name] = text
    return text


def _pdf(path, name, idx):
    doc = fitz.open(path)
    parts, imgs = [], []
    for i, page in enumerate(doc, 1):
        t = page.get_text().strip()
        if t:
            idx.pages[f"{name}::{i}"] = t
            parts.append(f"## {name} / page {i}\n{t}")
        elif len(imgs) < MAX_IMAGE_PAGES:  # scanned page: the model reads it as an image
            png = page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4)).tobytes("png")
            imgs.append({"label": f"{name} / page {i}", "media_type": "image/png",
                         "data": base64.b64encode(png).decode()})
    text = "\n\n".join(parts)
    idx.files_text[name] = text
    return text, imgs


def read_source(path, filename, idx):
    """Returns (text, images). Unsupported types raise ValueError."""
    ext = Path(filename).suffix.lower()
    p = str(path)
    if ext in (".xlsx", ".xlsm"):
        return _excel(p, filename, idx), []
    if ext == ".pdf":
        return _pdf(p, filename, idx)
    if ext in TEXT_EXT:
        t = Path(p).read_text(errors="replace")
        idx.files_text[filename] = t
        return f"## {filename}\n{t}", []
    if ext in IMG_EXT:
        data = base64.b64encode(Path(p).read_bytes()).decode()
        return "", [{"label": filename, "media_type": IMG_EXT[ext], "data": data}]
    raise ValueError(f"Unsupported file type: {ext or filename}")


_NAME_NOISE = {"llc", "inc", "corp", "corporation", "company", "co", "ltd", "the", "and", "services", "service",
               "group", "holdings", "enterprises", "of", "dba"}


def name_words(name):
    """Distinctive words of an applicant name, in order: 'Starlight ProCare Services LLC' -> ['starlight', 'procare']."""
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) >= 3 and w not in _NAME_NOISE]


def files_about(idx, applicant):
    """Files whose text names the applicant: its first distinctive word (usually the brand), or most of its words,
    as whole words. 'Care' inside 'ProCare' doesn't count. Facts from other files may describe someone else."""
    words = name_words(applicant)
    if not words:
        return set(idx.files_text)
    out = set()
    for f, t in idx.files_text.items():
        found = [w for w in words if re.search(rf"\b{re.escape(w)}\b", t, re.I)]
        if words[0] in found or len(found) * 2 > len(words):
            out.add(f)
    return out


def cited_files(source, idx):
    """Which uploaded files a citation points into."""
    src = source or ""
    out = {f for f in idx.files_text if f.lower() in src.lower()}
    for sheet, col, row in cell_refs(src):
        out |= {k.split("::")[0] for k in idx.cells if "::" in k and k.endswith(f"::{sheet}!{col}{row}")}
    return out


_INDEX_CACHE = {}


def index_for(docs):
    """Index of a case's documents for showing sources; cached until a file changes."""
    key = tuple((str(p), fn, Path(p).stat().st_mtime if Path(p).exists() else 0) for p, fn in docs)
    if key not in _INDEX_CACHE:
        if len(_INDEX_CACHE) > 32:
            _INDEX_CACHE.clear()
        _INDEX_CACHE[key] = read_all([(p, fn) for p, fn in docs if Path(p).exists()])[2]
    return _INDEX_CACHE[key]


def read_all(docs):
    """docs: list of (path, filename). Returns (text, images, Index)."""
    idx = Index()
    texts, imgs = [], []
    for path, fn in docs:
        t, i = read_source(path, fn, idx)
        if t:
            texts.append(t)
        imgs.extend(i)
    return "\n\n".join(texts), imgs, idx


# ---------- citation checking ----------
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _digits(s):
    return re.sub(r"\D", "", str(s))


def _matches(value, text):
    v, t = _norm(value), _norm(text)
    if not v or not t:
        return False
    # value inside the source, or a source cell that is most of the value (e.g. '742 Mockingbird Ln' -> '742 Mockingbird Ln.')
    if v in t or (t in v and len(t) >= 0.6 * len(v)):
        return True
    d = _digits(value)  # '36,000' vs '36000', '09/28/2026' vs '2026-09-28 00:00:00'
    if len(d) >= 4 and d in _digits(text):
        return True
    return False


# 'Questionnaire!C8', "q.xlsx / sheet 'Questionnaire'!C8", "'My Sheet'!$C$8" -> sheet, column, row
CELL_RE = re.compile(r"'?([A-Za-z0-9_\-][A-Za-z0-9 _\-]*?)'?\s*!\s*\$?([A-Z]{1,3})\$?(\d+)")  # also "'Sheet' ! C8"
PAGE_RE = re.compile(r"page\s*(\d+)", re.I)
SHEET_WORD_RE = re.compile(r"sheet\s*'([^']+)'|sheet\s+([A-Za-z0-9_\-]+)", re.I)
CELL_WORD_RE = re.compile(r"\bcells?\s*\$?([A-Z]{1,3})\$?(\d+)", re.I)


def cell_refs(source):
    """Every (sheet, column, row) a citation points at, in any of the styles models write:
    'Questionnaire!C8', "'Questionnaire' ! C8", "q.xlsx / sheet 'Questionnaire' Cell C8"."""
    src = source or ""
    refs = [(m.group(1).strip(), m.group(2), m.group(3)) for m in CELL_RE.finditer(src)]
    if not refs:
        sm = SHEET_WORD_RE.search(src)
        if sm:
            sheet = (sm.group(1) or sm.group(2)).strip()
            refs = [(sheet, m.group(1).upper(), m.group(2)) for m in CELL_WORD_RE.finditer(src)]
    return refs


# Words that say nothing about which field a question is for
_STOP = {"a", "an", "the", "of", "and", "or", "per", "each", "to", "for", "in", "on", "by", "is", "if", "any",
         "code", "indicator", "identifier", "amount", "count", "number", "no", "yes", "provided", "needed",
         "confirm", "client", "reply", "card", "page", "sheet", "current", "requested"}
# Questionnaire wording -> ACORD wording
_SYNONYMS = {"make": "manufacturer", "manufacturers": "manufacturer", "carrier": "insurer", "auto": "automobile",
             "dl": "license", "licensed": "license", "fein": "tax", "ein": "tax", "tin": "tax", "dob": "birth",
             "hire": "hired", "started": "start", "miles": "radius", "llc": "entity", "type": "entity",
             "deductibles": "deductible", "limits": "limit", "experience": "experience", "um": "uninsured",
             "uim": "underinsured", "pip": "medical", "med": "medical", "seats": "seating", "capacity": "seating"}


def _words(text):
    out = set()
    split = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(text))  # 'VINIdentifier' -> 'VIN Identifier'
    for w in re.findall(r"[a-z]+", split.lower()):
        if len(w) < 2 or w in _STOP:
            continue
        w = _SYNONYMS.get(w, w)
        out.add(w[:5])  # crude stem: 'expiration'/'expires', 'licensed'/'license'
    return out


def related(field_key, field_help, row_cells, cited):
    """Does the questionnaire row the model cited look like it is about this ACORD field?"""
    question = " ".join(v for c, v in row_cells.items() if c != cited)
    if not question.strip():
        return True  # nothing to compare against; don't second-guess
    parts = field_key.split("_")
    section = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", parts[0])  # 'Vehicle', 'Named Insured': too generic to count
    body = " ".join(parts[1:-1] if re.fullmatch(r"[A-Z]{1,2}", parts[-1]) else parts[1:])
    want = _words(body) | _words(field_help)
    want -= _words(section)
    return bool(want & (_words(question) - _words(section)))


def short_source(source):
    """Display form of a citation: "q.xlsx / sheet 'Questionnaire' ! C8" -> 'Questionnaire!C8'."""
    cells = [f"{sh}!{col}{row}" for sh, col, row in cell_refs(source)]
    if cells:
        return ", ".join(dict.fromkeys(cells))
    m = PAGE_RE.search(source or "")
    f = re.search(r"([\w\-. ]+\.(?:pdf|png|jpe?g|txt|eml|csv))", source or "", re.I)
    if f and m:
        return f"{f.group(1).strip()} p.{m.group(1)}"
    return source or ""


def _col_num(col):
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n


def describe(source, idx):
    """How a citation is shown to agents, and where to open it.

    "Questionnaire!C8" -> {'quote': 'Business mailing address', 'where': 'Questionnaire, row 8',
                           'file': 'q.xlsx', 'sheet': 'Questionnaire', 'row': '8', 'page': None}
    "Auto.pdf page 1"  -> {'quote': '', 'where': 'Auto.pdf, page 1', 'file': 'Auto.pdf', 'page': '1', ...}"""
    src = source or ""
    files = sorted(cited_files(src, idx)) if idx else []
    out = {"quote": "", "where": src, "file": files[0] if files else None, "sheet": None, "row": None, "page": None}
    refs = cell_refs(src)
    if refs:
        sheet, col, row = refs[0]
        cells = idx.rows.get(f"{sheet}!{row}", {}) if idx else {}
        # the question is the nearest text cell to the left of the answer (column B in most questionnaires)
        left = [(c, v) for c, v in cells.items()
                if _col_num(re.match(r"[A-Z]+", c).group(0)) < _col_num(col) and not re.fullmatch(r"[\d.\s]+", v)]
        quote = max(left, key=lambda cv: _col_num(re.match(r"[A-Z]+", cv[0]).group(0)))[1] if left else ""
        rows = sorted({r for _, _, r in refs}, key=int)
        out.update(quote=quote[:90], where=f"{sheet}, row{'s' if len(rows) > 1 else ''} {', '.join(rows)}", sheet=sheet, row=row)
        return out
    page = PAGE_RE.search(src)
    f = re.search(r"([\w\-. ]+\.(?:pdf|png|jpe?g|txt|eml|csv|xlsx))", src, re.I)
    if f:
        out.update(where=f.group(1).strip() + (f", page {page.group(1)}" if page else ""), page=page.group(1) if page else None,
                   file=out["file"] or f.group(1).strip())
    return out


def sheet_grid(path, max_rows=2000, max_cols=40):
    """Excel file as display rows for the document viewer: [(sheet, [[row_no, [cell values...]]...], n_cols)]."""
    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    for ws in wb:
        n_cols = min(ws.max_column or 1, max_cols)
        rows = []
        for r in ws.iter_rows(min_row=1, max_row=min(ws.max_row or 1, max_rows), max_col=n_cols):
            vals = ["" if c.value is None else str(c.value) for c in r]
            if any(v.strip() for v in vals):
                rows.append([r[0].row, vals])
        out.append((ws.title, rows, n_cols))
    return out


def check(value, source, idx, field_key=None, field_help=""):
    """Return 'verified', 'confirm' or 'review' for a model value and its citation.

    With field_key, a spreadsheet citation also has to come from a row whose question matches the field:
    the value '6' sitting in 'Seating capacity' is not evidence for 'Gross vehicle weight'."""
    if value is True:  # checkbox: accept if the cited cell/page exists; the agent still sees the source
        value = ""
    src = source or ""
    cited_cell = False
    for sheet, col, row in cell_refs(src):
        cell = idx.cells.get(f"{sheet}!{col}{row}")
        if cell is None:
            continue
        cited_cell = True
        if value == "" or _matches(value, cell):
            if field_key and not related(field_key, field_help, idx.rows.get(f"{sheet}!{row}", {}), f"{col}{row}"):
                return "review"
            return "confirm" if idx.row_status.get(f"{sheet}!{row}") else "verified"
    if cited_cell:
        # The cited cell exists but doesn't hold the value. Even if the value appears elsewhere,
        # the model may have mixed up fields (100 mile radius vs $100,000 limit), so a person checks.
        return "review"
    pm = PAGE_RE.search(src)
    named = [f for f in idx.files_text if f.lower() in src.lower()] or list(idx.files_text)
    for fname in named:
        page = idx.pages.get(f"{fname}::{pm.group(1)}") if pm else None
        text = page if page is not None else idx.files_text[fname]
        if value == "" and page is not None:
            return "verified"
        # Short values ('TX', 'Yes') match almost any document, so only trust them against a cited page
        if value != "" and len(_norm(value)) >= (2 if page is not None else 4) and _matches(value, text):
            return "verified"
    return "review"
