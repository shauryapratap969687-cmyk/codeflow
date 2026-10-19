"""Standalone CodeFlow backend.

The application deliberately keeps all user-program execution in short-lived
child processes.  FastAPI/uvicorn are used when present, while the same
handlers are available through the standard-library fallback server.
"""

from __future__ import annotations

import ast
import collections
import datetime
import json
import math
import os
import shutil
import signal
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
RESULT_TARGET_BYTES = 200 * 1024  # the worker fits its result under this, below RESULT_FILE_LIMIT
EVENT_BUDGET_BYTES = 150 * 1024
RESULT_MARKER = "CodeFlow backend"
# The only files the static handlers will ever serve (never source, config or .git).
STATIC_FILES = frozenset({"index.html", "app.js", "styles.css", "responsive.css"})
PYTHON_MEMORY_LIMIT_BYTES = 768 * 1024 * 1024
RUN_QUEUE_WAIT_SECONDS = 5.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# Public-deployment protection. All values can be tuned with environment
# variables; 0 disables the matching limit.
MAX_CONCURRENT_RUNS = max(1, _env_int("CODEFLOW_MAX_CONCURRENT", 4))
RATE_LIMIT_EXECUTE_PER_MINUTE = _env_int("CODEFLOW_RATE_EXECUTE", 60)
RATE_LIMIT_AI_PER_MINUTE = _env_int("CODEFLOW_RATE_AI", 15)
AI_DAILY_LIMIT = _env_int("CODEFLOW_AI_DAILY_LIMIT", 500)


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


_PRIVILEGE_DROP_CACHE: dict[str, tuple[int, int] | None] = {}
_PRIVILEGE_DROP_LOCK = threading.Lock()


def _unprivileged_ids(executable: str, probe_args: list[str]) -> tuple[int, int] | None:
    """uid/gid of "nobody" when the server runs as root and the interpreter works as that user.

    A child that runs as a different user cannot read the server's /proc entry
    (environment, memory) or its files. The check is done once per executable so
    a host where "nobody" cannot run the interpreter simply keeps the old behaviour.
    """
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    with _PRIVILEGE_DROP_LOCK:
        if executable in _PRIVILEGE_DROP_CACHE:
            return _PRIVILEGE_DROP_CACHE[executable]
        ids: tuple[int, int] | None = None
        try:
            import pwd

            entry = pwd.getpwnam("nobody")
            probe = subprocess.run(
                [executable, *probe_args],
                cwd="/",
                env={"PATH": os.environ.get("PATH", "")},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                user=entry.pw_uid,
                group=entry.pw_gid,
                extra_groups=[],
            )
            if probe.returncode == 0:
                ids = (entry.pw_uid, entry.pw_gid)
        except Exception:  # noqa: BLE001 - any failure just means "do not drop privileges"
            ids = None
        _PRIVILEGE_DROP_CACHE[executable] = ids
        return ids


def _child_options(executable: str, probe_args: list[str], paths: list[Path]) -> dict[str, Any]:
    """Extra Popen arguments that make the child less powerful than the server."""
    if os.name != "posix":
        return {}
    options: dict[str, Any] = {"start_new_session": True}
    ids = _unprivileged_ids(executable, probe_args)
    if ids is not None:
        try:
            for path in paths:
                os.chown(path, ids[0], ids[1])
            options.update(user=ids[0], group=ids[1], extra_groups=[])
        except OSError:
            pass
    return options


