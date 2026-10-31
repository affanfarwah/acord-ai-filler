"""AI layer: extract ACORD facts from a case's sources, and draft a missing-information email.

Providers: ACORD_AI=claude (ANTHROPIC_API_KEY), gemini (GEMINI_API_KEY), or fake (offline, tests only).
With no ACORD_AI set, whichever key is present is used. Keys can live in the project's .env file.
The model only proposes values; sources.check() decides how far each one can be trusted.
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MAX_FIELDS_PER_CALL = 150      # Claude: several calls in parallel
GEMINI_FIELDS_PER_CALL = 400   # Gemini free tier: fewer, larger calls under tight rate limits

SYSTEM = """You extract facts for ACORD commercial insurance forms from source documents supplied by an insurance agent.

Rules:
- Use only facts explicitly stated in the sources or the agent's instructions. NEVER guess, infer or invent.
- Omit anything missing, unclear, or marked Needed/TBD. Rows tagged [UNCONFIRMED] may be used, but say so in notes.
- Sources may describe a different person or business than the applicant. Follow the agent's instructions about
  which sources apply; if unclear, omit facts about anyone who is not the applicant.
- Field ids ending _A, _B, _C... are instances (first driver, second driver...). Keep each instance consistent.
  Use _A for the first one. Fill _B, _C... ONLY when the sources describe a second, third... separate
  driver/vehicle/location/policy. Never copy the same item into several instances.
- Dates MM/DD/YYYY. Phone (555) 555-5555. Amounts as digits with commas.
- Checkbox fields: true only when the sources clearly say it applies; otherwise omit.
- Fields with options: return exactly one listed option, or omit.
- Respect max length; abbreviate rather than overflow.
- Policy_* fields describe the NEW policy being applied for. The client's current or expiring policy (carrier,
  policy number, dates, premium) goes ONLY in PriorCoverage_* fields, never in Policy_*. Don't copy current
  policy dates into Policy_EffectiveDate/ExpirationDate unless the sources say they are the requested new dates.
- Every value needs evidence in `sources`, written exactly like this: a spreadsheet cell as Questionnaire!C8
  (sheet name, !, cell, no file name, no spaces), or a PDF as Auto.pdf page 2.
- Answer a Yes/No question only if a source directly answers that question. Don't infer it from unrelated rows.
- Put conflicts between sources in `notes`."""

TOOL = {
    "name": "submit_values",
    "description": "Return the field values you can support from the sources.",
    "input_schema": {
        "type": "object",
        "properties": {
            "values": {"type": "object", "description": "field id -> value (string, or true for checkbox)"},
            "sources": {"type": "object", "description": "field id -> evidence reference"},
            "notes": {"type": "array", "items": {"type": "string"}, "description": "Conflicts or doubts"},
        },
        "required": ["values"],
    },
}


class AIUnavailable(Exception):
    """Setup problem (no key, bad key, model gone). Stops the whole read, since every call would fail."""


class AIError(Exception):
    """One call failed (timeout, server error). Other sections can still succeed."""


def _google_message(r):
    try:
        return r.json()["error"]["message"][:240]
    except Exception:
        return f"HTTP {r.status_code}"


def _load_env():
    """Read KEY=VALUE lines from the project's .env (never committed)."""
    f = Path(__file__).resolve().parent.parent / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
CLAUDE_MODEL = os.environ.get("ACORD_MODEL", "claude-sonnet-5-5")
GEMINI_MODEL = os.environ.get("ACORD_GEMINI_MODEL", "gemini-3.5-flash-lite")


def provider():
    p = os.environ.get("ACORD_AI")
    if p:
        return p
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return "claude"


def is_ready():
    p = provider()
    return p == "fake" or bool(os.environ.get("GEMINI_API_KEY" if p == "gemini" else "ANTHROPIC_API_KEY"))


def _field_lines(entries):
    out = []
    for e in entries:
        extra = ""
        if e["options"] and e["type"] in ("radio", "choice"):
            extra = f" options={e['options'][:15]}"
        elif e["maxlen"]:
            extra = f" max={e['maxlen']}"
        tip = re.sub(r"^(Enter [a-z ]+|Select[^:]*|Check[^:]*):\s*", "", e["tooltip"]).strip()[:140]
        out.append(f"{e['key']} [{e['type']}]{extra} :: {tip}")
    return "\n".join(out)


def _chunks(entries, size=MAX_FIELDS_PER_CALL):
    """Keep sections together so the model sees related fields (all of Driver_*, all of Vehicle_*) at once."""
    entries = sorted(entries, key=lambda e: (e["section"], e["instance"], e["key"]))
    for i in range(0, len(entries), size):
        yield entries[i:i + size]


def _known_block(vals):
    """Round 2 context: what the first instances already hold, so later slots only get *different* items."""
    if not vals:
        return ""
    lines = "\n".join(f"{k} = {v}" for k, v in sorted(vals.items()))[:6000]
    return ("ALREADY FILLED (first driver, vehicle, location...). Fill the fields below ONLY for an additional, "
            "different item described in the sources. If there is no second one, return no values.\n"
            f"{lines}\n\n")


