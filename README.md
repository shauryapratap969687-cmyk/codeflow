# CodeFlow

CodeFlow is a standalone frontend and Python backend for learning and visualizing code execution. The backend has no project imports and uses only the Python standard library plus FastAPI/uvicorn when those packages are already installed.

## What problem this solves

Beginners can see that code ran, but not why a value changed, where a repeated call came from, or which recorded step produced an unexpected output. CodeFlow keeps the user's source beside a bounded execution trace and turns that evidence into a timeline, Teacher narration, a Value Journey, a Call tree, data-structure cells, Debug evidence, and an optional prediction quiz.

## Feature map

- **Teacher mode** — trace-derived Simple/Detailed Value Journey, English/Hindi captions, optional browser speech, replay, and keyboard navigation.
- **Call tree** — inline SVG call/return tree with recorded arguments, returns, repeated-call badges, selection highlighting, and a visible-node cap.
- **Data structures** — type-aware list/tuple/set/dict/string/scalar cells with added, removed, and changed markers.
- **Debug mode** — compare expected output, show whitespace visibly, map output lines to recorded trace steps, and inspect the last recorded write for values read by the line.
- **Quiz mode** — optional playback pauses that ask for a recorded scalar/list result or a branch outcome before revealing it.
- **Sharing and heat** — copy a code-containing URL hash without auto-running it, show recorded line heat, and load six plain-data demo samples.

### Architecture

```text
editor + controls
       │
       ├── /api/execute ── bounded Python/Node worker ── events + snapshots + metadata
       │                                              │
       └── frontend renderers ── timeline / Teacher / Call tree / Debug / Quiz / heat
```

The frontend never needs a build step or external script. The backend is the source of runtime values; visual explanations label missing evidence as unknown.

## Run it

Open `index.html` in a modern browser for the offline frontend, or start the backend from this folder:

```bash
python backend.py
```

If FastAPI and uvicorn are available, that command starts the FastAPI app at
`http://127.0.0.1:8010`. Otherwise it starts the built-in threaded
standard-library server on the same port. Port 8010 is intentional so this
standalone prototype does not collide with another local app using port 8000.
A different port can be selected with `CODEFLOW_PORT=8011`.

## Deploy on Render

This repository includes `requirements.txt` and `render.yaml`. In Render:

1. Create a **New Web Service** and connect the GitHub repository
   `shauryapratap969687-cmyk/codeflow`.
2. Choose the Python runtime. Render can use the checked-in Blueprint by
   selecting **New → Blueprint** and choosing this repository, or enter these
   commands manually:

   ```text
   Build Command: pip install -r requirements.txt
   Start Command: python backend.py
   ```

3. Deploy the service. Render supplies `PORT`; `backend.py` reads it and binds
   to the required public interface automatically.
4. Open the generated `https://<service-name>.onrender.com` URL. The frontend
   already calls `/api/execute`, `/api/verify`, `/api/ai`, and `/api/health` as
   same-origin routes, so no frontend API URL change is needed.

### Configure remote AI on Render

In the Render service's **Environment** tab, add these variables. Never put the
key in `index.html`, `app.js`, GitHub, or a public README.

```text
CODEFLOW_AI_API_KEY=your-provider-key
CODEFLOW_AI_BASE_URL=https://api.openai.com/v1
CODEFLOW_AI_MODEL=gpt-4o-mini
```

For OpenRouter, use:

```text
CODEFLOW_AI_BASE_URL=https://openrouter.ai/api/v1
CODEFLOW_AI_MODEL=openai/gpt-4o-mini
```

After saving environment variables, trigger a redeploy. Check the deployed
configuration at:

```text
https://<service-name>.onrender.com/api/health
```

It should return `status: "ok"` and `ai_configured: true` when the key is
present. If the key is absent, CodeFlow continues to run code but correctly
shows **Not configured** for remote AI.

The backend serves the local frontend and exposes:

- `POST /api/execute` — `{ language, code }` → `{ output, variables, diagnostics, events, line_info, teacher_mode }`
- `POST /api/verify` — `{ language, code }` → `{ valid, errors }`
- `POST /api/ai` — `{ mode, prompt, language, code, diagnostics, selected_event }` → a provider status plus a structured `{ answer, proposed_code? }`
- `GET /api/health` — backend status and execution capability information

`/api/ai` uses `status: "ok"` with `provider_status: "remote"` for a validated provider answer, `status: "not-configured"` with `configuration_status: "missing"` when no key is present, and `status: "error"` with provider/configuration diagnostics when a configured provider cannot be used. In every non-success case `answer` is `null`.

Python programs run in a bounded child process with captured stdout/stderr, syntax/runtime locations, normalized trace events, explicit before/after variable snapshots, call-stack snapshots, AST-derived `line_info` keyed by source line, loop metadata, source/output/event limits, and a hard timeout. The frontend Teacher mode uses those recorded values for Simple/Detailed value journeys, captions, optional English/Hindi browser speech, and step navigation. JavaScript runs in Node.js when it is on `PATH`, but remains explicitly `teacher_mode: "output-only"` with `trace_supported: false`; it never presents Python-style traced steps. The AI endpoint returns `status: "not-configured"` and no answer when `CODEFLOW_AI_API_KEY` is absent. With the variables below, it sends the current code, language, diagnostics, selected trace event, and user prompt to an OpenAI-compatible chat-completions endpoint. The provider must return a JSON object in `message.content` with a non-empty `answer` and may include a bounded `proposed_code` object.

