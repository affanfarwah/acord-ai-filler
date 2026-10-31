# Demo: Bluebonnet Care Transport LLC (fictional)

Every name, number, address and the VIN here are made up. Regenerate with `.venv/bin/python tools/make_demo.py`.

## Input
`input/Demo_Questionnaire_Bluebonnet_Care.xlsx`: the agency's questionnaire layout (Question, Response, Source,
Status, Notes). Two rows are **Needed** (DBA, annual revenue) and two are **Confirm** (driver date of birth, medical payments).

## Output
| File | What it is |
|---|---|
| `Bluebonnet_Care_ACORD_125.pdf` | Commercial Insurance Application, fillable |
| `Bluebonnet_Care_ACORD_127.pdf` | Business Auto Section, fillable |
| `Bluebonnet_Care_ACORD_137.pdf` | Texas Commercial Auto Coverages, fillable |
| `Bluebonnet_Care_ACORD_package.pdf` | All three combined, read-only |
| `facts_review.csv` | Every fact: value, source cell, status, and whether it went into the PDFs |

50 facts: 47 went into the PDFs and 3 are held back for review:
- **Driver date of birth** and **medical payments**: their rows are marked Confirm in the questionnaire.
- **Vehicle cost new**: the citation points at the wrong cell (on purpose, to show the check working).

Needed rows never reach the AI, so DBA and revenue are blank.

## How this demo was made
The AI step was replaced by a fixed list of answers with cell citations (`DEMO_ANSWERS` in `tools/make_demo.py`),
so it runs without an API key. Everything after that is the app's real code: Excel rules, citation checks,
statuses, agency settings and PDF filling. With a real key, the AI produces those answers from the documents.