def _prompt_parts(text, instructions):
    """The shared prefix (sources + instructions) is identical for every chunk, so providers can cache it."""
    return (f"AGENT INSTRUCTIONS:\n{instructions or '(none)'}\n\nSOURCES:\n{text or '(no text sources)'}",
            "FIELDS TO FILL (id [type] :: meaning):\n")


# ---------- Claude ----------
def _claude_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AIUnavailable("No AI key set. Add GEMINI_API_KEY or ANTHROPIC_API_KEY to the .env file.")
    import anthropic
    return anthropic.Anthropic(api_key=key)


def _claude_chunk(client, entries, text, images, instructions, known=""):
    prefix, head = _prompt_parts(text, instructions)
    content = []
    for im in images:
        content.append({"type": "text", "text": f"[Image source: {im['label']}]"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": im["media_type"], "data": im["data"]}})
    content.append({"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}})
    content.append({"type": "text", "text": known + head + _field_lines(entries)})
    resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=8000, system=SYSTEM, tools=[TOOL],
                                  tool_choice={"type": "tool", "name": "submit_values"},
                                  messages=[{"role": "user", "content": content}])
    for b in resp.content:
        if b.type == "tool_use":
            return b.input
    return {"values": {}}


# ---------- Gemini (plain REST, no SDK) ----------
def _gemini_call(parts, system, max_retries=5):
    import httpx
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise AIUnavailable("GEMINI_API_KEY is not set. Add it to the .env file.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    budget = os.environ.get("ACORD_GEMINI_THINKING_BUDGET")  # '0' turns thinking off: much faster on the free tier
    if budget not in (None, ""):
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": int(budget)}
    for attempt in range(max_retries):
        try:
            r = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=240)
        except httpx.TimeoutException:
            if attempt < max_retries - 1:
                continue
            raise AIError("Gemini took too long to answer.")
        if r.status_code in (429, 500, 503) and attempt < max_retries - 1:  # rate limits / overloaded
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code in (400, 401, 403) and "key" in r.text.lower():
            raise AIUnavailable("Gemini rejected the API key. Check GEMINI_API_KEY in .env.")
        if r.status_code == 404:
            raise AIUnavailable(f"Gemini model '{GEMINI_MODEL}' isn't available: {_google_message(r)} "
                                "Set ACORD_GEMINI_MODEL in .env.")
        if r.status_code >= 400:
            raise AIError(f"Gemini error: {_google_message(r)}")
        break
    cand = (r.json().get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}


def _gemini_chunk(entries, text, images, instructions, known=""):
    prefix, head = _prompt_parts(text, instructions)
    parts = []
    for im in images:
        parts.append({"text": f"[Image source: {im['label']}]"})
        parts.append({"inlineData": {"mimeType": im["media_type"], "data": im["data"]}})
    parts.append({"text": prefix})
    parts.append({"text": known + head + _field_lines(entries) +
                  '\n\nRespond with JSON only: {"values": {id: value}, "sources": {id: evidence}, "notes": [string]}'})
    out = _gemini_call(parts, SYSTEM)
    return out if isinstance(out, dict) else {"values": {}}


# ---------- public ----------
def validate(entries, raw):
    """Keep only known fields with legal values. Returns {key: value}."""
    by = {e["key"]: e for e in entries}
    good = {}
    for k, v in (raw or {}).items():
        e = by.get(k)
        if not e or v in (None, "", False):
            continue
        if e["type"] == "checkbox":
            if v is True or str(v).strip().lower() in ("true", "yes", "on", "x"):
                good[k] = True
        elif e["type"] in ("radio", "choice") and e["options"]:
            if str(v) in [str(o) for o in e["options"]]:
                good[k] = str(v)
        else:
            s = str(v).strip()
            good[k] = s[: e["maxlen"]] if e["maxlen"] else s
    return good


def extract(entries, text, images, instructions, on_progress=None):
    """Propose facts for catalog `entries`. Returns {'values', 'sources', 'notes', 'failed', 'total'}.

    A failed section doesn't lose the others: their facts are kept and the failure is reported.
    Setup problems (AIUnavailable) stop straight away, since every section would fail the same way.
    """
    p = provider()
    size = GEMINI_FIELDS_PER_CALL if p == "gemini" else MAX_FIELDS_PER_CALL
    # Round 1 fills only first instances (_A, and fields with no instance); round 2 the extra slots.
    # Mixing them lets a batch that only holds Vehicle_*_D put the one vehicle there.
    first = [e for e in entries if e["instance"] == "A"]
    more = [e for e in entries if e["instance"] != "A"]
    rounds = [list(_chunks(first, size)), list(_chunks(more, size))]
    total = sum(len(r) for r in rounds)
    done, errors = [0], []
    if on_progress:
        on_progress(0, total)

    def run(call, c):
        try:
            return call(c)
        except AIUnavailable:
            raise
        except Exception as e:  # keep going; report at the end
            errors.append(str(e) if isinstance(e, AIError) else type(e).__name__)
            return {}
        finally:
            done[0] += 1
            if on_progress:
                on_progress(done[0], total)

    results, found = [], {}
    for i, chunks in enumerate(rounds):
        known = _known_block(found) if i else ""
        if not chunks:
            continue
        if p == "fake":
            got = [_fake([e for c in chunks for e in c], text)] if i == 0 else []
        elif p == "gemini":  # free tier: one call at a time
            got = [run(lambda c: _gemini_chunk(c, text, images, instructions, known), c) for c in chunks]
        else:
            client = _claude_client()
            with ThreadPoolExecutor(max_workers=4) as ex:
                got = list(ex.map(lambda c: run(lambda x: _claude_chunk(client, x, text, images, instructions, known), c), chunks))
        for r in got:
            found.update(r.get("values") or {})
        results += got
    if errors and len(errors) == total:
        raise AIError(errors[-1])
    vals, src, notes = {}, {}, []
    for r in results:
        vals.update(r.get("values") or {})
        src.update(r.get("sources") or {})
        notes += [str(n) for n in (r.get("notes") or [])]
    if errors:
        notes.insert(0, f"{len(errors)} of {total} sections couldn't be read ({errors[-1]}). Read the documents again to retry them.")
    vals = drop_duplicate_instances(vals, src)
    notes = list(dict.fromkeys(n.strip() for n in notes if n.strip()))  # same note from several sections
    good = validate(entries, vals)
    return {"values": good, "sources": {k: str(src.get(k, "")) for k in good}, "notes": notes[:30],
            "failed": len(errors), "total": total}


_INSTANCE = re.compile(r"^(.*)_([A-Z]{1,2})$")


def drop_duplicate_instances(vals, src):
    """One source cell is one item: if the same cell fills Driver_GivenName_A and Driver_GivenName_E,
    keep only the earliest instance. Models sometimes copy the single driver into every driver slot."""
    keep, seen = {}, {}
    for k in sorted(vals, key=lambda k: (len(_INSTANCE.match(k).group(2)) if _INSTANCE.match(k) else 0, k[-2:], k)):
        m = _INSTANCE.match(k)
        from .sources import cell_refs
        cells = [f"{sh}!{col}{row}" for sh, col, row in cell_refs(str(src.get(k, "")))]
        if m and cells:
            sig = (m.group(1), tuple(sorted(cells)))
            if sig in seen:
                continue
            seen[sig] = k
        keep[k] = vals[k]
    return keep


FAKE_PICKS = [  # (fact key, sheet!cell, value override) -- offline test stand-in only
    ("NamedInsured_FullName_A", "Questionnaire!C6", None),
    ("NamedInsured_MailingAddress_CityName_A", "Questionnaire!C8", "Frisco"),
    ("Driver_BirthDate_A", "Questionnaire!C23", None),
    ("NamedInsured_Primary_PhoneNumber_A", "Questionnaire!C53", "WRONG-123"),
    ("Policy_PolicyNumberIdentifier_A", "Questionnaire!C53", None),  # must be ignored: new policy number is never AI
]


def _fake(entries, text):
    keys = {e["key"] for e in entries}
    vals, src = {}, {}
    for key, ref, override in FAKE_PICKS:
        m = re.search(re.escape(ref) + r"=([^|\n]+)", text)
        if key in keys and m:
            vals[key] = override or m.group(1).strip().replace(" [UNCONFIRMED]", "")
            src[key] = ref
    return {"values": vals, "sources": src, "notes": []}


def missing_email(insured, groups, notes=""):
    """Draft an email asking the insured for missing info. Falls back to a plain template."""
    lines = [f"{sec}:\n" + "\n".join(f"  - {i}" for i in items) for sec, items in groups.items()]
    body_list = "\n\n".join(lines)
    fallback = (f"Hello,\n\nTo finish the insurance application for {insured or 'your business'}, "
                f"we still need the following:\n\n{body_list}\n\nReply with whatever you have so far "
                f"and we'll follow up on the rest.\n\nThank you.")
    ask = (f"Insured: {insured}\nAgent notes: {notes}\nMissing items:\n{body_list}\n\n"
           "Write a short, polite, plain-English email body from the agent asking the client for these. "
           "Group related questions, no jargon or field codes, no promises about coverage or price.")
    p = provider()
    try:
        if p == "gemini":
            out = _gemini_call([{"text": ask + ' Respond as JSON: {"email": "..."}'}], "You write emails for insurance agents.")
            return out.get("email") or fallback
        if p == "claude" and os.environ.get("ANTHROPIC_API_KEY"):
            resp = _claude_client().messages.create(model=CLAUDE_MODEL, max_tokens=1500,
                                                    messages=[{"role": "user", "content": ask}])
            return "".join(b.text for b in resp.content if b.type == "text") or fallback
    except Exception:
        pass
    return fallback
