# Lyftr AI — LLM Chatbot with Key Failover and Response Caching

Full-stack chatbot: React frontend, Python (FastAPI) backend, SQLite storage,
LiteLLM gateway with a 2-key pool, automatic failover and response caching.

## 1. Live URLs

- **Frontend:** https://lyftr-ai-chatbot-bpoa.onrender.com
- **Backend:** https://lyftr-ai-chatbot-api.onrender.com
- **GitHub repo:** https://github.com/Ashutoshtiwar-e/lyftr-ai-chatbot
- **Demo video (2–3 min):** _TODO — Drive / YouTube URL, to be added after recording_

Deployment notes: Render assigned the frontend the `-bpoa` suffix because
`lyftr-ai-chatbot.onrender.com` was already taken. On Render's free tier
there is no persistent disk, so the deployed backend uses
`DATABASE_URL=sqlite:///./app.db`; in Docker, the database lives under the
`/data` volume as specified (`sqlite:////data/app.db`), so history survives
container restarts there. The free backend also sleeps when idle, so the
first request can take ~50 s to wake up.

## 2. Run locally: with Docker and without

### With Docker (the required path)

```bash
cp .env.example .env        # put your 2 real Gemini keys in LLM_API_KEYS
make up                     # backend on http://localhost:8000, frontend on http://localhost:3000
make down                   # stop everything
```

`docker-compose.yml` mounts a named volume `lyftr-data` at `/data`, so the
SQLite file (`/data/app.db`) survives container restarts.

### Without Docker

Backend:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export LLM_API_KEYS="key-one,key-two"          # at least 2 keys
export DATABASE_URL="sqlite:///./app.db"
export ENABLE_FAILURE_SIMULATION=true
export ALLOWED_ORIGINS="http://localhost:3000,http://localhost:5173"
uvicorn main:app --host 0.0.0.0 --port 8000
```

Frontend (in a second terminal):

```bash
cd frontend
npm install
VITE_BACKEND_URL=http://localhost:8000 npm run dev   # http://localhost:5173
# production build:  npm run build && npm run preview  # http://localhost:3000
```

Tests:

```bash
make test        # backend pytest (LiteLLM mocked — no real keys needed)
```

### Deploying to Render (the plan agreed for this submission)

- **Backend — Render Web Service:** root directory `backend`, build command
  `pip install -r requirements.txt`, start command
  `uvicorn main:app --host 0.0.0.0 --port $PORT`. On a paid plan, add a
  persistent disk mounted at `/data` and set `DATABASE_URL=sqlite:////data/app.db`.
  On the free plan used here there is no disk, so `DATABASE_URL` is
  `sqlite:///./app.db` instead (see Live URLs above). Set env vars
  `LLM_API_KEYS` (2 real keys), `LLM_MODEL=gemini/gemini-3.6-flash`,
  `ALLOWED_ORIGINS=<frontend URL>`, `ENABLE_FAILURE_SIMULATION=true`.
  Note: the Dockerfile hard-codes port 8000 for local Docker; on Render the
  start command above uses `$PORT` instead.
- **Frontend — Render Static Site:** root directory `frontend`, build command
  `npm install && npm run build`, publish directory `dist`, and set the
  build-time env var `VITE_BACKEND_URL=<backend URL>`.
- Render free-tier services sleep when idle, so the first request after a
  pause can take ~30–50 s to wake up — that is expected, not an error.

## 3. Design decisions: key fallback, caching, SQLite session storage

**Key fallback (LiteLLM).** The backend calls `litellm.completion()` once per
key, in order (`key_1`, then `key_2`), always with the same model
`gemini/gemini-3.6-flash`.

> **Why `gemini-3.6-flash`, not the assignment's example `gemini-2.0-flash`?**
> The example model was verified retired on 2026-11-24: a direct Gemini API
> call to `gemini-2.0-flash` returns 404 "This model … is no longer
> available", and `gemini-2.5-flash` also 404s for new users. `gemini-3.6-flash`
> was verified working the same day with a real `generateContent` call, so
> it is used as the working flash replacement. Shipping with `2.0-flash`
> would have made every live call fail and return
> `503 all providers unavailable`. Any provider failure on a key — 429, quota error,
5xx, or any other exception from LiteLLM — is caught, counted in
`fallback_attempts`, and the next key is tried. If both Gemini keys fail
for real, and an `OPENROUTER_API_KEY` is set, the backend tries the same
model once more through OpenRouter (`openrouter/google/gemini-3.6-flash`,
reported as `key_3`) — a separate paid route that does not share the
Gemini free-tier daily quota. The failover simulation never uses it, so
`X-Simulate-Failure: *=429` still returns the clean 503. If every route
fails the endpoint returns `503 {"detail": "all providers unavailable"}`
and nothing is written to history. The `X-Simulate-Failure` header (only honoured when
`ENABLE_FAILURE_SIMULATION=true`) makes a chosen key behave as if its
provider returned that status, for that request only, so failover can be
demonstrated without burning real quota.

