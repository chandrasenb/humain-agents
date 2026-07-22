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
`{reply: str, state: ConversationState}`.

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

## Environment variables (injected by the platform)

| Var | Source | Required for |
|---|---|---|
| `GOOGLE_ACCESS_TOKEN`, `GOOGLE_ACCOUNT_EMAIL` | Connectors hub OAuth (`agent.yaml`'s `deployment.connectors: [{provider: google}]`) | All Calendar API calls |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Secret Manager (`agent.yaml`'s `deployment.secrets: [openai-api-key, openai-base-url]`) — same secrets `agentic-invoice-processing` uses, pointed at NVIDIA NIM (`https://integrate.api.nvidia.com/v1`) | `/chat` only |
| `OPENAI_MODEL_ID` | Optional override | `/chat` only — defaults to `nvidia/nemotron-3-ultra-550b-a55b` |

The three structured endpoints work with no LLM key at all — only `/chat`
needs one.
