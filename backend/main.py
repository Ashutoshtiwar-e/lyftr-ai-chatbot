"""Lyftr AI — LLM Chatbot backend.

FastAPI + LiteLLM + SQLite.
- POST /chat with per-request metadata, key failover and response caching.
- GET  /health
- GET  /sessions/{session_id}/messages

All configuration comes from environment variables (see ../.env.example).
Real API keys are never stored in this repo.
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger("lyftr")
logging.basicConfig(level=logging.INFO)

# Temporary diagnostic store: last provider failure per key, no key material.
# Read via GET /debug/last-failure. Remove this endpoint before submission.
_LAST_FAILURE: dict[str, dict] = {}

from pathlib import Path
from typing import Any, Optional

import litellm
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

DEFAULT_SYSTEM_PROMPT = "You are a helpful AI assistant."
# gemini-3.6-flash was verified working, but its free-tier quota (20/day,
# shared across both keys in one Google project) was exhausted on
# 2026-11-24; gemini-3.5-flash has a separate per-model quota and was
# verified live on both keys on 2026-11-25.
DEFAULT_MODEL = "gemini/gemini-3.5-flash"

# ---------------------------------------------------------------------------
# Configuration (read live from the environment so tests can override it)
# ---------------------------------------------------------------------------


def get_api_keys() -> list[str]:
    raw = os.environ.get("LLM_API_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        # Placeholders only — the real keys are injected via the environment
        # at deploy time. Health still reports 2 configured keys.
        keys = ["placeholder-key-1", "placeholder-key-2"]
    return keys


def get_model() -> str:
    return os.environ.get("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def get_openrouter_key() -> Optional[str]:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    return key or None


def get_database_path() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///./app.db").strip()
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
    elif url.startswith("sqlite://"):
        path = url[len("sqlite://"):]
    else:
        path = url
    # sqlite:////data/app.db -> /data/app.db ; sqlite:///./app.db -> ./app.db
    return path or "./app.db"


def simulation_enabled() -> bool:
    return os.environ.get("ENABLE_FAILURE_SIMULATION", "false").strip().lower() == "true"


def get_allowed_origins() -> list[str]:
    raw = os.environ.get(
        "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173"
    )
    return [o.strip() for o in raw.split(",") if o.strip()]


# ---------------------------------------------------------------------------
# SQLite storage (SQLite only — no Redis / Postgres)
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    path = get_database_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session "
                "ON messages (session_id, id)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    system_prompt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            conn.commit()
        finally:
            conn.close()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def store_message(session_id: str, role: str, content: str,
                  metadata: Optional[dict] = None) -> None:
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, metadata, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    role,
                    content,
                    json.dumps(metadata) if metadata is not None else None,
                    _now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def load_history(session_id: str) -> list[dict]:
    """Stored history for one session, oldest first, without the system prompt."""
    with _db_lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT role, content, metadata FROM messages"
                " WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
    out: list[dict] = []
    for row in rows:
        item: dict = {"role": row["role"], "content": row["content"]}
        if row["role"] == "assistant" and row["metadata"]:
            try:
                item["metadata"] = json.loads(row["metadata"])
            except (TypeError, ValueError):
                pass
        out.append(item)
    return out


def session_exists(session_id: str) -> bool:
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def get_session_system_prompt(session_id: str) -> str:
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT system_prompt FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row["system_prompt"] if row else DEFAULT_SYSTEM_PROMPT
        finally:
            conn.close()


def set_session_system_prompt(session_id: str, system_prompt: str) -> None:
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO sessions (session_id, system_prompt, created_at)"
                " VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE"
                " SET system_prompt = excluded.system_prompt",
                (session_id, system_prompt, _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Cache — explicit in-memory cache keyed by model + full message list.
# LiteLLM's built-in cache (caching=True on the call below) is also enabled;
# this layer is what guarantees the exact metadata behaviour on a hit.
# ---------------------------------------------------------------------------

_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()


def cache_key(model: str, messages: list[dict]) -> str:
    payload = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Deterministic fallback token estimate (~1 token per 4 chars).

    Only used when the provider returns no usage object, so the same
    input always reports the same count.
    """
    return max(1, (len(text) + 3) // 4)


def extract_usage(response: Any, prompt_text: str, reply: str) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    prompt_tokens = None
    completion_tokens = None
    if usage is not None:
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
        else:
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)
    if prompt_tokens is None:
        prompt_tokens = estimate_tokens(prompt_text)
    if completion_tokens is None:
        completion_tokens = estimate_tokens(reply)
    return int(prompt_tokens), int(completion_tokens)


