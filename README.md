# CodeFlow

CodeFlow is a standalone frontend and Python backend for learning and visualizing code execution. The backend has no project imports and uses only the Python standard library plus FastAPI/uvicorn when those packages are already installed.

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

- `POST /api/execute` — `{ language, code }` → `{ output, variables, diagnostics }`
- `POST /api/verify` — `{ language, code }` → `{ valid, errors }`
- `POST /api/ai` — `{ mode, prompt, language, code, diagnostics, selected_event }` → a provider status plus a structured `{ answer, proposed_code? }`
- `GET /api/health` — backend status and execution capability information

`/api/ai` uses `status: "ok"` with `provider_status: "remote"` for a validated provider answer, `status: "not-configured"` with `configuration_status: "missing"` when no key is present, and `status: "error"` with provider/configuration diagnostics when a configured provider cannot be used. In every non-success case `answer` is `null`.

Python programs run in a bounded child process with captured stdout/stderr, syntax/runtime locations, normalized trace events, variable snapshots, call-stack snapshots, source/output/event limits, and a hard timeout. JavaScript runs in Node.js when it is on `PATH`; otherwise the API returns an explicit unsupported response. The AI endpoint returns `status: "not-configured"` and no answer when `CODEFLOW_AI_API_KEY` is absent. With the variables below, it sends the current code, language, diagnostics, selected trace event, and user prompt to an OpenAI-compatible chat-completions endpoint. The provider must return a JSON object in `message.content` with a non-empty `answer` and may include a bounded `proposed_code` object.

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
- FlowMind AI prompt chips and `/api/ai` integration with visible `Remote AI`, `Local fallback`, or `Not configured` provider state
- Import, export, undo, restore, clear console, examples, challenge loader, and theme toggle

## Checks

```bash
node --check app.js
node smoke-test.js
python -m unittest discover -s tests -v
```

The JavaScript smoke test is dependency-free and checks the static entry point. The Python tests exercise execution, limits/diagnostics, verification, JavaScript capability handling, missing AI credentials, and a mocked structured provider response with a proposed code change.
