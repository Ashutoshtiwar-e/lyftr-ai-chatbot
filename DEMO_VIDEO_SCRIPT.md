# Demo video script — Lyftr AI chatbot (2–3 minutes, voiceover)

Record on screen with your voice. Fill in the two URLs after deployment,
then read the lines below while you do the actions. Keep it under 3 minutes.

- Frontend URL: [FILL IN — e.g. https://lyftr-ai-chatbot.onrender.com]
- Backend URL: [FILL IN — e.g. https://lyftr-ai-chatbot-api.onrender.com]
- Model: `gemini/gemini-3.6-flash` — see README for why the assignment's
  example `gemini-2.0-flash` was not used (verified retired, 404, on 2026-11-24).

## 0:00–0:20 — Intro (show the live frontend)

> "Hi, I'm Ashutosh Kumar Tiwari. This is my Lyftr AI assignment — a
> full-stack LLM chatbot with key failover and response caching.
> Frontend is React, backend is FastAPI with LiteLLM, and history is
> stored in SQLite. Both are deployed live on Render."

Show the frontend URL in the browser address bar. Point at the chat box
and the system-prompt field.

## 0:20–0:55 — Live inference (show a real answer + metadata)

Type: `What is the capital of France?` and send it.

> "This is a live call. Under the reply you can see the metadata for
> this request only — resolution source LIVE_INFERENCE, model
> gemini-3.6-flash, latency in milliseconds, and input and output tokens."

Point at each metadata line under the reply.

Then ask a follow-up in the same chat: `And what river runs through it?`

> "The bot remembers the earlier message — this is multi-turn, the
> history is stored in SQLite per session."

## 0:55–1:30 — Cache hit (show LLM_CACHE)

Refresh the page — a new session starts on each load. Ask the exact
same first question: `What is the capital of France?`

> "Same first message, new session. This time it comes back as
> LLM_CACHE — identical reply text, same token counts, key is null,
> and it is much faster than the live call."

Point at `LLM_CACHE`, the identical text, and the lower latency.

## 1:30–2:15 — Failover (show X-Simulate-Failure)

Switch to a terminal. Run, with the backend URL filled in:

```bash
curl -X POST <BACKEND_URL>/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"s-fail","message":"Say hello in one line"}'
```

> "Now I simulate key_1 failing with a 429. The app still returns a
> real answer — from key_2, with fallback_attempts 1. No broken screen,
> no error wrapped in a 200."

Then run:

```bash
curl -X POST <BACKEND_URL>/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"s-dead","message":"Say hello"}'
```

> "If every key fails, it returns a clean 503 — all providers
> unavailable — and nothing is added to the history."

## 2:15–2:45 — Health and history (show the endpoints)

In the terminal or browser, show:

```bash
curl <BACKEND_URL>/health
curl <BACKEND_URL>/sessions/s-7f3a/messages
```

Use the real session id from your first chat instead of `s-7f3a` if
needed. Point at `"status": "ok", "keys_configured": 2,
"cache_enabled": true`, and at the stored history, oldest first.

## 2:45–3:00 — Close

> "That's the full flow — live inference, caching, failover, and
> per-session history, all deployed live. The code, Docker setup and
> README are in the public GitHub repo linked in the submission form.
> Thank you."

## Recording checklist

- [ ] Frontend URL and backend URL filled in above, and both awake
  (Render free tier sleeps — open both URLs a minute before recording)
- [ ] First question is exactly `What is the capital of France?`
  in both the live and the cache demos, so the cache hit is obvious
- [ ] Terminal text is large enough to read on video
- [ ] Video is 2–3 minutes, with your voice throughout
- [ ] Upload to Drive and use that link in the submission form
