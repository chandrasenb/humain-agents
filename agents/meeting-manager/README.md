# meeting-manager

Reads and schedules Google Calendar events on the user's behalf, with
conflict detection and alternative-slot suggestions. Requires the user to
have connected a Google account via the Connectors hub (HUMAIN-2060).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/chat` | Natural-language entry point — see below. |
| `POST` | `/fetch_calendar_events` | Structured: `{query}` → list of events for a time range. |
| `POST` | `/conflict_check` | Structured: `{when, duration_minutes}` → is that slot free? |
| `POST` | `/schedule_meeting` | Structured: `{title, when, duration_minutes, attendees, description}` → books it, or returns conflicts + alternatives. |

The three structured endpoints all accept and return an optional
`state: {messages: [{role, content}]}` — in-context conversation memory,
threaded straight through (no Redis/pgvector yet — HUMAIN-1966/1967).

## `/chat` — natural-language routing

```
POST /chat
{"message": "what's on my calendar this week?", "state": null}
→ {"reply": "Here's what I found (2 event(s)): ...", "state": {...}}
```

Request: `{message: str, state: ConversationState | null}`. Response:
`{reply: str, state: ConversationState, events: list[EventSummary] | null}`.

`events` is populated only for `fetch_calendar_events` (the events found)
and a successful `schedule_meeting` (the one event just created) — `null`
otherwise (including a `schedule_meeting` that hit a conflict, and always
for `conflict_check`). The [web UI](#web-ui) renders it as a card carousel
under the reply text; a plain API caller can ignore the field entirely.

How it works (this is `../agent.yaml`'s `router` → tool → `format_response`
graph, executed directly in Python rather than through H2O-O — see the note
at the top of `agent.yaml`):

1. The full conversation history (`state.messages`) plus the new user
   message go to an LLM (NVIDIA NIM, `nvidia/nemotron-3-ultra-550b-a55b`,
   via the OpenAI-compatible API — `OPENAI_API_KEY` + `OPENAI_BASE_URL`)
   with three function definitions: `fetch_calendar_events`,
   `conflict_check`, `schedule_meeting`. `tool_choice="required"` forces it
   to pick exactly one.
2. `src/main.py`'s `/chat` handler parses the returned tool name + JSON
   arguments and calls the matching Python function **directly** (e.g.
   `fetch_calendar_events(FetchEventsRequest(**args))`) — not an HTTP
   self-call, just a normal function call reusing the exact same code the
   structured endpoints use.
3. The structured result goes through `_format_reply()`, a plain template
   per result shape, to produce the `reply` string.
4. The user's message and the reply are appended to `state.messages` and
   returned, so the next `/chat` call has full history.

### Why `format_response` isn't an LLM call

`agent.yaml` declares `format_response` as `type: transform` (deterministic),
not a second `llm` node. The three tool results are already well-structured
(an event list, a conflict + alternatives list, a scheduled/conflict
status) — a template renders them naturally without a second model
round-trip. A second LLM call would only add latency, cost, and a new
failure mode (hallucination) for no real benefit here. If replies ever need
to get more conversational than the templates in `_format_reply()` allow,
that's the point to swap it for a real `llm` node — not before.

## Web UI

A minimal React + Vite single-page chat interface lives in `frontend/` and
is served by this same FastAPI app as static assets — no separate frontend
deployment, no separate origin, no CORS config needed.

- **What it is**: a message input + send button, chat history (your
  messages right-aligned, the agent's replies left-aligned), and — when a
  `/chat` reply carries `events` — a horizontal scroll-snap card carousel
  under that reply showing each event's title, time range, attendees, and
  a "Join meeting" link when a `hangoutLink` is present. Plain CSS
  `scroll-snap-type`/`scroll-snap-align`, no carousel library.
- **How it talks to the backend**: `fetch('/chat', {method: 'POST', body:
  {message, state}})` — a same-origin relative call, since the built UI is
  served from the same app it's calling.
- **Build**: `frontend/` is compiled at Docker build time, not committed as
  built output. The `Dockerfile` is a two-stage build:
  1. `node:20-slim` stage: `npm install` + `npm run build` inside
     `frontend/`, producing `frontend/dist/`.
  2. The existing `python:3.11-slim` runtime stage `COPY --from`s that
     `dist/` directory in as `./static`. `src/main.py` mounts `./static` at
     `/` via `StaticFiles(html=True)` **only if the directory exists** — a
     plain `uvicorn src.main:app` for backend-only local dev/tests (no
     Docker, no frontend build) still starts up fine with just the API
     routes.
- **Local frontend dev** (`frontend/`, iterating on the UI itself, hot
  reload): `npm install && npm run dev` — but note this runs on Vite's own
  dev server (default port 5173), not against a live backend; there's no
  dev-server proxy configured yet, so `fetch('/chat')` will 404 unless you
  also serve the built UI through the FastAPI app (rebuild the Docker image,
  or run `npm run build` and point `_STATIC_DIR` at the resulting `dist/`).
  For an actual round-trip test, build and run the full image (below).
- **Full round-trip test**: `docker build -t meeting-manager .` then
  `docker run -p 8080:8080 -e OPENAI-API-KEY=... -e OPENAI-BASE-URL=... -e
  OPENAI-MODEL-ID=... meeting-manager`, then open `http://localhost:8080/`.

## Environment variables (injected by the platform)

| Var | Source | Required for |
|---|---|---|
| `GOOGLE_ACCESS_TOKEN`, `GOOGLE_ACCOUNT_EMAIL` | Connectors hub OAuth (`agent.yaml`'s `deployment.connectors: [{provider: google}]`) | All Calendar API calls |
| `OPENAI-API-KEY`, `OPENAI-BASE-URL` | Secret Manager (`agent.yaml`'s `deployment.secrets: [openai-api-key, openai-base-url]`), pointed at NVIDIA NIM (`https://integrate.api.nvidia.com/v1`). Hyphenated, not `OPENAI_API_KEY` — the platform's shared pool-secret env vars use hyphens, confirmed via `kubectl` against a running pod's `envFrom` secret; the openai SDK's own implicit env lookup (which expects underscores) is not relied on. | `/chat` only |
| `OPENAI-MODEL-ID` | Optional override, same hyphenated-name reasoning as above | `/chat` only — defaults to `nvidia/nemotron-3-ultra-550b-a55b` |

The three structured endpoints work with no LLM key at all — only `/chat`
needs one. The web UI only ever calls `/chat`.
