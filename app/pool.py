"""
Upstream client + account pool for codecraftapi.com.

Each account = cf_clearance + remember_web_<hash> cookie. A curl_cffi session
(Chrome TLS impersonation) is kept per account, XSRF token is read fresh from
the cookie jar for every request. Requests fail over to other accounts.
"""
import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from curl_cffi.requests import AsyncSession

from app import db

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    def __init__(self, message: str, status: int = 502, account_fault: bool = True):
        super().__init__(message)
        self.status = status
        self.account_fault = account_fault


class _Runtime:
    def __init__(self):
        self.session: AsyncSession | None = None
        self.lock = asyncio.Lock()


_runtime: dict[int, _Runtime] = {}
_rr_counter = 0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _rt(acc_id: int) -> _Runtime:
    return _runtime.setdefault(acc_id, _Runtime())


def invalidate(acc_id: int) -> None:
    """Drop cached session (call after credentials change / delete)."""
    rt = _runtime.pop(acc_id, None)
    if rt and rt.session:
        asyncio.get_event_loop().create_task(rt.session.close())


def _xsrf(session: AsyncSession) -> str | None:
    try:
        v = session.cookies.get("XSRF-TOKEN")
    except Exception:
        v = None
        for c in session.cookies.jar:
            if c.name == "XSRF-TOKEN":
                v = c.value
    return unquote(v) if v else None


def _headers_extra(cfg: dict) -> dict:
    return {"user-agent": cfg["user_agent"]} if cfg.get("user_agent") else {}


async def _login(acc: dict, cfg: dict, rt: _Runtime) -> AsyncSession:
    if rt.session:
        try:
            await rt.session.close()
        except Exception:
            pass
    base = cfg["upstream_base"].rstrip("/")
    host = urlparse(base).hostname
    kw = {"impersonate": cfg["impersonate"], "timeout": int(cfg["request_timeout"])}
    if cfg.get("proxy"):
        kw["proxies"] = {"http": cfg["proxy"], "https": cfg["proxy"]}
    s = AsyncSession(**kw)
    s.cookies.set("cf_clearance", acc["cf_clearance"], domain=host)
    s.cookies.set(acc["remember_name"], acc["remember_value"], domain=host)
    r = await s.get(f"{base}/dashboard/playground", headers=_headers_extra(cfg))
    if r.status_code != 200:
        await s.close()
        hint = " (cf_clearance expired, or IP/User-Agent mismatch)" if r.status_code in (403, 503) else ""
        raise UpstreamError(f"Login failed: HTTP {r.status_code}{hint}", r.status_code)
    if "/login" in str(r.url):
        await s.close()
        raise UpstreamError("Login failed: remember_web cookie invalid/expired", 401)
    if not _xsrf(s):
        await s.close()
        raise UpstreamError("Login failed: no XSRF-TOKEN received", 502)
    rt.session = s
    await _sync_cookies(acc, s)
    return s


async def _sync_cookies(acc: dict, s: AsyncSession) -> None:
    """Upstream rotates cookies via Set-Cookie (remember_web / cf_clearance). Persist
    rotated values so the account keeps working after restarts/redeploys."""
    try:
        cur = {}
        for c in s.cookies.jar:
            if c.name in ("cf_clearance", acc["remember_name"]) and c.value:
                cur[c.name] = c.value
        sets, args = [], []
        if cur.get("cf_clearance") and cur["cf_clearance"] != acc["cf_clearance"]:
            sets.append("cf_clearance = ?"); args.append(cur["cf_clearance"]); acc["cf_clearance"] = cur["cf_clearance"]
        rv = cur.get(acc["remember_name"])
        if rv and rv != acc["remember_value"]:
            sets.append("remember_value = ?"); args.append(rv); acc["remember_value"] = rv
        if sets:
            await db.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", (*args, acc["id"]))
            logger.info("Account %s: cookies auto-refreshed from upstream", acc["name"])
    except Exception as e:
        logger.debug("cookie sync failed: %s", e)


# ── plan / quota ─────────────────────────────────────────────────────────────
def _num(s: str | None) -> int | None:
    if not s:
        return None
    d = re.sub(r"[^\d]", "", s)
    return int(d) if d else None


