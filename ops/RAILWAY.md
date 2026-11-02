# Deploying ACORD AI Filler to Railway

One service, one domain, the same pattern as Deal Analyzer: Railway builds the `Dockerfile`
from the GitHub repo and serves the whole app from one process.

Different from Deal Analyzer: no Postgres. The data is a SQLite file plus uploaded documents,
so the service needs a **volume**. Without it every redeploy starts with no clients.

---

## 1 · Provision

In a new Railway project:

1. **New → GitHub Repo → `affanfarwah/acord-ai-filler`**
2. On the service: **Settings → Volumes → New Volume**, mount path **`/data`**

`railway.toml` selects the Dockerfile builder, sets the health check to `/healthz` and pins one
replica. Keep it at one: the AI jobs run in background threads against one SQLite file.

## 2 · Environment variables

Service → **Variables**. Railway supplies `PORT` itself.

| Variable | Value |
|---|---|
| `ACORD_AUTH` | `on` |
| `ACORD_HTTPS` | `on` (cookies are sent over HTTPS only) |
| `ACORD_LOGIN_EMAIL` | the shared login, e.g. `office@<agency domain>` |
| `ACORD_LOGIN_PASSWORD` | 12+ characters: `openssl rand -base64 18` |
| `ACORD_AGENCY_NAME` | `Mir Insurance Agency` |
| `GEMINI_API_KEY` | the **paid** key (billing-enabled Google Cloud project) |
| `ACORD_GEMINI_THINKING_BUDGET` | `0` (faster, cheaper) |
| `RAILWAY_RUN_UID` | `0` (lets the app write to the volume) |

Deliberately not set: `ACORD_SIGNUP` stays off, so the sign-in page has no "Create an account"
and `/register` returns 404. Only the shared login exists.

**One login, shared.** It is created on first start. To change the password, edit
`ACORD_LOGIN_PASSWORD` and redeploy; the old one stops working. Everyone using it is the same
user in the audit log.

## 3 · Deploy, then generate the domain

**Settings → Networking → Generate Domain** gives `https://<name>.up.railway.app`.

For a custom domain (e.g. `forms.<agency domain>`): **Settings → Networking → Custom Domain**,
enter it, then add the record Railway shows (a `CNAME` to `<something>.up.railway.app`, plus a
`TXT` verification record if asked) at the domain's DNS provider. HTTPS is issued
automatically once DNS resolves, usually within minutes, up to a few hours.

## 4 · Verify

```bash
curl -s https://<domain>/healthz                              # {"ok":true}
curl -s -o /dev/null -w "%{http_code}\n" https://<domain>/    # 303, redirects to sign-in
```

Then sign in, open **Agency settings**, fill the agency details, and run the demo client from
`samples/demo/input/` end to end before any real client.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Health check fails, log shows `unable to open database file` / `Permission denied` | Volume missing, not at `/data`, or `RAILWAY_RUN_UID=0` not set |
| Clients disappear after a redeploy | No volume, data was written inside the container |
| Signed out after every redeploy | Same: the session key lives in `/data/secret.key` |
| Sign-in refused with the right password | `ACORD_LOGIN_EMAIL` has a typo, or the service was not redeployed after changing the password |
| Container exits: `ACORD_LOGIN_PASSWORD must be at least 12 characters` | Use a longer password |
| Sign-in loops back to the sign-in page | `ACORD_HTTPS=on` while opening the app over plain `http://` |
| "AI is not set up" banner | `GEMINI_API_KEY` missing |
