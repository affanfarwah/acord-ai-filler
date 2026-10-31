# ACORD AI Filler

Upload a client's documents (Excel questionnaire, policies, dec pages, prior ACORD forms), review the facts
the AI found, and download filled ACORD 125, 126, 127, 130, 131, 137 and 140 forms.

## Run it

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # then put your key in .env
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000

| Setting (env or .env) | Meaning |
|---|---|
| `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | AI key. Whichever is set is used. Free Gemini tier: sample data only. |
| `ACORD_AI` | Force `gemini`, `claude` or `fake` (offline stand-in for tests). |
| `ACORD_AUTH` | `off` (default, one local dev user) or `on` (accounts and agencies). |
| `ACORD_HTTPS` | `on` when hosted behind HTTPS (secure cookies). |
| `ACORD_DATA` | Where the database and uploads live (default `out/`). |

Tests: `.venv/bin/python -m pytest -q tests`

## How it works

- **Case**: one client submission. It owns the uploaded files, the chosen forms and one fact list.
- **Facts** are keyed by ACORD data-element names (`NamedInsured_FullName_A`). ACORD uses one data
  dictionary across forms, so one fact fills that field on every form, and an edit anywhere updates all of them.
- **Reading documents** runs in the background: sources are read once, the AI proposes values for every
  selected form's fields, then code checks each value against the cell or page it cites.
- **Statuses**: *Matches source* (citation checked), *Check source* (couldn't verify), *Client to confirm*
  (questionnaire row marked Confirm), *Approved*, *Entered by you*. Only the first, fourth and fifth go into PDFs.
- **Excel rules in code**: rows with Status = Needed are never sent to the AI; Confirm rows are tagged.
- Producer details come from Agency settings. Insurer, signature and form-date fields are left to the agent.

## Before launch

- ACORD form licensing for a hosted multi-agency product (the bundled PDFs are third-party copies).
- Postgres + private object storage + encryption at rest, retention/deletion policy, backups.
- A real job queue, password reset, rate limiting, roles, and a privacy policy / DPA.
