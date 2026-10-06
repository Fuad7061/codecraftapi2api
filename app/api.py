"""OpenAI-compatible endpoints: /v1/models, /v1/chat/completions, /health"""
import json
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import db, pool

logger = logging.getLogger(__name__)
router = APIRouter()


# ── helpers ──────────────────────────────────────────────────────────────────
def _split(s: str) -> list[str]:
    return [x.strip() for x in s.replace(",", "\n").splitlines() if x.strip()]


def check_key(request: Request, cfg: dict) -> None:
    keys = _split(cfg.get("api_keys", ""))
    if not keys:
        return  # open access
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    if token not in keys:
        raise HTTPException(401, detail={"error": {"message": "Invalid API key", "type": "invalid_request_error"}})


def build_payload(req: dict, cfg: dict) -> dict:
    """Generation params are omitted (upstream auto) unless the request sets them,
    or a non-empty default is configured in Settings. Request values always win."""
    payload = {
        "model": req.get("model") or cfg["default_model"],
        "messages": req.get("messages", []),
    }
    for k in ("temperature", "system_prompt"):
        if req.get(k) is not None:
            payload[k] = req[k]
    # Codecraft wants the system prompt at root level
    msgs = payload["messages"]
    if msgs and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
        payload["system_prompt"] = msgs[0]["content"]
    for k in ("tools", "tool_choice", "response_format"):
        if k in req:
            payload[k] = req[k]
    return payload


async def _log(cfg, acc, model, stream, status, ms, usage=None, error=None):
    if cfg.get("enable_logging") != "1":
        return
    usage = usage or {}
    await db.execute(
        "INSERT INTO request_logs (account_name, model, stream, status_code, duration_ms, "
        "prompt_tokens, completion_tokens, error) VALUES (?,?,?,?,?,?,?,?)",
        (acc["name"] if acc else None, model, int(stream), status, ms,
         usage.get("prompt_tokens", 0) or 0, usage.get("completion_tokens", 0) or 0, error),
    )
    import random
    import asyncio
    if random.random() < 0.01:
        asyncio.create_task(db.cleanup_logs())


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "upstream_error", "code": status}}, status_code=status)


def _total(usage) -> int:
    u = usage or {}
    return int(u.get("total_tokens") or (u.get("prompt_tokens", 0) or 0) + (u.get("completion_tokens", 0) or 0))


# ── core ─────────────────────────────────────────────────────────────────────
async def collect(payload: dict, cfg: dict) -> dict:
    """Non-streaming completion. Raises pool.UpstreamError."""
    t0 = time.time()
    model = payload["model"]
    acc, resp = await pool.acquire(payload, cfg)
    content, reasoning, usage, calls = "", "", None, {}
    try:
        async for d in pool.iter_events(resp):
            if d.get("error"):
                raise pool.UpstreamError(str(d["error"]), 502)
            content += d.get("content") or ""
            reasoning += d.get("reasoning") or ""
            for i, tc in enumerate(d.get("tool_calls") or []):
                cur = calls.setdefault(tc.get("index", i), {"id": tc.get("id"), "type": "function",
                                                            "function": {"name": "", "arguments": ""}})
                fn = tc.get("function") or {}
                cur["function"]["name"] += fn.get("name") or ""
                cur["function"]["arguments"] += fn.get("arguments") or ""
                cur["id"] = cur["id"] or tc.get("id")
            if d.get("usage"):
                usage = d["usage"]
    except Exception as e:
        await pool.record_failure(acc["id"], str(e), int(cfg["max_failures"]))
        await _log(cfg, acc, model, False, 502, int((time.time() - t0) * 1000), error=str(e))
        raise
    await pool.record_success(acc["id"], _total(usage))
    await _log(cfg, acc, model, False, 200, int((time.time() - t0) * 1000), usage)
    msg = {"role": "assistant", "content": content or None}
    if reasoning:
        msg[cfg["reasoning_field"]] = reasoning
    if calls:
        msg["tool_calls"] = [calls[k] for k in sorted(calls)]
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def stream_chunks(payload: dict, cfg: dict, acc, resp, include_usage: bool):
    t0 = time.time()
    model = payload["model"]
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    usage, had_tools, ok, error = None, False, True, None

    def chunk(delta, finish=None, **extra):
        return "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra,
        }) + "\n\n"

    try:
        yield chunk({"role": "assistant"})
        async for d in pool.iter_events(resp):
            print("RAW UPSTREAM CHUNK:", d)
            if "error" in d:
                raise pool.UpstreamError(str(d["error"]), 502)
            delta = {}
            if "content" in d:
                delta["content"] = d["content"]
            if "reasoning" in d:
                delta[cfg["reasoning_field"]] = d["reasoning"]
            if "tool_calls" in d:
                delta["tool_calls"] = d["tool_calls"]
                had_tools = True
            if delta:
                yield chunk(delta)
            if d.get("usage"):
                usage = d["usage"]
        yield chunk({}, "tool_calls" if had_tools else "stop")
        if usage and include_usage:
            yield "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                         "model": model, "choices": [], "usage": usage}) + "\n\n"
    except Exception as e:
        ok, error = False, str(e)
        yield "data: " + json.dumps({"error": {"message": error, "type": "upstream_error"}}) + "\n\n"
    yield "data: [DONE]\n\n"
    if ok:
        await pool.record_success(acc["id"], _total(usage))
    else:
        await pool.record_failure(acc["id"], error, int(cfg["max_failures"]))
    await _log(cfg, acc, model, True, 200 if ok else 502, int((time.time() - t0) * 1000), usage, error)


# ── routes ───────────────────────────────────────────────────────────────────
@router.get("/health")
async def health():
    n = await db.fetchone("SELECT COUNT(*) AS n FROM accounts WHERE is_active = 1")
    return {"status": "ok", "active_accounts": n["n"]}


@router.get("/v1/models")
async def list_models(request: Request):
    cfg = await db.get_config()
    check_key(request, cfg)
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": m, "object": "model", "created": now, "owned_by": "codecraft"} for m in _split(cfg["models"])
    ]}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    cfg = await db.get_config()
    check_key(request, cfg)
    try:
        req = await request.json()
    except Exception:
        return _err("Invalid JSON body", 400)
    payload = build_payload(req, cfg)
    try:
        if not req.get("stream", False):
            return await collect(payload, cfg)
        acc, resp = await pool.acquire(payload, cfg)
    except pool.UpstreamError as e:
        await _log(cfg, None, payload["model"], bool(req.get("stream")), e.status, 0, error=str(e))
        return _err(str(e), e.status if 400 <= e.status < 600 else 502)
    except Exception as e:
        logger.exception("chat error")
        return _err(f"Upstream error: {e}", 502)
    include_usage = req.get("stream_options", {}).get("include_usage", False)
    return StreamingResponse(
        stream_chunks(payload, cfg, acc, resp, include_usage),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