def parse_plan(html: str) -> dict:
    """Scrape /dashboard/billing/plan. remaining = -1 means unlimited."""
    out: dict = {}
    m = re.search(r"Current Plan</h2>\s*<p[^>]*>\s*([^<]+?)\s*</p>", html)
    out["plan_name"] = m.group(1) if m else None
    m = re.search(r"Plan tokens remaining</p>\s*<p[^>]*>\s*([^<]+?)\s*</p>", html)
    if m:
        txt = m.group(1)
        out["plan_remaining"] = -1 if re.search(r"unlimit|∞", txt, re.I) else _num(txt)
    m = re.search(r"Used this month</span>\s*<span>\s*([\d,]+)\s*/\s*([\d,]+)", html)
    if m:
        out["plan_used"], out["plan_total"] = _num(m.group(1)), _num(m.group(2))
    else:
        m = re.search(r"of\s+([\d,]+)\s+this month", html)
        if m:
            out["plan_total"] = _num(m.group(1))
    m = re.search(r">Balance</h2>\s*<p[^>]*>\s*\$?\s*([\d,]*\.?\d+)", html)
    if m:
        out["balance"] = float(m.group(1).replace(",", ""))
    return out


async def fetch_plan(acc: dict, cfg: dict) -> dict:
    base = cfg["upstream_base"].rstrip("/")
    url = f"{base}/dashboard/billing/plan"
    hdr = {"accept": "text/html,application/xhtml+xml", "referer": f"{base}/dashboard/usage", **_headers_extra(cfg)}
    r = None
    for attempt in (0, 1):
        s = await ensure_session(acc, cfg, force=(attempt == 1))
        r = await s.get(url, headers=hdr)
        if r.status_code == 200 and "/login" not in str(r.url):
            break
        if attempt == 1:
            raise UpstreamError(f"Plan fetch failed: HTTP {r.status_code}", r.status_code)
    info = parse_plan(r.text)
    if info.get("plan_remaining") is None and info.get("plan_total") is None:
        raise UpstreamError("Plan page layout not recognised", 502, False)
    await _sync_cookies(acc, s)
    cols = ["plan_name", "plan_remaining", "plan_total", "plan_used", "balance"]
    vals = [info.get(c) for c in cols]
    await db.execute(
        f"UPDATE accounts SET {', '.join(c + ' = ?' for c in cols)}, plan_checked_at = ? WHERE id = ?",
        (*vals, _now(), acc["id"]),
    )
    return info


def is_exhausted(a: dict) -> bool:
    """Plan tokens used up AND no prepaid balance to fall back on."""
    return a.get("plan_remaining") == 0 and (a.get("balance") or 0) <= 0


async def ensure_session(acc: dict, cfg: dict, force: bool = False) -> AsyncSession:
    rt = _rt(acc["id"])
    async with rt.lock:
        if rt.session is None or force:
            return await _login(acc, cfg, rt)
        return rt.session


async def _post(acc: dict, cfg: dict, payload: dict):
    base = cfg["upstream_base"].rstrip("/")
    url = f"{base}/dashboard/playground"
    for attempt in (0, 1):
        s = await ensure_session(acc, cfg, force=(attempt == 1))
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "referer": url,
            "origin": base,
            "x-xsrf-token": _xsrf(s) or "",
            "x-requested-with": "XMLHttpRequest",
            **_headers_extra(cfg),
        }
        resp = await s.post(url, headers=headers, json=payload, stream=True)
        if resp.status_code == 200:
            return resp
        body = (await resp.acontent())[:300].decode("utf-8", "replace")
        await resp.aclose()
        if resp.status_code in (401, 403, 419) and attempt == 0:
            continue  # re-login once
        fault = resp.status_code not in (400, 422)
        raise UpstreamError(f"HTTP {resp.status_code}: {body}", resp.status_code, fault)


async def _pick(tried: set, strategy: str) -> dict | None:
    global _rr_counter
    rows = await db.fetchall("SELECT * FROM accounts WHERE is_active = 1 ORDER BY id")
    rows = [r for r in rows if r["id"] not in tried and not is_exhausted(r)]
    if not rows:
        return None
    if strategy == "random":
        return random.choice(rows)
    if strategy == "most_remaining":
        return max(rows, key=lambda r: (10**15 if r["plan_remaining"] == -1 else (r["plan_remaining"] or 0)))
    if strategy == "least_used":
        return min(rows, key=lambda r: (r["success_count"] + r["error_count"], r["last_used"] or ""))
    _rr_counter += 1
    return rows[_rr_counter % len(rows)]


async def record_success(acc_id: int, tokens: int = 0) -> None:
    # tokens: locally decrement the cached plan balance between upstream refreshes
    await db.execute(
        "UPDATE accounts SET success_count = success_count + 1, consecutive_failures = 0, "
        "last_used = ?, last_error = NULL, "
        "plan_remaining = CASE WHEN plan_remaining > 0 THEN MAX(plan_remaining - ?, 0) ELSE plan_remaining END, "
        "plan_used = CASE WHEN plan_used IS NOT NULL THEN plan_used + ? ELSE NULL END WHERE id = ?",
        (_now(), tokens, tokens, acc_id),
    )


