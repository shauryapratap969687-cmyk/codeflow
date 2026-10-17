"""Standalone CodeFlow backend.

The application deliberately keeps all user-program execution in short-lived
child processes.  FastAPI/uvicorn are used when present, while the same
handlers are available through the standard-library fallback server.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request as URLRequest, urlopen


BASE_DIR = Path(__file__).resolve().parent
MAX_SOURCE_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
MAX_EVENTS = 500
MAX_VARIABLES = 100
MAX_STACK = 50
EXECUTION_TIMEOUT_SECONDS = 2.0
AI_TIMEOUT_SECONDS = 12.0
MAX_AI_PROMPT_BYTES = 8 * 1024
MAX_AI_CONTEXT_ITEMS = 50
MAX_AI_ANSWER_BYTES = 16 * 1024
RESULT_FILE_LIMIT = 256 * 1024
RESULT_MARKER = "CodeFlow backend"


def _diagnostic(
    title: str,
    detail: str,
    *,
    line: int | None = None,
    column: int | None = None,
    kind: str = "error",
    **extra: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "message": detail,
        "detail": detail,
    }
    if line is not None:
        item["line"] = line
    if column is not None:
        item["column"] = column
    item.update(extra)
    return item


def _validate_payload(payload: Any) -> tuple[str, str] | tuple[None, dict[str, Any]]:
    if not isinstance(payload, dict):
        return None, {"error": "Request body must be a JSON object."}
    language = payload.get("language", "python")
    source = payload.get("code", "")
    if not isinstance(language, str) or language.lower() not in {"python", "javascript", "js"}:
        return None, {"error": "language must be 'python' or 'javascript'."}
    if not isinstance(source, str):
        return None, {"error": "code must be a string."}
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return None, {
            "error": "Source exceeds the 64 KiB limit.",
            "limit": MAX_SOURCE_BYTES,
        }
    return ("javascript" if language.lower() == "js" else language.lower()), source


def _split_output(value: str) -> list[str]:
    return value.splitlines() if value else []


def _read_limited(pipe: Any, limit: int) -> tuple[bytes, bool]:
    """Drain a child pipe without allowing unbounded output in this process."""
    data = bytearray()
    total = 0
    truncated = False
    while True:
        chunk = pipe.read(4096)
        if not chunk:
            break
        if len(data) < limit:
            remaining = limit - len(data)
            data.extend(chunk[:remaining])
        total += len(chunk)
        if total > limit:
            truncated = True
    return bytes(data), truncated


def _run_process(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float = EXECUTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return {
            "started": started,
            "returncode": None,
            "stdout": b"",
            "stderr": str(exc).encode("utf-8", "replace"),
            "stdout_truncated": False,
            "stderr_truncated": False,
            "spawn_error": str(exc),
        }

    stdout_result: list[tuple[bytes, bool]] = []
    stderr_result: list[tuple[bytes, bool]] = []

    def drain(pipe: Any, destination: list[tuple[bytes, bool]]) -> None:
        destination.append(_read_limited(pipe, MAX_OUTPUT_BYTES))

    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout_result), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr_result), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    stdout = stdout_result[0] if stdout_result else (b"", False)
    stderr = stderr_result[0] if stderr_result else (b"", False)
    return {
        "started": started,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
        "returncode": process.returncode,
        "stdout": stdout[0],
        "stderr": stderr[0],
        "stdout_truncated": stdout[1],
        "stderr_truncated": stderr[1],
        "timed_out": timed_out,
    }


def _decode(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def _bounded_text(value: str, limit: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", "replace"), True


PYTHON_RUNNER = r'''
import json
import os
import sys
import traceback

SOURCE_PATH = sys.argv[1]
RESULT_PATH = sys.argv[2]
SOURCE_NAME = "codeflow_user.py"
MAX_EVENTS = int(os.environ.get("CODEFLOW_MAX_EVENTS", "500"))
MAX_VARIABLES = int(os.environ.get("CODEFLOW_MAX_VARIABLES", "100"))
MAX_STACK = int(os.environ.get("CODEFLOW_MAX_STACK", "50"))
MAX_VALUE = 800


class LimitedText:
    def __init__(self, limit):
        self.limit = limit
        self.value = []
        self.size = 0
        self.truncated = False

    def write(self, text):
        text = str(text)
        remaining = self.limit - self.size
        if remaining > 0:
            self.value.append(text[:remaining])
            self.size += min(len(text), remaining)
        if len(text) > remaining:
            self.truncated = True
        return len(text)

    def flush(self):
        return None

    def isatty(self):
        return False

    def getvalue(self):
        return "".join(self.value)


def safe_value(value, depth=0):
    if depth > 3:
        return "<nested>"
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > MAX_VALUE:
            return value[:MAX_VALUE] + "…"
        return value
    if isinstance(value, (list, tuple, set)):
        return [safe_value(item, depth + 1) for item in list(value)[:30]]
    if isinstance(value, dict):
        return {str(key)[:80]: safe_value(item, depth + 1) for key, item in list(value.items())[:30]}
    try:
        text = repr(value)
    except Exception:
        text = "<unrepresentable>"
    return text[:MAX_VALUE]


def snapshot(values):
    result = {}
    for key, value in list(values.items())[:MAX_VARIABLES]:
        if not str(key).startswith("__"):
            result[str(key)] = safe_value(value)
    return result


events = []
stack = []
events_truncated = False


def trace(frame, event, arg):
    global events_truncated
    if frame.f_code.co_filename != SOURCE_NAME:
        return trace
    function = frame.f_code.co_name
    if event == "call":
        if len(stack) < MAX_STACK:
            stack.append(function)
    elif event == "return":
        event_name = "return"
    else:
        event_name = event
    if event in ("call", "line", "return", "exception"):
        if len(events) < MAX_EVENTS:
            item = {
                "sequence": len(events),
                "type": event,
                "line": frame.f_lineno,
                "function": function,
                "variables": snapshot(frame.f_locals),
                "call_stack": list(stack[-MAX_STACK:]),
            }
            if event == "exception" and isinstance(arg, tuple) and len(arg) > 1:
                item["exception"] = str(arg[1])[:MAX_VALUE]
            events.append(item)
        else:
            events_truncated = True
    if event == "return" and stack:
        stack.pop()
    return trace


stdout_capture = LimitedText(16384)
stderr_capture = LimitedText(16384)
real_stdout = sys.stdout
real_stderr = sys.stderr
sys.stdout = stdout_capture
sys.stderr = stderr_capture
namespace = {"__name__": "__main__"}
metadata = {
    "status": "completed",
    "diagnostics": [],
    "events": events,
    "variables": {},
    "call_stack": [],
    "events_truncated": False,
}
try:
    with open(SOURCE_PATH, "r", encoding="utf-8") as source_file:
        source = source_file.read()
    try:
        compiled = compile(source, SOURCE_NAME, "exec")
    except SyntaxError as exc:
        metadata["status"] = "error"
        metadata["diagnostics"] = [{
            "kind": "syntax",
            "title": "SyntaxError",
            "message": str(exc.msg),
            "detail": str(exc.msg),
            "line": exc.lineno,
            "column": exc.offset,
            "end_line": getattr(exc, "end_lineno", None),
            "end_column": getattr(exc, "end_offset", None),
        }]
    else:
        sys.settrace(trace)
        try:
            exec(compiled, namespace, namespace)
        except BaseException as exc:
            metadata["status"] = "error"
            frames = traceback.extract_tb(exc.__traceback__)
            user_frame = next((item for item in reversed(frames) if item.filename == SOURCE_NAME), None)
            metadata["diagnostics"] = [{
                "kind": "runtime",
                "title": type(exc).__name__,
                "message": str(exc) or type(exc).__name__,
                "detail": str(exc) or type(exc).__name__,
                "line": user_frame.lineno if user_frame else None,
                "column": None,
            }]
        finally:
            sys.settrace(None)
finally:
    sys.stdout = real_stdout
    sys.stderr = real_stderr
metadata["events"] = events
metadata["events_truncated"] = events_truncated
metadata["variables"] = snapshot(namespace)
metadata["call_stack"] = list(stack[-MAX_STACK:])
metadata["stdout"] = stdout_capture.getvalue()
metadata["stderr"] = stderr_capture.getvalue()
metadata["stdout_truncated"] = stdout_capture.truncated
metadata["stderr_truncated"] = stderr_capture.truncated
with open(RESULT_PATH, "w", encoding="utf-8") as result_file:
    json.dump(metadata, result_file, ensure_ascii=False)
'''


def _python_result_from_child(child: dict[str, Any], result_path: Path) -> dict[str, Any]:
    stdout = _decode(child["stdout"])
    stderr = _decode(child["stderr"])
    if child.get("timed_out"):
        return {
            "status": "timeout",
            "language": "python",
            "output": _split_output(stdout),
            "stdout": stdout,
            "stderr": stderr,
            "variables": {},
            "events": [],
            "call_stack": [],
            "diagnostics": [_diagnostic(
                "Execution timed out",
                f"Python execution exceeded the {EXECUTION_TIMEOUT_SECONDS:g}-second limit.",
                kind="timeout",
            )],
        }
    metadata: dict[str, Any] = {}
    try:
        if result_path.exists() and result_path.stat().st_size <= RESULT_FILE_LIMIT:
            metadata = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        metadata = {}
    if not metadata:
        message = stderr.strip() or "Python worker did not return a result."
        return {
            "status": "error",
            "language": "python",
            "output": _split_output(stdout),
            "stdout": stdout,
            "stderr": stderr,
            "variables": {},
            "events": [],
            "call_stack": [],
            "diagnostics": [_diagnostic("Python worker error", message)],
        }
    diagnostics = metadata.get("diagnostics") or []
    stdout = metadata.get("stdout", stdout)
    stderr = metadata.get("stderr", stderr)
    stdout, stdout_text_truncated = _bounded_text(stdout)
    stderr, stderr_text_truncated = _bounded_text(stderr)
    if child.get("returncode") not in (0, None) and not diagnostics:
        diagnostics = [_diagnostic("Python worker error", stderr.strip() or "The worker exited unexpectedly.")]
    return {
        "status": metadata.get("status", "error"),
        "language": "python",
        "output": _split_output(stdout),
        "stdout": stdout,
        "stderr": stderr,
        "variables": metadata.get("variables", {}),
        "events": metadata.get("events", []),
        "trace": metadata.get("events", []),
        "call_stack": metadata.get("call_stack", []),
        "diagnostics": diagnostics,
        "limits": {
            "source_bytes": MAX_SOURCE_BYTES,
            "output_bytes": MAX_OUTPUT_BYTES,
            "events": MAX_EVENTS,
            "stdout_truncated": bool(child.get("stdout_truncated") or metadata.get("stdout_truncated") or stdout_text_truncated),
            "stderr_truncated": bool(child.get("stderr_truncated") or metadata.get("stderr_truncated") or stderr_text_truncated),
            "events_truncated": bool(metadata.get("events_truncated")),
        },
    }


def execute_python(source: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codeflow-python-") as workdir:
        work = Path(workdir)
        source_path = work / "source.py"
        result_path = work / "result.json"
        source_path.write_text(source, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "CODEFLOW_MAX_EVENTS": str(MAX_EVENTS),
            "CODEFLOW_MAX_VARIABLES": str(MAX_VARIABLES),
            "CODEFLOW_MAX_STACK": str(MAX_STACK),
        }
        child = _run_process(
            [sys.executable, "-I", "-S", "-c", PYTHON_RUNNER, str(source_path), str(result_path)],
            cwd=workdir,
            env=env,
        )
        result = _python_result_from_child(child, result_path)
        result["duration_ms"] = round((time.monotonic() - child["started"]) * 1000, 2)
        return result


JS_RUNNER = r'''
const fs = require("fs");
const vm = require("vm");
const sourcePath = process.argv[1];
const resultPath = process.argv[2];
const source = fs.readFileSync(sourcePath, "utf8");
const limit = 16384;
const output = [];
const errors = [];
let outputSize = 0;
function bounded(target, args) {
  const text = args.map((value) => {
    try { return typeof value === "string" ? value : JSON.stringify(value); }
    catch (_) { return String(value); }
  }).join(" ");
  if (outputSize < limit) {
    target.push(text.slice(0, limit - outputSize));
    outputSize += Math.min(text.length, limit - outputSize);
  }
}
const sandbox = {
  console: {
    log: (...args) => bounded(output, args),
    info: (...args) => bounded(output, args),
    warn: (...args) => bounded(errors, args),
    error: (...args) => bounded(errors, args),
  },
};
const result = { status: "completed", diagnostics: [], variables: {}, events: [], call_stack: [] };
try {
  const context = vm.createContext(sandbox);
  const script = new vm.Script(source, { filename: "codeflow_user.js", displayErrors: true });
  script.runInContext(context, { timeout: 2000, displayErrors: true });
  for (const [key, value] of Object.entries(sandbox)) {
    if (key === "console" || key.startsWith("_")) continue;
    try { result.variables[key] = JSON.parse(JSON.stringify(value)); }
    catch (_) { result.variables[key] = String(value); }
  }
} catch (error) {
  const message = String(error && error.message ? error.message : error);
  const stack = String(error && error.stack ? error.stack : "");
  const match = stack.match(/codeflow_user\.js:(\d+)(?::(\d+))?/);
  result.status = "error";
  result.diagnostics.push({
    kind: error && error.name === "SyntaxError" ? "syntax" : "runtime",
    title: error && error.name ? error.name : "JavaScriptError",
    message,
    detail: message,
    line: match ? Number(match[1]) : null,
    column: match && match[2] ? Number(match[2]) : null,
  });
}
result.output = output;
result.stderr = errors.join("\n");
fs.writeFileSync(resultPath, JSON.stringify(result), "utf8");
'''


def _node_command() -> str | None:
    return shutil.which("node") or shutil.which("nodejs")


def execute_javascript(source: str) -> dict[str, Any]:
    node = _node_command()
    if not node:
        return {
            "status": "unsupported",
            "supported": False,
            "language": "javascript",
            "output": [],
            "stdout": "",
            "stderr": "",
            "variables": {},
            "events": [],
            "call_stack": [],
            "diagnostics": [_diagnostic(
                "JavaScript unavailable",
                "Node.js is not installed or is not available on PATH.",
                kind="unsupported",
            )],
        }
    with tempfile.TemporaryDirectory(prefix="codeflow-javascript-") as workdir:
        work = Path(workdir)
        source_path = work / "source.js"
        result_path = work / "result.json"
        source_path.write_text(source, encoding="utf-8")
        env = os.environ.copy()
        env["NODE_NO_WARNINGS"] = "1"
        child = _run_process(
            [node, "-e", JS_RUNNER, "--", str(source_path), str(result_path)],
            cwd=workdir,
            env=env,
        )
        stdout = _decode(child["stdout"])
        stderr = _decode(child["stderr"])
        if child.get("timed_out"):
            return {
                "status": "timeout",
                "language": "javascript",
                "output": _split_output(stdout),
                "stdout": stdout,
                "stderr": stderr,
                "variables": {},
                "events": [],
                "call_stack": [],
                "diagnostics": [_diagnostic("Execution timed out", "JavaScript execution exceeded the 2-second limit.", kind="timeout")],
            }
        try:
            metadata = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
        except (OSError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not metadata:
            return {
                "status": "error",
                "language": "javascript",
                "output": _split_output(stdout),
                "stdout": stdout,
                "stderr": stderr,
                "variables": {},
                "events": [],
                "call_stack": [],
                "diagnostics": [_diagnostic("Node worker error", stderr.strip() or "Node.js did not return a result.")],
            }
        return {
            "status": metadata.get("status", "error"),
            "supported": True,
            "language": "javascript",
            "output": metadata.get("output", []),
            "stdout": stdout,
            "stderr": metadata.get("stderr", "") or stderr,
            "variables": metadata.get("variables", {}),
            "events": metadata.get("events", []),
            "call_stack": metadata.get("call_stack", []),
            "diagnostics": metadata.get("diagnostics", []),
            "limits": {
                "source_bytes": MAX_SOURCE_BYTES,
                "output_bytes": MAX_OUTPUT_BYTES,
                "events": MAX_EVENTS,
                "stdout_truncated": bool(child.get("stdout_truncated")),
                "stderr_truncated": bool(child.get("stderr_truncated")),
            },
        }


def execute_payload(payload: Any) -> tuple[dict[str, Any], int]:
    validated = _validate_payload(payload)
    if validated[0] is None:
        return validated[1], HTTPStatus.BAD_REQUEST
    language, source = validated
    result = execute_python(source) if language == "python" else execute_javascript(source)
    result.setdefault("request", {"language": language})
    return result, HTTPStatus.OK


def verify_payload(payload: Any) -> tuple[dict[str, Any], int]:
    validated = _validate_payload(payload)
    if validated[0] is None:
        return validated[1], HTTPStatus.BAD_REQUEST
    language, source = validated
    if language == "python":
        try:
            ast.parse(source, filename="codeflow_user.py", mode="exec")
        except SyntaxError as exc:
            diagnostic = _diagnostic(
                "SyntaxError",
                str(exc.msg),
                line=exc.lineno,
                column=exc.offset,
                kind="syntax",
                end_line=getattr(exc, "end_lineno", None),
                end_column=getattr(exc, "end_offset", None),
            )
            return {"valid": False, "language": language, "errors": [diagnostic], "diagnostics": [diagnostic]}, HTTPStatus.OK
        return {"valid": True, "language": language, "errors": [], "diagnostics": []}, HTTPStatus.OK

    node = _node_command()
    if not node:
        diagnostic = _diagnostic("JavaScript unavailable", "Node.js is not installed or is not available on PATH.", kind="unsupported")
        return {"valid": False, "supported": False, "language": language, "errors": [diagnostic], "diagnostics": [diagnostic]}, HTTPStatus.OK
    # Node --check parses without running the program.
    with tempfile.TemporaryDirectory(prefix="codeflow-verify-") as workdir:
        path = Path(workdir) / "source.js"
        path.write_text(source, encoding="utf-8")
        child = _run_process([node, "--check", str(path)], cwd=workdir, env={"PATH": os.environ.get("PATH", "")})
    if child.get("returncode") == 0:
        return {"valid": True, "supported": True, "language": language, "errors": [], "diagnostics": []}, HTTPStatus.OK
    text = _decode(child["stderr"]) or _decode(child["stdout"])
    line = None
    column = None
    import re
    match = re.search(r":(\d+)(?::(\d+))?", text)
    if match:
        line = int(match.group(1))
        column = int(match.group(2)) if match.group(2) else None
    diagnostic = _diagnostic("JavaScript syntax error", text.strip() or "Node.js rejected the source.", line=line, column=column, kind="syntax")
    return {"valid": False, "supported": True, "language": language, "errors": [diagnostic], "diagnostics": [diagnostic]}, HTTPStatus.OK


def _ai_endpoint(base_url: str) -> str | None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _proposed_code(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("code"), str):
        return None
    if len(value["code"].encode("utf-8")) > MAX_SOURCE_BYTES:
        return None
    result = {"code": value["code"]}
    if isinstance(value.get("language"), str) and value["language"].lower() in {"python", "javascript", "js"}:
        result["language"] = "javascript" if value["language"].lower() == "js" else value["language"].lower()
    if isinstance(value.get("explanation"), str):
        result["explanation"] = value["explanation"][:2000]
    return result


def _ai_error(
    title: str,
    detail: str,
    *,
    configured: bool,
    status: str = "error",
    kind: str = "provider",
) -> dict[str, Any]:
    return {
        "status": status,
        "provider_status": "error" if status == "error" else "not-configured",
        "configuration_status": "invalid" if kind == "configuration" else "configured",
        "configured": configured,
        "answer": None,
        "response": None,
        "diagnostics": [_diagnostic(title, detail, kind=kind)],
    }


def _structured_ai_content(content: Any) -> tuple[str, dict[str, Any]]:
    """Decode and validate the JSON object returned in message.content."""
    if isinstance(content, dict):
        decoded = content
    elif isinstance(content, str):
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                body = lines[1:-1]
                if body and body[0].strip().lower() in {"json", "jsonc"}:
                    body = body[1:]
                text = "\n".join(body).strip()
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("AI message content was not valid JSON.") from exc
    else:
        raise ValueError("AI message content was not a JSON object.")
    if not isinstance(decoded, dict):
        raise ValueError("AI message content must be a JSON object.")
    answer = decoded.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("AI response must contain a non-empty answer string.")
    if len(answer.encode("utf-8")) > MAX_AI_ANSWER_BYTES:
        raise ValueError("AI answer exceeded the response limit.")
    proposed_value = decoded.get("proposed_code")
    proposed = None
    if proposed_value is not None:
        proposed = _proposed_code(proposed_value)
        if proposed is None:
            raise ValueError("AI proposed_code must contain valid bounded code.")
    return answer.strip(), {"proposed_code": proposed} if proposed is not None else {}


def ai_payload(payload: Any) -> tuple[dict[str, Any], int]:
    if not isinstance(payload, dict):
        return {"error": "Request body must be a JSON object."}, HTTPStatus.BAD_REQUEST
    prompt = payload.get("prompt", "")
    language = payload.get("language", "python")
    source = payload.get("code", "")
    diagnostics = payload.get("diagnostics", [])
    selected_event = payload.get("selected_event")
    if not isinstance(prompt, str) or not isinstance(language, str) or not isinstance(source, str):
        return {"error": "prompt, language, and code must be strings."}, HTTPStatus.BAD_REQUEST
    if language.lower() not in {"python", "javascript", "js"}:
        return {"error": "language must be 'python' or 'javascript'."}, HTTPStatus.BAD_REQUEST
    if not isinstance(diagnostics, list) or len(diagnostics) > MAX_AI_CONTEXT_ITEMS:
        return {"error": f"diagnostics must be a list of at most {MAX_AI_CONTEXT_ITEMS} items."}, HTTPStatus.BAD_REQUEST
    if any(not isinstance(item, dict) for item in diagnostics):
        return {"error": "diagnostics items must be JSON objects."}, HTTPStatus.BAD_REQUEST
    if selected_event is not None and not isinstance(selected_event, dict):
        return {"error": "selected_event must be a JSON object or null."}, HTTPStatus.BAD_REQUEST
    if len(prompt.encode("utf-8")) > MAX_AI_PROMPT_BYTES:
        return {"error": "prompt exceeds the 8 KiB limit."}, HTTPStatus.BAD_REQUEST
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return {"error": "Source exceeds the 64 KiB limit."}, HTTPStatus.BAD_REQUEST
    api_key = os.environ.get("CODEFLOW_AI_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "not-configured",
            "provider_status": "not-configured",
            "configuration_status": "missing",
            "configured": False,
            "answer": None,
            "response": None,
            "message": "AI is not configured. Set CODEFLOW_AI_API_KEY to enable an OpenAI-compatible provider.",
            "diagnostics": [_diagnostic("AI not configured", "No CODEFLOW_AI_API_KEY is present.", kind="configuration")],
        }, HTTPStatus.OK
    endpoint = _ai_endpoint(os.environ.get("CODEFLOW_AI_BASE_URL", "https://api.openai.com/v1"))
    model = os.environ.get("CODEFLOW_AI_MODEL", "gpt-4o-mini").strip()
    if not endpoint or not model:
        return _ai_error(
            "Invalid AI configuration",
            "Set CODEFLOW_AI_BASE_URL to an HTTP(S) URL and CODEFLOW_AI_MODEL to a model name.",
            configured=False,
            kind="configuration",
        ), HTTPStatus.OK
    normalized_language = "javascript" if language.lower() == "js" else language.lower()
    context = {
        "language": normalized_language,
        "code": source,
        "diagnostics": diagnostics,
        "selected_event": selected_event,
        "prompt": prompt,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are CodeFlow, a concise programming tutor. Use only the supplied execution evidence. "
                "Return only a JSON object with a non-empty string field `answer`. You may include an optional "
                "`proposed_code` object with `code`, optional `language`, and optional `explanation`. "
                "Never claim to have executed code or invent runtime values."
            ),
        },
        {
            "role": "user",
            "content": f"Mode: {payload.get('mode', 'chat')}\nContext:\n{json.dumps(context, ensure_ascii=False)}",
        },
    ]
    request_body = json.dumps({"model": model, "messages": messages, "temperature": 0.2}).encode("utf-8")
    request = URLRequest(endpoint, data=request_body, method="POST", headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    try:
        with urlopen(request, timeout=AI_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_OUTPUT_BYTES * 8 + 1)
        if len(raw) > MAX_OUTPUT_BYTES * 8:
            raise ValueError("AI response exceeded the response limit.")
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("AI response was not a JSON object.")
        choices = decoded.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("AI response did not contain choices.")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        answer, structured = _structured_ai_content(content)
        result: dict[str, Any] = {
            "status": "ok",
            "provider_status": "remote",
            "configuration_status": "configured",
            "configured": True,
            "answer": answer,
            "response": answer,
        }
        result.update(structured)
        return result, HTTPStatus.OK
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "error",
            "provider_status": "error",
            "configuration_status": "configured",
            "configured": True,
            "answer": None,
            "response": None,
            "message": "The configured AI provider could not be reached or returned a valid structured response.",
            "diagnostics": [_diagnostic("AI provider error", str(exc)[:500], kind="provider")],
        }, HTTPStatus.OK


def health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": RESULT_MARKER,
        "python": sys.version.split()[0],
        "fastapi": FASTAPI_AVAILABLE,
        "node_available": bool(_node_command()),
        "ai_configured": bool(os.environ.get("CODEFLOW_AI_API_KEY", "").strip()),
        "limits": {
            "source_bytes": MAX_SOURCE_BYTES,
            "output_bytes": MAX_OUTPUT_BYTES,
            "events": MAX_EVENTS,
            "timeout_seconds": EXECUTION_TIMEOUT_SECONDS,
        },
    }


def _safe_static_path(relative: str) -> Path | None:
    candidate = (BASE_DIR / relative).resolve()
    try:
        candidate.relative_to(BASE_DIR)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


try:
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse, Response

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


if FASTAPI_AVAILABLE:
    app = FastAPI(title="CodeFlow", version="1.0.0")

    async def _fastapi_json(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            return None, JSONResponse({"error": "Request body exceeds the 128 KiB limit."}, status_code=413)
        try:
            value = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        return value, None

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return health_payload()

    @app.post("/api/execute")
    async def execute(request: Request) -> Response:
        payload, error = await _fastapi_json(request)
        if error:
            return error
        result, status = execute_payload(payload)
        return JSONResponse(result, status_code=status)

    @app.post("/api/verify")
    async def verify(request: Request) -> Response:
        payload, error = await _fastapi_json(request)
        if error:
            return error
        result, status = verify_payload(payload)
        return JSONResponse(result, status_code=status)

    @app.post("/api/ai")
    async def ai(request: Request) -> Response:
        payload, error = await _fastapi_json(request)
        if error:
            return error
        result, status = ai_payload(payload)
        return JSONResponse(result, status_code=status)

    @app.get("/")
    async def index() -> Response:
        return FileResponse(BASE_DIR / "index.html")

    @app.get("/{asset_path:path}")
    async def static_asset(asset_path: str) -> Response:
        path = _safe_static_path(asset_path)
        if path is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(path)
else:
    app = None


class CodeFlowHandler(BaseHTTPRequestHandler):
    """Small fallback HTTP server using exactly the same endpoint functions."""

    server_version = "CodeFlow/1.0"

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/health":
            self._send_json(health_payload())
            return
        relative = "index.html" if self.path == "/" else self.path.split("?", 1)[0].lstrip("/")
        path = _safe_static_path(relative)
        if path is None:
            self._send_json({"error": "Not found"}, 404)
            return
        data = path.read_bytes()
        content_type = "text/html; charset=utf-8" if path.suffix == ".html" else "text/plain; charset=utf-8"
        if path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/api/execute", "/api/verify", "/api/ai"}:
            self._send_json({"error": "Not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_REQUEST_BYTES:
            self._send_json({"error": "Request body exceeds the 128 KiB limit."}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "Request body must be valid JSON."}, 400)
            return
        function: Callable[[Any], tuple[dict[str, Any], int]] = {
            "/api/execute": execute_payload,
            "/api/verify": verify_payload,
            "/api/ai": ai_payload,
        }[self.path]
        result, status = function(payload)
        self._send_json(result, int(status))

    def log_message(self, format: str, *args: Any) -> None:
        return None


def serve(host: str = "127.0.0.1", port: int = 8010) -> None:
    server = ThreadingHTTPServer((host, port), CodeFlowHandler)
    print(f"CodeFlow backend listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    # Render and other managed hosts provide PORT and require binding on all
    # interfaces. Local development keeps the safer loopback default and uses
    # 8010 so it does not collide with another app on port 8000.
    host = os.environ.get("CODEFLOW_HOST") or ("0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("CODEFLOW_PORT", "8010"))
    if FASTAPI_AVAILABLE:
        try:
            import uvicorn
        except ImportError:
            serve(host, port)
            return
        uvicorn.run(app, host=host, port=port)
    else:
        serve(host, port)


if __name__ == "__main__":
    main()