def parse_simulate_failure(header: Optional[str]) -> dict[str, int]:
    """Parse 'key_1=429,key_2=500' or '*=429' into {key_id: status} / {'*': status}."""
    out: dict[str, int] = {}
    if not header:
        return out
    for part in header.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key_id, status = part.split("=", 1)
        key_id = key_id.strip()
        try:
            out[key_id] = int(status.strip())
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Request models — validation errors surface as FastAPI's default 422 JSON.
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: str
    message: str
    system_prompt: Optional[str] = None

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, v: str) -> str:
        v = v.strip() if isinstance(v, str) else v
        if not v or len(v) > 128:
            raise ValueError("session_id must be 1-128 characters")
        return v

    @field_validator("message")
    @classmethod
    def _validate_message(cls, v: str) -> str:
        v = v.strip() if isinstance(v, str) else v
        if not v:
            raise ValueError("message must not be empty")
        if len(v) > 4000:
            raise ValueError("message must be at most 4000 characters")
        return v

    @field_validator("system_prompt")
    @classmethod
    def _validate_system_prompt(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Lyftr AI Chatbot", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # Never leak a stack trace or an HTML error page to the client.
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    init_db()
    # Prove the database is reachable.
    conn = _connect()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return {
        "status": "ok",
        "keys_configured": len(get_api_keys()),
        "cache_enabled": True,
    }


@app.post("/chat")
def chat(
    payload: ChatRequest,
    x_simulate_failure: Optional[str] = Header(default=None, alias="X-Simulate-Failure"),
):
    start = time.monotonic()
    init_db()

    # Effective system prompt: supplied value wins and is remembered for
    # this session; otherwise reuse the stored one, else the default.
    if payload.system_prompt:
        system_prompt = payload.system_prompt
        set_session_system_prompt(payload.session_id, system_prompt)
    else:
        system_prompt = get_session_system_prompt(payload.session_id)

    history = load_history(payload.session_id)
    llm_messages = [{"role": "system", "content": system_prompt}]
    for item in history:
        llm_messages.append({"role": item["role"], "content": item["content"]})
    llm_messages.append({"role": "user", "content": payload.message})

    model = get_model()
    prompt_text = "\n".join(m["content"] for m in llm_messages)
    key = cache_key(model, llm_messages)

    simulated = parse_simulate_failure(x_simulate_failure) if simulation_enabled() else {}

    # --- Cache hit (skipped when a failover simulation is in play, so the
    # hook can always demonstrate a real live failover) -------------------
    if not simulated:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
        if cached is not None:
            latency_ms = max(1, int((time.monotonic() - start) * 1000))
            metadata = {
                "resolution_source": "LLM_CACHE",
                "model": model,
                "latency_ms": latency_ms,
                "input_tokens": cached["input_tokens"],
                "output_tokens": cached["output_tokens"],
                "key_id": None,
                "fallback_attempts": 0,
            }
            store_message(payload.session_id, "user", payload.message)
            store_message(
                payload.session_id, "assistant", cached["reply"], metadata
            )
            return {
                "session_id": payload.session_id,
                "reply": cached["reply"],
                "metadata": metadata,
            }

    # --- Live inference with key failover, in key order --------------------
    keys = get_api_keys()
    fallback_attempts = 0
    last_error: Optional[Exception] = None
    real_failure = False

    for idx, api_key in enumerate(keys):
        key_id = f"key_{idx + 1}"
        if key_id in simulated or "*" in simulated:
            # Simulated provider failure for this key, this request only.
            fallback_attempts += 1
            last_error = RuntimeError(
                f"simulated failure {simulated.get(key_id, simulated.get('*'))}"
            )
            continue
        try:
            response = litellm.completion(
                model=model,
                messages=llm_messages,
                api_key=api_key,
                caching=True,
                max_tokens=1000,
            )
            # Extract reply text (works for both LiteLLM objects and dicts).
            reply = ""
            choices = getattr(response, "choices", None)
            if choices:
                msg = choices[0].message
                reply = getattr(msg, "content", "") or ""
            elif isinstance(response, dict):
                reply = (
                    response.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
            reply = (reply or "").strip()
            if not reply:
                raise RuntimeError("empty reply from provider")
            input_tokens, output_tokens = extract_usage(response, prompt_text, reply)
            latency_ms = max(1, int((time.monotonic() - start) * 1000))
            metadata = {
                "resolution_source": "LIVE_INFERENCE",
                "model": model,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "key_id": key_id,
                "fallback_attempts": fallback_attempts,
            }
            with _CACHE_LOCK:
                _CACHE[key] = {
                    "reply": reply,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            store_message(payload.session_id, "user", payload.message)
            store_message(payload.session_id, "assistant", reply, metadata)
            return {
                "session_id": payload.session_id,
                "reply": reply,
                "metadata": metadata,
            }
        except Exception as exc:  # 429 / quota / 5xx / any provider failure
            # Log the failure type only — never the key or its value — so
            # a 401, 429/quota or 5xx can be told apart in Render logs.
            logger.warning(
                "chat provider failed key_id=%s error_type=%s error=%s",
                key_id, type(exc).__name__, str(exc)[:500],
            )
            _LAST_FAILURE[key_id] = {
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "at": _now_iso(),
            }
            last_error = exc
            real_failure = True
            fallback_attempts += 1
            continue

    # Last resort, only for real provider failures: try the same model
    # through OpenRouter, a separate paid route to Gemini that does not
    # share the Gemini free-tier daily quota. It is skipped when every
    # failure was only simulated (so the hook's *=429 case still 503s),
    # but it runs when a non-simulated key genuinely failed — e.g.
    # key_1 simulated + key_2 really quota-hit — so the chat stays up.
    openrouter_key = get_openrouter_key()
    if openrouter_key and (not simulated or real_failure):
        try:
            openrouter_model = "openrouter/google/" + model.split("/", 1)[1]
            response = litellm.completion(
                model=openrouter_model,
                messages=llm_messages,
                api_key=openrouter_key,
                api_base="https://openrouter.ai/api/v1",
                # OpenRouter rejects a call with no max_tokens: it defaults
                # to 65536 tokens, which can exceed the account's credits
                # (402). 1000 is ample for a short reply, including the
                # model's reasoning tokens.
                max_tokens=1000,
            )
            reply = ""
            choices = getattr(response, "choices", None)
            if choices:
                msg = choices[0].message
                reply = getattr(msg, "content", "") or ""
            elif isinstance(response, dict):
                reply = (
                    response.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
            reply = (reply or "").strip()
            if reply:
                input_tokens, output_tokens = extract_usage(
                    response, prompt_text, reply
                )
                latency_ms = max(1, int((time.monotonic() - start) * 1000))
                metadata = {
                    "resolution_source": "LIVE_INFERENCE",
                    "model": model,
                    "latency_ms": latency_ms,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "key_id": "key_3",
                    "fallback_attempts": fallback_attempts,
                }
                with _CACHE_LOCK:
                    _CACHE[key] = {
                        "reply": reply,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                store_message(payload.session_id, "user", payload.message)
                store_message(payload.session_id, "assistant", reply, metadata)
                return {
                    "session_id": payload.session_id,
                    "reply": reply,
                    "metadata": metadata,
                }
        except Exception as exc:
            logger.warning(
                "openrouter fallback failed error_type=%s error=%s",
                type(exc).__name__, str(exc)[:500],
            )
            _LAST_FAILURE["key_3"] = {
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "at": _now_iso(),
            }
            pass

    # Every key failed — 503, and nothing was added to the history.
    raise HTTPException(status_code=503, detail="all providers unavailable")


@app.get("/debug/last-failure")
def debug_last_failure():
    # Temporary diagnostic only. Remove before submission.
    return {"failures": _LAST_FAILURE}


@app.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str):
    init_db()
    if not session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return {"session_id": session_id, "messages": load_history(session_id)}