**Caching.** The cache key is `SHA-256(model + full message list sent to the
LLM)` — system prompt + stored history + new message. The same first message
in a *new* session therefore hits the cache; a different message never does.
On a hit the identical reply text is returned with the usage stored with the
cached response, `resolution_source: LLM_CACHE`, `key_id: null`,
`fallback_attempts: 0`, and a latency of ~1 ms (a dict lookup, far faster
than a live call). LiteLLM's built-in cache is also enabled
(`caching=True` on every `litellm.completion` call); the explicit in-memory
cache is what guarantees the exact metadata behaviour above, because
LiteLLM's cache alone does not return the stored token usage / key id in
the shape this contract requires.

**SQLite session storage.** One `messages` table
(`id, session_id, role, content, metadata JSON, created_at`) plus an index
on `(session_id, id)`, and a small `sessions` table holding each session's
system prompt. History is loaded per `session_id` on every `/chat` call, so
the client only ever sends the new message and the bot remembers earlier
turns. Sessions are never mixed: every query is filtered by `session_id`.
The schema is created automatically on startup, the file lives under the
`/data` Docker volume via `DATABASE_URL`, and a failed request is never
stored. Token counts are per-request only: `input_tokens` covers system +
history + new message for *that* request, `output_tokens` covers *that*
reply, taken from `response.usage` when present, otherwise from a
deterministic estimate (~1 token / 4 chars) so equal inputs report equal
counts.

**System prompt.** Default is `"You are a helpful AI assistant."` A
`system_prompt` supplied with a message overrides it for that session and
is remembered for later turns (stored in the `sessions` table) until
overridden again. It is part of the cache key and is never returned by the
history endpoint.

## 4. Verification: how to see failover, cache hits and per-message token usage

With the backend running (replace the base URL with the deployed one):

```bash
# Health — 2 keys, cache on
curl http://localhost:8000/health
# {"status":"ok","keys_configured":2,"cache_enabled":true}

# Live call — note LIVE_INFERENCE, key_1, fallback_attempts 0
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s-7f3a","message":"What is the capital of France?"}'

# Same first message, NEW session — identical reply, LLM_CACHE, key_id null
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s-9b1c","message":"What is the capital of France?"}'

# Failover — key_1 simulated as 429, answer still comes back from key_2
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"s-fail","message":"Say hello"}'
# -> 200, metadata.key_id == "key_2", metadata.fallback_attempts == 1

# All keys fail — 503, nothing stored
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"s-dead","message":"Say hello"}'
# -> 503 {"detail":"all providers unavailable"}

# History for a session (oldest first, no system prompt)
curl http://localhost:8000/sessions/s-7f3a/messages

# Unknown session / bad input
curl http://localhost:8000/sessions/nope/messages        # 404
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s-1","message":"   "}'              # 422
```

In the UI, every assistant reply shows its own metadata underneath it:
source (`LIVE_INFERENCE` / `LLM_CACHE`), model, latency in ms, and input /
output tokens. Send the same question twice in two fresh page loads (each
load starts a new session) and watch the second one come back as
`LLM_CACHE` — faster, with the same text and the same token counts.

## 5. Setup Used: your editor and AI tools

- Built with Muse (Meta's AI agent) in this VM — editor: terminal / file
  tools in the workspace; Python 3.12, Node 24.
- AI tools: Muse for code generation and testing; LiteLLM documentation
  for the gateway / caching API. No other code assistants were used.
- Backend tests mock `litellm.completion`, so they pass with placeholder
  keys; live calls were verified against the deployed backend (see
  Verification above).

## 6. Bonus features (if any)

- **Per-session system prompt that persists** across turns (stored in
  SQLite, included in the cache key) — the assignment only requires an
  optional per-call override.
- **Deterministic token fallback** when a provider returns no usage object,
  so equal-length first messages in different sessions still report
  near-identical `input_tokens`.
- **Failover simulation bypasses the cache**, so the `X-Simulate-Failure`
  hook always demonstrates a real live failover even if that exact prompt
  was answered (and cached) before.