async def record_failure(acc_id: int, error: str, max_failures: int) -> None:
    await db.execute(
        "UPDATE accounts SET error_count = error_count + 1, consecutive_failures = consecutive_failures + 1, "
        "last_used = ?, last_error = ?, "
        "is_active = CASE WHEN consecutive_failures + 1 >= ? THEN 0 ELSE is_active END WHERE id = ?",
        (_now(), error[:300], max_failures, acc_id),
    )


async def acquire(payload: dict, cfg: dict):
    """Open an upstream stream, failing over across accounts. Returns (account, response)."""
    tried: set = set()
    last: UpstreamError | None = None
    max_failures = int(cfg["max_failures"])
    while True:
        acc = await _pick(tried, cfg["strategy"])
        if not acc:
            raise last or UpstreamError("No active accounts in pool. Add one in the dashboard.", 503, False)
        tried.add(acc["id"])
        try:
            return acc, await _post(acc, cfg, payload)
        except UpstreamError as e:
            if not e.account_fault:
                raise
            logger.warning("Account %s failed: %s", acc["name"], e)
            await record_failure(acc["id"], str(e), max_failures)
            last = e
        except Exception as e:  # network errors etc.
            logger.warning("Account %s error: %s", acc["name"], e)
            await record_failure(acc["id"], str(e), max_failures)
            last = UpstreamError(f"Upstream error: {e}", 502)


async def iter_events(resp):
    """Yield parsed JSON objects from the upstream SSE stream."""
    try:
        async for line in resp.aiter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8", "ignore")
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                return
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue
    finally:
        try:
            await resp.aclose()
        except Exception:
            pass


async def test_account(acc: dict, cfg: dict) -> tuple[bool, str]:
    """Force a fresh login for one account and update its status."""
    try:
        await ensure_session(acc, cfg, force=True)
    except UpstreamError as e:
        await record_failure(acc["id"], str(e), 10**9)
        return False, str(e)
    except Exception as e:
        await record_failure(acc["id"], str(e), 10**9)
        return False, f"Error: {e}"
    await record_success(acc["id"])
    try:
        await fetch_plan(acc, cfg)
    except Exception as e:
        return True, f"Authenticated OK (plan fetch failed: {e})"
    return True, "Authenticated OK, plan refreshed"


async def plan_loop() -> None:
    """Refresh every account's plan tokens (they reset monthly) on an interval."""
    await asyncio.sleep(5)
    while True:
        mins = 10
        try:
            cfg = await db.get_config()
            mins = max(1, int(cfg.get("plan_refresh_minutes") or 10))
            for acc in await db.fetchall("SELECT * FROM accounts WHERE is_active = 1"):
                try:
                    await fetch_plan(acc, cfg)
                except Exception as e:
                    logger.warning("plan refresh %s: %s", acc["name"], e)
        except Exception as e:
            logger.error("plan loop error: %s", e)
        await asyncio.sleep(mins * 60)


async def cleanup_logs(days: int | None = None) -> int:
    """Delete request logs older than `days` (default: log_retention_days setting)."""
    if days is None:
        days = int((await db.get_config()).get("log_retention_days") or 0)
    if days <= 0:
        return 0
    n = (await db.fetchone(
        "SELECT COUNT(*) AS n FROM request_logs WHERE created_at < datetime('now', ?)", (f"-{days} days",)))["n"]
    if n:
        await db.execute("DELETE FROM request_logs WHERE created_at < datetime('now', ?)", (f"-{days} days",))
        await db.vacuum()
    return n


async def cleanup_loop() -> None:
    while True:
        try:
            n = await cleanup_logs()
            if n:
                logger.info("Auto-cleanup removed %d old log rows", n)
        except Exception as e:
            logger.error("cleanup error: %s", e)
        await asyncio.sleep(3600)


async def keepalive_loop() -> None:
    """Periodically refresh sessions so cookies/XSRF stay warm."""
    while True:
        try:
            cfg = await db.get_config()
            mins = int(cfg.get("keepalive_minutes") or 0)
        except Exception:
            mins = 0
        await asyncio.sleep(max(60, mins * 60) if mins else 300)
        if not mins:
            continue
        try:
            cfg = await db.get_config()
            for acc in await db.fetchall("SELECT * FROM accounts WHERE is_active = 1"):
                try:
                    await ensure_session(acc, cfg, force=True)
                except Exception as e:
                    await record_failure(acc["id"], f"keepalive: {e}", int(cfg["max_failures"]))
        except Exception as e:
            logger.error("keepalive error: %s", e)