### Deployment hardening

- User programs run in short-lived workers with bounded time, output, trace,
  result size, concurrency, and (where the host supports it) memory limits.
- Python workers receive a minimal environment and do not receive the AI key;
  process, socket, `/proc`, and out-of-directory writes are blocked by the
  worker audit guard. JavaScript runs in a restricted VM with no host
  `process`/`require` access and bounded virtual timers.
- Public requests have in-memory per-client run/AI rate limits and an AI daily
  budget. Tune them with `CODEFLOW_MAX_CONCURRENT`, `CODEFLOW_RATE_EXECUTE`,
  `CODEFLOW_RATE_AI`, and `CODEFLOW_AI_DAILY_LIMIT`.
- Static HTTP serving is allowlisted to the four frontend files. Backend
  source, `.env`, `render.yaml`, tests, and `.git` files are not served.
- A 4xx API response is shown as a request/diagnostic error in the UI instead
  of being misreported as offline mode.

## Security

The backend runs user Python in a short-lived child process with a minimal environment, audit-hook restrictions, bounded time/output/events/result size, concurrency and rate limits, and memory limits where the host supports them. It blocks process/socket access, `/proc` access where applicable, and writes outside a temporary work folder. JavaScript runs in a restricted Node VM with no host `process` or `require` access and bounded timers. Static serving is an explicit allowlist.

These controls are defense-in-depth for a teaching service, not a replacement for a hardened multi-tenant sandbox or an operating-system container. Do not treat public CodeFlow as a place to run secrets or hostile workloads.

## Limitations

- Python tracing is capped at 500 recorded events; long runs are marked truncated and the UI says when counts/evidence cover recorded steps only.
- JavaScript executes when Node is available, but Teacher-style Python tracing is intentionally unavailable and is reported as output-only.
- `input()` is not supported because runs are non-interactive; assign a sample value instead.
- Browser speech depends on the operating system's Web Speech voices. Captions remain available without speech.
- Share links contain the source code in the URL hash. Review the code before sharing and remember that URL history may retain it.

## Screenshots and live demo

- Screenshots/GIF: **TODO — add project screenshots here.**
- Live URL: **TODO — add the deployed URL here.**

### Configure remote AI

Set the key before starting `backend.py`. `CODEFLOW_AI_BASE_URL` is optional and defaults to `https://api.openai.com/v1`; it may also be a complete `/chat/completions` URL. `CODEFLOW_AI_MODEL` is optional and defaults to `gpt-4o-mini`.

PowerShell:

```powershell
$env:CODEFLOW_AI_API_KEY = "your-provider-key"
$env:CODEFLOW_AI_BASE_URL = "https://api.openai.com/v1"
$env:CODEFLOW_AI_MODEL = "gpt-4o-mini"
python backend.py
```

macOS/Linux shells:

```bash
export CODEFLOW_AI_API_KEY="your-provider-key"
export CODEFLOW_AI_BASE_URL="https://api.openai.com/v1"
export CODEFLOW_AI_MODEL="gpt-4o-mini"
python backend.py
```

For an OpenAI-compatible local provider, replace `CODEFLOW_AI_BASE_URL` and `CODEFLOW_AI_MODEL` with that provider's values while keeping the same `/v1` or `/chat/completions` convention. The key is sent as a Bearer token and is never returned by `/api/health`.

## Included interactions

- Responsive dark workspace with mobile stacked panels
- Python/JavaScript selector, editor line numbers, cursor position, find/replace
- Run, Analyze, Verify, timeline playback, flow map, variables, call stack, console, and diagnostics
- Compact laptop-first visualizer with a single-screen editor, selected-step explanation, timeline, state, output, and checks
- Optional Web Audio step feedback with event-specific tones; click **Sound off** once to unlock it, then each selected/playback step is audible
- Teacher mode with trace-derived Value Journey cards, Simple/Detailed views, English/Hindi captions, browser speech controls, replay, auto-advance, and keyboard navigation
- FlowMind AI prompt chips and `/api/ai` integration with visible `Remote AI`, `Local fallback`, or `Not configured` provider state
- Import, export, undo, restore, clear console, examples, challenge loader, and theme toggle

## Checks

```bash
node --check app.js
node smoke-test.js
npm test
python -m unittest discover -s tests -v
```

The JavaScript smoke test is dependency-free and checks the static entry point. `npm test` also runs DOM-free VM checks for Teacher narration, Call tree, data structures, Debug mode, Quiz mode, sharing, and line heat. The Python tests exercise execution, Teacher metadata and ten arbitrary programs, limits/diagnostics, verification, JavaScript capability handling, missing AI credentials, a mocked structured provider response with a proposed code change, sandbox boundaries, rate limits, result-size robustness, and static-file exposure. The hardening suite is in `tests/test_hardening.py`.
