"""Dashboard: login + JSON management API + static single-page UI."""
import hashlib
import hmac
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app import api as openai_api
from app import db, pool

STATIC = Path(__file__).parent / "static"
router = APIRouter()  # dashboard UI lives at the site root (/)

SETTING_KEYS = [
    "api_keys", "models", "default_model", "strategy", "request_timeout", "max_failures",
    "impersonate", "user_agent", "proxy", "upstream_base", "reasoning_field",
    "default_temperature", "default_max_tokens", "enable_logging", "keepalive_minutes",
    "log_retention_days", "plan_refresh_minutes",
]


def _token(cfg: dict) -> str:
    return hmac.new(cfg["secret_key"].encode(), cfg["dashboard_password"].encode(), hashlib.sha256).hexdigest()


async def require_auth(request: Request) -> dict:
    cfg = await db.get_config()
    cookie = request.cookies.get("cc_auth", "")
    if not hmac.compare_digest(cookie, _token(cfg)):
        raise HTTPException(401, "Unauthorized")
    return cfg


# ── pages ────────────────────────────────────────────────────────────────────
@router.get("/login")
async def login_page():
    return FileResponse(STATIC / "login.html")


@router.post("/login")
async def login(request: Request):
    cfg = await db.get_config()
    body = await request.json()
    if not hmac.compare_digest(str(body.get("password", "")), cfg["dashboard_password"]):
        return JSONResponse({"error": "Invalid password"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("cc_auth", _token(cfg), httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


@router.post("/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("cc_auth")
    return resp


@router.get("/")
async def index(request: Request):
    try:
        await require_auth(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC / "index.html")


# ── helpers ──────────────────────────────────────────────────────────────────
def _mask(v: str) -> str:
    return v if len(v) <= 10 else f"{v[:4]}…{v[-4:]}"


def _public(a: dict) -> dict:
    return {
        "id": a["id"], "name": a["name"], "is_active": bool(a["is_active"]),
        "cf_clearance": _mask(a["cf_clearance"]), "remember_name": a["remember_name"],
        "remember_value": _mask(a["remember_value"]),
        "success_count": a["success_count"], "error_count": a["error_count"],
        "consecutive_failures": a["consecutive_failures"],
        "last_used": a["last_used"], "last_error": a["last_error"], "created_at": a["created_at"],
        "plan_name": a.get("plan_name"), "plan_remaining": a.get("plan_remaining"),
        "plan_total": a.get("plan_total"), "plan_used": a.get("plan_used"),
        "balance": a.get("balance"), "plan_checked_at": a.get("plan_checked_at"),
        "exhausted": pool.is_exhausted(a),
    }


def parse_cookies(raw: str) -> dict:
    """Parse a raw `Cookie:` header (or 'name=value' lines) into constants."""
    out = {}
    for part in re.split(r"[;\n]", raw):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            k, v = k.strip(), v.strip()
            if k == "cf_clearance":
                out["cf_clearance"] = v
            elif k.startswith("remember_web_"):
                out["remember_name"], out["remember_value"] = k, v
    return out


def _fields(d: dict) -> dict:
    f = {k: (d.get(k) or "").strip() for k in ("cf_clearance", "remember_name", "remember_value")}
    if d.get("cookie_string"):
        parsed = parse_cookies(d["cookie_string"])
        f.update({k: v for k, v in parsed.items() if v})
    return f


async def _unique_name(base: str) -> str:
    name, i = base, 2
    while await db.fetchone("SELECT 1 FROM accounts WHERE name = ?", (name,)):
        name, i = f"{base}-{i}", i + 1
    return name


# ── accounts ─────────────────────────────────────────────────────────────────
@router.get("/api/accounts")
async def list_accounts(_=Depends(require_auth)):
    return [_public(a) for a in await db.fetchall("SELECT * FROM accounts ORDER BY id")]


@router.post("/api/accounts")
async def add_account(request: Request, _=Depends(require_auth)):
    d = await request.json()
    f = _fields(d)
    if not all(f.values()):
        raise HTTPException(400, "cf_clearance, remember_web name and value are required (or paste a cookie string)")
    name = await _unique_name((d.get("name") or "").strip() or f"account-{len(await db.fetchall('SELECT id FROM accounts')) + 1}")
    aid = await db.execute(
        "INSERT INTO accounts (name, cf_clearance, remember_name, remember_value) VALUES (?,?,?,?)",
        (name, f["cf_clearance"], f["remember_name"], f["remember_value"]),
    )
    return {"id": aid, "name": name}


@router.post("/api/accounts/bulk")
async def bulk_add(request: Request, _=Depends(require_auth)):
    """One account per line: raw cookie string, optionally prefixed with 'name|'."""
    d = await request.json()
    added, skipped = 0, 0
    for line in (d.get("text") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name = ""
        if "|" in line and "=" in line.split("|", 1)[1]:
            name, line = line.split("|", 1)
        f = parse_cookies(line)
        if len(f) != 3:
            skipped += 1
            continue
        name = await _unique_name(name.strip() or f"account-{int(await _count()) + 1}")
        await db.execute(
            "INSERT INTO accounts (name, cf_clearance, remember_name, remember_value) VALUES (?,?,?,?)",
            (name, f["cf_clearance"], f["remember_name"], f["remember_value"]),
        )
        added += 1
    return {"added": added, "skipped": skipped}


async def _count() -> int:
    return (await db.fetchone("SELECT COUNT(*) AS n FROM accounts"))["n"]


@router.patch("/api/accounts/{aid}")
async def update_account(aid: int, request: Request, _=Depends(require_auth)):
    d = await request.json()
    acc = await db.fetchone("SELECT * FROM accounts WHERE id = ?", (aid,))
    if not acc:
        raise HTTPException(404, "Not found")
    f = {k: v for k, v in _fields(d).items() if v}  # blank = keep existing
    sets, args = [], []
    for k, v in f.items():
        sets.append(f"{k} = ?"); args.append(v)
    if d.get("name"):
        sets.append("name = ?"); args.append(d["name"].strip())
    if "is_active" in d:
        sets.append("is_active = ?"); args.append(int(bool(d["is_active"])))
        if d["is_active"]:
            sets.append("consecutive_failures = 0")
    if f:  # credentials changed -> reactivate + new session
        if "is_active" not in d:
            sets.append("is_active = 1")
        sets.append("consecutive_failures = 0")
    if sets:
        await db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", (*args, aid))
        pool.invalidate(aid)
    return {"ok": True}


@router.delete("/api/accounts/{aid}")
async def delete_account(aid: int, _=Depends(require_auth)):
    await db.execute("DELETE FROM accounts WHERE id = ?", (aid,))
    pool.invalidate(aid)
    return {"ok": True}


@router.post("/api/accounts/{aid}/test")
async def test_account(aid: int, cfg=Depends(require_auth)):
    acc = await db.fetchone("SELECT * FROM accounts WHERE id = ?", (aid,))
    if not acc:
        raise HTTPException(404, "Not found")
    ok, msg = await pool.test_account(acc, cfg)
    if ok:
        await db.execute("UPDATE accounts SET is_active = 1, consecutive_failures = 0 WHERE id = ?", (aid,))
    return {"ok": ok, "message": msg}


@router.post("/api/accounts/{aid}/plan")
async def refresh_plan(aid: int, cfg=Depends(require_auth)):
    acc = await db.fetchone("SELECT * FROM accounts WHERE id = ?", (aid,))
    if not acc:
        raise HTTPException(404, "Not found")
    try:
        return {"ok": True, "plan": await pool.fetch_plan(acc, cfg)}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@router.post("/api/plans/refresh")
async def refresh_all_plans(cfg=Depends(require_auth)):
    ok = fail = 0
    for acc in await db.fetchall("SELECT * FROM accounts"):
        try:
            await pool.fetch_plan(acc, cfg); ok += 1
        except Exception:
            fail += 1
    return {"ok": ok, "failed": fail}


# ── settings ─────────────────────────────────────────────────────────────────
@router.get("/api/settings")
async def get_settings(cfg=Depends(require_auth)):
    return {k: cfg[k] for k in SETTING_KEYS}


@router.put("/api/settings")
async def put_settings(request: Request, _=Depends(require_auth)):
    d = await request.json()
    values = {k: str(d[k]).strip() for k in SETTING_KEYS if k in d}
    if values.get("strategy") not in (None, "round_robin", "random", "least_used", "most_remaining"):
        raise HTTPException(400, "Invalid strategy")
    await db.set_config(values)
    for a in await db.fetchall("SELECT id FROM accounts"):
        pool.invalidate(a["id"])  # session settings (proxy/UA/impersonate) may have changed
    return {"ok": True}


@router.put("/api/password")
async def change_password(request: Request, _=Depends(require_auth)):
    d = await request.json()
    pw = (d.get("password") or "").strip()
    if len(pw) < 4:
        raise HTTPException(400, "Password too short")
    await db.set_config({"dashboard_password": pw})
    cfg = await db.get_config()
    r = JSONResponse({"ok": True})
    r.set_cookie("cc_auth", _token(cfg), httponly=True, samesite="lax", max_age=86400 * 30)
    return r


# ── stats / logs / test ──────────────────────────────────────────────────────
@router.get("/api/stats")
async def stats(_=Depends(require_auth)):
    acc = await db.fetchone("SELECT COUNT(*) AS total, SUM(is_active) AS active FROM accounts")
    req = await db.fetchone(
        "SELECT COUNT(*) AS total, SUM(status_code = 200) AS ok, "
        "ROUND(AVG(CASE WHEN status_code = 200 THEN duration_ms END)) AS avg_ms, "
        "SUM(prompt_tokens) AS pt, SUM(completion_tokens) AS ct FROM request_logs"
    )
    rows = await db.fetchall("SELECT plan_remaining, plan_total FROM accounts WHERE is_active = 1")
    unlimited = any(r["plan_remaining"] == -1 for r in rows)
    plan_rem = sum(r["plan_remaining"] for r in rows if (r["plan_remaining"] or 0) > 0)
    plan_tot = sum(r["plan_total"] or 0 for r in rows)
    return {
        "plan_remaining": plan_rem, "plan_total": plan_tot, "plan_unlimited": unlimited,
        "accounts_total": acc["total"], "accounts_active": acc["active"] or 0,
        "requests_total": req["total"], "requests_ok": req["ok"] or 0,
        "avg_ms": int(req["avg_ms"] or 0), "prompt_tokens": req["pt"] or 0, "completion_tokens": req["ct"] or 0,
    }


@router.get("/api/logs")
async def logs(limit: int = 100, _=Depends(require_auth)):
    return await db.fetchall("SELECT * FROM request_logs ORDER BY id DESC LIMIT ?", (min(limit, 500),))


@router.delete("/api/logs")
async def clear_logs(older_than_days: int | None = None, _=Depends(require_auth)):
    """No param = delete everything; ?older_than_days=N = delete only logs older than N days."""
    if older_than_days is None:
        n = (await db.fetchone("SELECT COUNT(*) AS n FROM request_logs"))["n"]
        await db.execute("DELETE FROM request_logs")
        await db.vacuum()
        return {"ok": True, "deleted": n}
    n = await pool.cleanup_logs(max(1, older_than_days))
    return {"ok": True, "deleted": n}


@router.post("/api/chat-test")
async def chat_test(request: Request, cfg=Depends(require_auth)):
    d = await request.json()
    payload = openai_api.build_payload(
        {"model": d.get("model"), "messages": [{"role": "user", "content": d.get("prompt", "Say hello")}]}, cfg
    )
    try:
        r = await openai_api.collect(payload, cfg)
    except pool.UpstreamError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"Error: {e}"}
    m = r["choices"][0]["message"]
    return {"ok": True, "content": m.get("content") or "", "reasoning": m.get(cfg["reasoning_field"]), "usage": r["usage"]}