def _kill_process_group(process: "subprocess.Popen[bytes]") -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _run_process(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float = EXECUTION_TIMEOUT_SECONDS,
    popen_options: dict[str, Any] | None = None,
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
            **(popen_options or {}),
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
        _kill_process_group(process)
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


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Replace NaN/Infinity (not valid JSON) so every response can be serialised."""
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if depth > 40:
        return value
    if isinstance(value, dict):
        return {key: _json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in value]
    return value


class _RateLimiter:
    """Small in-memory sliding-window limiter (per key, per process)."""

    def __init__(self) -> None:
        self._hits: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float = 60.0) -> bool:
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, collections.deque())
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > 5000:
                for stale in [name for name, queue in self._hits.items() if not queue or now - queue[-1] > window_seconds]:
                    del self._hits[stale]
            return True


_RATE_LIMITER = _RateLimiter()
_RUN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)
_AI_DAILY = {"day": "", "count": 0}
_AI_DAILY_LOCK = threading.Lock()


def _client_ip(forwarded_for: str | None, peer: str | None) -> str:
    """Best-effort client address. Proxies append the real peer last, so use the last entry."""
    if forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return last
    return peer or "unknown"


def _ai_daily_budget_available() -> bool:
    if AI_DAILY_LIMIT <= 0:
        return True
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    with _AI_DAILY_LOCK:
        if _AI_DAILY["day"] != today:
            _AI_DAILY["day"] = today
            _AI_DAILY["count"] = 0
        if _AI_DAILY["count"] >= AI_DAILY_LIMIT:
            return False
        _AI_DAILY["count"] += 1
        return True


def _limited_run_result(language: str, status: str, title: str, detail: str, kind: str) -> dict[str, Any]:
    """A normal-shaped (HTTP 200) result so the UI shows the reason instead of 'offline mode'."""
    return {
        "status": status,
        "language": language,
        "output": [],
        "stdout": "",
        "stderr": "",
        "variables": {},
        "events": [],
        "call_stack": [],
        "diagnostics": [_diagnostic(title, detail, kind=kind)],
    }


def _harden_server_process() -> None:
    """Mark this process non-dumpable (Linux) so same-user child programs cannot read
    /proc/<server pid>/environ or /proc/<server pid>/mem, which hold the AI key."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
    except Exception:  # noqa: BLE001 - best effort only
        pass


_harden_server_process()


PYTHON_RUNNER = r'''
import builtins
import itertools
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
RESULT_TARGET = int(os.environ.get("CODEFLOW_RESULT_TARGET", "204800"))
EVENT_BUDGET = int(os.environ.get("CODEFLOW_EVENT_BUDGET", "153600"))
MEMORY_LIMIT = int(os.environ.get("CODEFLOW_MEMORY_BYTES", "0"))
MAX_VALUE = 800

# Resource limits for the user program (POSIX only; skipped where unsupported).
try:
    import resource
    for _name, _limit in (("RLIMIT_AS", MEMORY_LIMIT), ("RLIMIT_FSIZE", 1024 * 1024), ("RLIMIT_CORE", 0)):
        if _limit or _name == "RLIMIT_CORE":
            try:
                resource.setrlimit(getattr(resource, _name), (_limit, _limit))
            except (ValueError, OSError, AttributeError):
                pass
except ImportError:
    pass

WORKDIR = os.path.realpath(os.path.dirname(SOURCE_PATH))
BLOCKED_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
    "os.forkpty", "os.startfile", "socket.connect", "socket.bind", "socket.getaddrinfo",
    "ctypes.dlopen", "ctypes.dlsym",
}
WRITE_FLAGS = getattr(os, "O_WRONLY", 1) | getattr(os, "O_RDWR", 2) | getattr(os, "O_CREAT", 64) | getattr(os, "O_TRUNC", 512) | getattr(os, "O_APPEND", 1024)


def _guard(event, args):
    """Audit hook: keep learner programs away from processes, sockets, /proc and files outside the work folder."""
    if event in BLOCKED_EVENTS:
        raise PermissionError("CodeFlow does not allow " + event + " in visualized programs.")
    if event == "open" and args:
        target = args[0]
        if isinstance(target, int):
            return
        try:
            real = os.path.realpath(os.fsdecode(target))
        except (TypeError, ValueError):
            return
        if real.startswith(("/proc", "/sys")):
            raise PermissionError("CodeFlow does not allow reading " + real + ".")
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        writing = bool(flags & WRITE_FLAGS) or any(ch in mode for ch in "wax+")
        if writing and real != os.devnull and not (real == WORKDIR or real.startswith(WORKDIR + os.sep)):
            raise PermissionError("CodeFlow only allows writing inside its temporary work folder.")


def _no_input(prompt=""):
    raise EOFError("input() is not available in CodeFlow (there is no keyboard input). Assign a sample value instead, for example: name = 'Ada'")


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
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return repr(value)
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 4000:
        return "<very large int, %d bits>" % value.bit_length()
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > MAX_VALUE:
            return value[:MAX_VALUE] + "…"
        return value
    if isinstance(value, (list, tuple, set)):
        return [safe_value(item, depth + 1) for item in itertools.islice(value, 30)]
    if isinstance(value, dict):
        return {str(key)[:80]: safe_value(item, depth + 1) for key, item in itertools.islice(value.items(), 30)}
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
event_bytes = 0
tracing_stopped = False


def trace(frame, event, arg):
    global events_truncated, event_bytes, tracing_stopped
    if tracing_stopped or frame.f_code.co_filename != SOURCE_NAME:
        return None
    function = frame.f_code.co_name
    if event == "call":
        if len(stack) < MAX_STACK:
            stack.append(function)
    elif event == "return":
        event_name = "return"
    else:
        event_name = event
    if event in ("call", "line", "return", "exception"):
        recorded = False
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
            size = len(json.dumps(item, ensure_ascii=False, default=str))
            if event_bytes + size <= EVENT_BUDGET:
                event_bytes += size
                events.append(item)
                recorded = True
        if not recorded:
            # Event count or size budget reached: stop tracing so the program still
            # finishes quickly and the result always fits in the response.
            events_truncated = True
            tracing_stopped = True
            sys.settrace(None)
            return None
    if event == "return" and stack:
        stack.pop()
    return trace


def fit_result(meta):
    """Serialise the result, shrinking it (events first, then variables) until it fits."""
    def encode():
        text = json.dumps(meta, ensure_ascii=False)
        return text, len(text.encode("utf-8", "replace"))
    text, size = encode()
    while size > RESULT_TARGET and meta["events"]:
        meta["events"] = meta["events"][: len(meta["events"]) // 2]
        meta["events_truncated"] = True
        text, size = encode()
    if size > RESULT_TARGET:
        meta["variables"] = {key: (value if isinstance(value, (bool, int, float, type(None))) else str(value)[:120]) for key, value in meta["variables"].items()}
        text, size = encode()
    if size > RESULT_TARGET:
        meta["variables"] = {}
        text, size = encode()
    return text


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
        builtins.input = _no_input
        sys.addaudithook(_guard)
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
metadata["call_stack"] = [] if tracing_stopped else list(stack[-MAX_STACK:])
metadata["stdout"] = stdout_capture.getvalue()
metadata["stderr"] = stderr_capture.getvalue()
metadata["stdout_truncated"] = stdout_capture.truncated
metadata["stderr_truncated"] = stderr_capture.truncated
result_text = fit_result(metadata)
with open(RESULT_PATH, "w", encoding="utf-8", errors="replace") as result_file:
    result_file.write(result_text)
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
        # Deliberately minimal environment: the AI key and other server secrets never reach user code.
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CODEFLOW_MAX_EVENTS": str(MAX_EVENTS),
            "CODEFLOW_MAX_VARIABLES": str(MAX_VARIABLES),
            "CODEFLOW_MAX_STACK": str(MAX_STACK),
            "CODEFLOW_MEMORY_BYTES": str(PYTHON_MEMORY_LIMIT_BYTES),
        }
        if os.name == "nt" and os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        child = _run_process(
            [sys.executable, "-I", "-S", "-c", PYTHON_RUNNER, str(source_path), str(result_path)],
            cwd=workdir,
            env=env,
            popen_options=_child_options(
                sys.executable,
                ["-I", "-S", "-c", "import json, os, sys, traceback, itertools, builtins, resource"],
                [work, source_path],
            ),
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
const LIMIT = 16384;
const MAX_TIMER_RUNS = 1000;
const MAX_VARIABLES = 100;
const output = [];
const errors = [];
let outputSize = 0;

function bounded(target, text) {
  if (outputSize < LIMIT) {
    target.push(text.slice(0, LIMIT - outputSize));
    outputSize += Math.min(text.length, LIMIT - outputSize);
  }
}

// Host-side helpers. They are handed to the sandbox only through the bootstrap
// closure below and only ever receive/return primitives or sandbox objects.
function hostPrint(kind, text) { bounded(kind === 1 ? errors : output, String(text)); }

let virtualNow = 0;
let order = 0;
let nextId = 0;
let timersRun = 0;
const timers = new Map();
function hostSchedule(fn, delay, args, interval, immediate) {
  let wait = immediate ? 0 : Number(delay);
  if (!(wait >= 1)) wait = immediate ? 0 : 1;
  if (wait > 2147483647) wait = 1;
  const id = ++nextId;
  timers.set(id, { id, fn, args, time: virtualNow + wait, order: ++order, interval: interval ? wait : 0 });
  return id;
}
function hostCancel(id) { timers.delete(Number(id)); }

const bootstrapSource = `(function (hostPrint, hostSchedule, hostCancel) {
  "use strict";
  const fmt = (v) => {
    if (typeof v === "string") return v;
    try { const s = JSON.stringify(v); return s === undefined ? String(v) : s; } catch (_) { return String(v); }
  };
  const write = (kind) => (...args) => { hostPrint(kind, args.map(fmt).join(" ")); };
  const define = (name, value) => Object.defineProperty(globalThis, name, { value, writable: true, configurable: true, enumerable: false });
  define("console", { log: write(0), info: write(0), debug: write(0), warn: write(1), error: write(1) });
  const timer = (interval, immediate) => function (fn, delay, ...rest) {
    if (typeof fn !== "function") throw new TypeError("The callback argument must be of type function.");
    return hostSchedule(fn, Number(delay), rest, interval, immediate);
  };
  define("setTimeout", timer(false, false));
  define("setInterval", timer(true, false));
  define("setImmediate", timer(false, true));
  const cancel = (id) => { hostCancel(Number(id)); };
  define("clearTimeout", cancel);
  define("clearInterval", cancel);
  define("clearImmediate", cancel);
  define("queueMicrotask", (fn) => { Promise.resolve().then(fn); });
})`;

const result = { status: "completed", diagnostics: [], variables: {}, events: [], call_stack: [] };
let failed = false;

function record(error) {
  if (failed) return;
  failed = true;
  let name = "JavaScriptError";
  let message = "";
  let stack = "";
  try {
    name = String(error && error.name ? error.name : "JavaScriptError");
    message = String(error && error.message ? error.message : error);
    stack = String(error && error.stack ? error.stack : "");
  } catch (_) {
    message = "The program threw a value that could not be printed.";
  }
  const match = stack.match(/codeflow_user\.js:(\d+)(?::(\d+))?/);
  result.status = "error";
  result.diagnostics.push({
    kind: name === "SyntaxError" ? "syntax" : "runtime",
    title: name,
    message,
    detail: message,
    line: match ? Number(match[1]) : null,
    column: match && match[2] ? Number(match[2]) : null,
  });
}
process.on("unhandledRejection", (reason) => record(reason));
process.on("uncaughtException", (error) => record(error));

const sandbox = Object.create(null);
const context = vm.createContext(sandbox, { codeGeneration: { strings: false, wasm: false } });
vm.runInContext(bootstrapSource, context, { timeout: 1000 })(hostPrint, hostSchedule, hostCancel);

function snapshotValue(value) {
  try {
    if (typeof value === "number" && !Number.isFinite(value)) return String(value);
    if (typeof value === "function") return String(value).slice(0, 200);
    const text = JSON.stringify(value);
    if (text === undefined) return String(value).slice(0, 200);
    if (text.length > 2000) return text.slice(0, 2000) + "…";
    return JSON.parse(text);
  } catch (_) {
    try { return String(value).slice(0, 200); } catch (__) { return "<unrepresentable>"; }
  }
}

function collectVariables() {
  const variables = {};
  let count = 0;
  try {
    for (const key of Object.keys(sandbox)) {
      if (key.startsWith("_") || count >= MAX_VARIABLES) continue;
      variables[key] = snapshotValue(sandbox[key]);
      count += 1;
    }
  } catch (_) { /* keep what was collected */ }
  // let / const / class declarations are not properties of the global object, so read them by name.
  const declared = new Set();
  const pattern = /(?:^|[\n;{}])[ \t]*(?:let|const|class)\s+([A-Za-z_$][\w$]*)/g;
  let match;
  while ((match = pattern.exec(source)) !== null) declared.add(match[1]);
  for (const name of declared) {
    if (count >= MAX_VARIABLES) break;
    if (Object.prototype.hasOwnProperty.call(variables, name) || name.startsWith("_")) continue;
    try {
      variables[name] = snapshotValue(vm.runInContext(name, context, { timeout: 200 }));
      count += 1;
    } catch (_) { /* block scoped or not initialised yet */ }
  }
  return variables;
}

async function settle() { await new Promise((resolve) => setImmediate(resolve)); }

async function main() {
  try {
    const script = new vm.Script(source, { filename: "codeflow_user.js", displayErrors: true });
    script.runInContext(context, { timeout: 1500, displayErrors: true });
  } catch (error) {
    record(error);
  }
  if (!failed) await settle();
  // Virtual-time event loop: timers fire in (time, creation) order without real waiting.
  while (!failed && timers.size) {
    if (timersRun >= MAX_TIMER_RUNS) {
      const limit = new Error("Stopped after " + MAX_TIMER_RUNS + " timer callbacks (possible endless setInterval).");
      limit.name = "TimerLimit";
      record(limit);
      break;
    }
    let next = null;
    for (const item of timers.values()) {
      if (!next || item.time < next.time || (item.time === next.time && item.order < next.order)) next = item;
    }
    virtualNow = next.time;
    if (next.interval) { next.time = virtualNow + next.interval; next.order = ++order; } else { timers.delete(next.id); }
    timersRun += 1;
    try { Reflect.apply(next.fn, undefined, next.args); } catch (error) { record(error); break; }
    await settle();
  }
}

function finish() {
  result.variables = collectVariables();
  result.output = output;
  result.stderr = errors.join("\n");
  fs.writeFileSync(resultPath, JSON.stringify(result), "utf8");
}
main().then(finish, (error) => { record(error); finish(); });
'''


def _node_command() -> str | None:
    return shutil.which("node") or shutil.which("nodejs")


_NODE_PERMISSION_FLAG: dict[str, str | None] = {}


def _node_permission_flag(node: str) -> str | None:
    """Node's permission model (blocks child processes and files outside the work folder), if this Node has it."""
    if node in _NODE_PERMISSION_FLAG:
        return _NODE_PERMISSION_FLAG[node]
    flag: str | None = None
    for candidate in ("--permission", "--experimental-permission"):
        try:
            probe = subprocess.run(
                [node, candidate, "-e", "0"],
                cwd=tempfile.gettempdir(),
                env={"PATH": os.environ.get("PATH", ""), "NODE_NO_WARNINGS": "1"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            flag = candidate
            break
    _NODE_PERMISSION_FLAG[node] = flag
    return flag


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
        # Minimal environment: the server's secrets (AI key, etc.) are never passed to user code.
        env = {"PATH": os.environ.get("PATH", ""), "NODE_NO_WARNINGS": "1"}
        if os.name == "nt" and os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        options = _child_options(node, ["-e", "require('fs'); require('vm')"], [work, source_path])
        permission_flag = _node_permission_flag(node)
        real_workdir = os.path.realpath(workdir)
        attempts = [True, False] if permission_flag else [False]
        child: dict[str, Any] = {}
        for use_permission in attempts:
            command = [node, "--max-old-space-size=128"]
            if use_permission and permission_flag:
                command += [permission_flag, f"--allow-fs-read={real_workdir}", f"--allow-fs-write={real_workdir}"]
            command += ["-e", JS_RUNNER, "--", str(source_path), str(result_path)]
            child = _run_process(command, cwd=workdir, env=env, popen_options=options)
            if child.get("timed_out") or result_path.exists():
                break
            if use_permission:
                # The permission model did not work on this host; remember that and run without it.
                _NODE_PERMISSION_FLAG[node] = None
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
            metadata = {}
            if result_path.exists() and result_path.stat().st_size <= RESULT_FILE_LIMIT:
                metadata = json.loads(result_path.read_text(encoding="utf-8"))
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


def execute_payload(payload: Any, client: str | None = None) -> tuple[dict[str, Any], int]:
    """Run a program. `client` (an address) enables per-client rate limiting; omit it to skip that."""
    validated = _validate_payload(payload)
    if validated[0] is None:
        return validated[1], HTTPStatus.BAD_REQUEST
    language, source = validated
    if client is not None and not _RATE_LIMITER.allow(f"run:{client}", RATE_LIMIT_EXECUTE_PER_MINUTE):
        return _limited_run_result(
            language, "rate-limited", "Too many runs",
            "You are running code too quickly. Wait a few seconds and try again.", "rate-limit",
        ), HTTPStatus.OK
    # Bound how many programs run at once so a burst cannot exhaust the host.
    if not _RUN_SLOTS.acquire(timeout=RUN_QUEUE_WAIT_SECONDS):
        return _limited_run_result(
            language, "busy", "Server busy",
            "Too many programs are running right now. Try again in a few seconds.", "busy",
        ), HTTPStatus.OK
    try:
        result = execute_python(source) if language == "python" else execute_javascript(source)
    finally:
        _RUN_SLOTS.release()
    result.setdefault("request", {"language": language})
    return _json_safe(result), HTTPStatus.OK


def verify_payload(payload: Any, client: str | None = None) -> tuple[dict[str, Any], int]:
    validated = _validate_payload(payload)
    if validated[0] is None:
        return validated[1], HTTPStatus.BAD_REQUEST
    language, source = validated
    if client is not None and not _RATE_LIMITER.allow(f"run:{client}", RATE_LIMIT_EXECUTE_PER_MINUTE):
        diagnostic = _diagnostic("Too many requests", "You are verifying code too quickly. Wait a few seconds and try again.", kind="rate-limit")
        return {"valid": False, "language": language, "errors": [diagnostic], "diagnostics": [diagnostic]}, HTTPStatus.OK
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


def ai_payload(payload: Any, client: str | None = None) -> tuple[dict[str, Any], int]:
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
    # Protect the owner's API key/credit on a public deployment: per-client rate limit plus a daily cap.
    if client is not None and not _RATE_LIMITER.allow(f"ai:{client}", RATE_LIMIT_AI_PER_MINUTE):
        return dict(_ai_error(
            "Too many AI requests",
            "You are asking the AI too quickly. Wait a moment and try again.",
            configured=True,
            kind="rate-limit",
        ), message="Too many AI requests right now. Please wait a moment and try again."), HTTPStatus.OK
    if not _ai_daily_budget_available():
        return dict(_ai_error(
            "AI daily limit reached",
            "The daily AI request limit for this deployment has been reached.",
            configured=True,
            kind="rate-limit",
        ), message="The daily AI request limit for this deployment has been reached. Try again tomorrow."), HTTPStatus.OK
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
    # Serve only the frontend files. Source code, config, tests and .git are never exposed.
    if relative not in STATIC_FILES:
        return None
    candidate = (BASE_DIR / relative).resolve()
    try:
        candidate.relative_to(BASE_DIR)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


try:
    from fastapi import FastAPI, Request
    from fastapi.concurrency import run_in_threadpool
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

    def _fastapi_client(request: Request) -> str:
        peer = request.client.host if request.client else None
        return _client_ip(request.headers.get("x-forwarded-for"), peer)

    # The handlers below run the blocking work (child processes, the AI HTTP call)
    # in a worker thread so one slow program never freezes the whole server.
    @app.post("/api/execute")
    async def execute(request: Request) -> Response:
        payload, error = await _fastapi_json(request)
        if error:
            return error
        result, status = await run_in_threadpool(execute_payload, payload, _fastapi_client(request))
        return JSONResponse(result, status_code=status)

    @app.post("/api/verify")
    async def verify(request: Request) -> Response:
        payload, error = await _fastapi_json(request)
        if error:
            return error
        result, status = await run_in_threadpool(verify_payload, payload, _fastapi_client(request))
        return JSONResponse(result, status_code=status)

    @app.post("/api/ai")
    async def ai(request: Request) -> Response:
        payload, error = await _fastapi_json(request)
        if error:
            return error
        result, status = await run_in_threadpool(ai_payload, payload, _fastapi_client(request))
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
    timeout = 20  # seconds; a client that stalls mid-request cannot hold a thread forever

    def _client(self) -> str:
        return _client_ip(self.headers.get("X-Forwarded-For"), self.client_address[0] if self.client_address else None)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route == "/api/health":
            self._send_json(health_payload())
            return
        relative = "index.html" if route == "/" else route.lstrip("/")
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
        route = self.path.split("?", 1)[0]
        if route not in {"/api/execute", "/api/verify", "/api/ai"}:
            self._send_json({"error": "Not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0:
            self._send_json({"error": "Invalid Content-Length."}, 400)
            return
        if length > MAX_REQUEST_BYTES:
            self._send_json({"error": "Request body exceeds the 128 KiB limit."}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "Request body must be valid JSON."}, 400)
            return
        function: Callable[..., tuple[dict[str, Any], int]] = {
            "/api/execute": execute_payload,
            "/api/verify": verify_payload,
            "/api/ai": ai_payload,
        }[route]
        result, status = function(payload, self._client())
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
