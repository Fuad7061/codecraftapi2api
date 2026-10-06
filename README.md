# Codecraft2API

OpenAI-compatible gateway for **codecraftapi.com** with an **account pool**, failover, and a web dashboard.

- `POST /v1/chat/completions` (streaming + non-streaming, tools, vision, JSON mode, reasoning, usage)
- `GET /v1/models`
- Dashboard at `/dashboard/` — add accounts (paste constants or a whole Cookie header), bulk import, test, enable/disable, all settings, logs, test chat
- Round-robin / random / least-used rotation, automatic failover, auto-disable after N failures
- SQLite persistence in `/app/data` (accounts + settings survive redeploys)

## Deploy on Coolify (Hetzner)

1. Push this folder to GitHub.
2. Coolify → New Resource → Public/Private Repository → Build Pack: **Dockerfile**, port **8000**.
3. **Persistent Storage**: add a volume mounted at `/app/data` (**required** — otherwise accounts are lost on redeploy).
4. Environment variables (first start only; later managed in dashboard):
   `API_KEY`, `DASHBOARD_PASSWORD`. Optionally `CF_CLEARANCE`, `REMEMBER_WEB_NAME`, `REMEMBER_WEB_VALUE` to seed one account.
5. Deploy, open `https://your-domain/dashboard/`, log in, add accounts.

Point your client (OmniRoute etc.) to `https://your-domain/v1` with your API key.

## Getting the constants

Log into codecraftapi.com, open DevTools → Application → Cookies, copy `cf_clearance` and the
`remember_web_<hash>` cookie (name **and** value). Or copy the request `Cookie:` header and paste it into the dashboard.

> **Important:** `cf_clearance` is bound to the **IP address and User-Agent** of the browser that solved the
> Cloudflare challenge. A cookie grabbed on your laptop may be rejected from the Hetzner IP (HTTP 403).
> Fix: get the cookie via a browser routed through the VPS IP (SSH SOCKS proxy `ssh -D 1080 user@vps`),
> and/or set the browser's **User-Agent override** and **Outbound proxy** in Settings. Expired accounts show the
> error in the dashboard; use **Test** after updating cookies.

## Local run

```bash
pip install -r requirements.txt
DB_PATH=./data/codecraft.db python run.py   # http://localhost:8000
```
