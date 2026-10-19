"""Regression tests for the stability and security fixes in backend.py."""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import backend

try:
    import resource  # noqa: F401
    HAS_RESOURCE = True
except ImportError:  # Windows
    HAS_RESOURCE = False

HAS_NODE = bool(backend._node_command())
SECRET = "sk-test-secret-key-do-not-leak"


def run(language, code):
    result, status = backend.execute_payload({"language": language, "code": code})
    assert status == 200, (status, result)
    return result


class SecretsAndSandboxTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"CODEFLOW_AI_API_KEY": SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_python_program_does_not_receive_the_api_key(self):
        result = run("python", "import os\nprint(sorted(k for k in os.environ if 'CODEFLOW_AI' in k))")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], ["[]"])
        self.assertNotIn(SECRET, json.dumps(result))

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
    def test_python_program_cannot_read_server_environment_through_proc(self):
        code = "import os\nprint(open('/proc/%d/environ' % os.getppid(), 'rb').read())"
        result = run("python", code)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["diagnostics"][0]["title"], "PermissionError")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_python_program_cannot_start_processes_or_sockets_or_write_elsewhere(self):
        for code in (
            "import subprocess\nsubprocess.run(['echo', 'hi'])",
            "import os\nos.system('echo hi')",
            "import socket\nsocket.create_connection(('127.0.0.1', 9), timeout=1)",
            "open(%r, 'w').write('x')" % os.path.join(tempfile.gettempdir(), 'codeflow_should_not_exist.txt'),
        ):
            with self.subTest(code=code):
                result = run("python", code)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["diagnostics"][0]["title"], "PermissionError")

    def test_python_program_can_still_use_the_standard_library_and_work_folder(self):
        code = (
            "import math, random, json, re, collections, itertools, datetime, functools, heapq, string, textwrap\n"
            "open('scratch.txt', 'w').write('ok')\n"
            "print(open('scratch.txt').read(), math.floor(2.5), json.dumps({'a': 1}))"
        )
        result = run("python", code)
        self.assertEqual(result["status"], "completed", result["diagnostics"])
        self.assertEqual(result["output"], ['ok 2 {"a": 1}'])

    def test_input_gives_a_clear_message(self):
        result = run("python", "name = input('Name? ')")
        self.assertEqual(result["status"], "error")
        self.assertIn("input() is not available", result["diagnostics"][0]["message"])

    @unittest.skipUnless(HAS_RESOURCE, "needs POSIX resource limits")
    def test_python_memory_is_capped(self):
        result = run("python", "x = bytearray(2 * 1024 * 1024 * 1024)")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["diagnostics"][0]["title"], "MemoryError")

    @unittest.skipUnless(HAS_NODE, "Node.js not installed")
    def test_javascript_program_cannot_reach_host_process_or_key(self):
        escape = "const p = this.constructor.constructor('return process')();\nconsole.log(p.env.CODEFLOW_AI_API_KEY);"
        result = run("javascript", escape)
        self.assertEqual(result["status"], "error")
        self.assertNotIn(SECRET, json.dumps(result))
        result = run("javascript", "console.log(typeof process, typeof require)")
        self.assertEqual(result["output"], ["undefined undefined"])


class ResultRobustnessTests(unittest.TestCase):
    def test_non_finite_numbers_are_valid_json(self):
        result = run("python", "x = float('inf')\ny = float('nan')\nz = -float('inf')")
        self.assertEqual(result["status"], "completed")
        json.dumps(result, allow_nan=False)  # must not raise
        self.assertEqual(result["variables"]["x"], "inf")
        self.assertEqual(result["variables"]["y"], "nan")

    def test_huge_integer_does_not_break_the_run(self):
        result = run("python", "n = 10 ** 6000\nprint('done')")
        self.assertEqual(result["status"], "completed", result["diagnostics"])
        self.assertEqual(result["output"], ["done"])

    def test_large_variables_no_longer_discard_the_whole_run(self):
        code = (
            "words = ['the quick brown fox jumps over the lazy dog ' * 15 for _ in range(30)]\n"
            "total = 0\n"
            "for w in words:\n"
            "    total += len(w)\n"
            "print(total)\n"
        )
        result = run("python", code)
        self.assertEqual(result["status"], "completed", result["diagnostics"])
        self.assertEqual(result["output"], ["19800"])
        self.assertTrue(result["events"])
        self.assertLess(len(json.dumps(result)), backend.RESULT_FILE_LIMIT * 2)

    def test_long_loop_finishes_and_reports_truncated_trace(self):
        result = run("python", "total = 0\nfor i in range(300000):\n    total += i\nprint(total)")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], ["44999850000"])
        self.assertTrue(result["limits"]["events_truncated"])
        self.assertLessEqual(len(result["events"]), backend.MAX_EVENTS)

    def test_deep_recursion_still_traces(self):
        result = run("python", "def f(n):\n    return 0 if n == 0 else 1 + f(n - 1)\nprint(f(70))")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], ["70"])

    def test_unicode_edge_cases_do_not_lose_the_result(self):
        result = run("python", "s = '\\ud800'\nprint('ok')")
        self.assertEqual(result["status"], "completed", result["diagnostics"])
        self.assertEqual(result["output"], ["ok"])


@unittest.skipUnless(HAS_NODE, "Node.js not installed")
class JavaScriptTests(unittest.TestCase):
    def test_timers_promises_and_lexical_variables(self):
        code = (
            "let a = 1;\nconst b = 2;\nvar c = 3;\n"
            "setTimeout(() => console.log('later'), 10);\n"
            "setTimeout(() => console.log('sooner'), 5);\n"
            "Promise.resolve().then(() => console.log('promise'));\n"
            "console.log('now');"
        )
        result = run("javascript", code)
        self.assertEqual(result["status"], "completed", result["diagnostics"])
        self.assertEqual(result["output"], ["now", "promise", "sooner", "later"])
        self.assertEqual((result["variables"]["a"], result["variables"]["b"], result["variables"]["c"]), (1, 2, 3))

    def test_errors_keep_their_line_numbers(self):
        result = run("javascript", "let a = 1;\nnull.x;")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["diagnostics"][0]["title"], "TypeError")
        self.assertEqual(result["diagnostics"][0]["line"], 2)

    def test_rejected_promise_is_reported(self):
        result = run("javascript", "Promise.reject(new Error('boom'));")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["diagnostics"][0]["message"], "boom")

    def test_endless_interval_is_stopped(self):
        result = run("javascript", "setInterval(() => {}, 1);")
        self.assertIn(result["status"], ("error", "timeout"))

    def test_non_finite_numbers_are_valid_json(self):
        result = run("javascript", "var x = 1 / 0;\nvar y = 0 / 0;")
        json.dumps(result, allow_nan=False)


class LimitTests(unittest.TestCase):
    def test_rate_limiter_window(self):
        limiter = backend._RateLimiter()
        self.assertTrue(limiter.allow("a", 2))
        self.assertTrue(limiter.allow("a", 2))
        self.assertFalse(limiter.allow("a", 2))
        self.assertTrue(limiter.allow("b", 2))
        self.assertTrue(limiter.allow("a", 0))  # 0 disables the limit

    def test_execute_rate_limit_returns_a_normal_shaped_result(self):
        with patch.object(backend, "_RATE_LIMITER", backend._RateLimiter()), patch.object(backend, "RATE_LIMIT_EXECUTE_PER_MINUTE", 1):
            first, _ = backend.execute_payload({"language": "python", "code": "print(1)"}, "10.0.0.1")
            second, status = backend.execute_payload({"language": "python", "code": "print(2)"}, "10.0.0.1")
            other, _ = backend.execute_payload({"language": "python", "code": "print(3)"}, "10.0.0.2")
        self.assertEqual(first["status"], "completed")
        self.assertEqual(status, 200)
        self.assertEqual(second["status"], "rate-limited")
        self.assertEqual(second["diagnostics"][0]["kind"], "rate-limit")
        self.assertEqual(other["status"], "completed")

    def test_busy_server_answers_instead_of_piling_up(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(backend, "_RUN_SLOTS", slots), patch.object(backend, "RUN_QUEUE_WAIT_SECONDS", 0.05):
            result, status = backend.execute_payload({"language": "python", "code": "print(1)"})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "busy")

    def test_ai_rate_limit_and_daily_cap_protect_the_key(self):
        good = {"choices": [{"message": {"content": json.dumps({"answer": "fine"})}}]}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, *_):
                return json.dumps(good).encode()

        payload = {"prompt": "hi", "language": "python", "code": "x = 1"}
        env = {"CODEFLOW_AI_API_KEY": "sk-test"}
        with patch.dict(os.environ, env), patch.object(backend, "urlopen", return_value=Response()) as opened:
            with patch.object(backend, "_RATE_LIMITER", backend._RateLimiter()), patch.object(backend, "RATE_LIMIT_AI_PER_MINUTE", 1):
                first, _ = backend.ai_payload(payload, "10.0.0.9")
                second, status = backend.ai_payload(payload, "10.0.0.9")
            self.assertEqual(first["status"], "ok")
            self.assertEqual(status, 200)
            self.assertEqual(second["status"], "error")
            self.assertIn("Too many AI requests", second["message"])
            self.assertEqual(opened.call_count, 1)  # the limited call never reached the provider
            with patch.object(backend, "AI_DAILY_LIMIT", 1), patch.dict(backend._AI_DAILY, {"day": "", "count": 0}):
                self.assertEqual(backend.ai_payload(payload)[0]["status"], "ok")
                capped, _ = backend.ai_payload(payload)
            self.assertEqual(capped["status"], "error")
            self.assertIn("daily", capped["message"])

    def test_client_ip_prefers_the_proxy_appended_address(self):
        self.assertEqual(backend._client_ip("1.1.1.1, 2.2.2.2", "9.9.9.9"), "2.2.2.2")
        self.assertEqual(backend._client_ip(None, "9.9.9.9"), "9.9.9.9")
        self.assertEqual(backend._client_ip("", None), "unknown")


class StaticAndHttpTests(unittest.TestCase):
    def test_only_frontend_files_are_served(self):
        for name in ("index.html", "app.js", "styles.css", "responsive.css"):
            self.assertIsNotNone(backend._safe_static_path(name), name)
        for name in ("backend.py", ".git/config", "render.yaml", "requirements.txt", "tests/test_api.py", "../backend.py", ".env", ""):
            self.assertIsNone(backend._safe_static_path(name), name)

    def test_fallback_server_end_to_end(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), backend.CodeFlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]

        with urlopen(base + "/") as response:
            self.assertEqual(response.status, 200)
        with urlopen(base + "/api/health?probe=1") as response:
            self.assertEqual(json.load(response)["status"], "ok")
        for path in ("/backend.py", "/.git/config", "/render.yaml"):
            with self.assertRaises(HTTPError) as caught:
                urlopen(base + path)
            self.assertEqual(caught.exception.code, 404, path)

        body = json.dumps({"language": "python", "code": "x = float('nan')\nprint('hi')"}).encode()
        request = Request(base + "/api/execute?cache=1", data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            payload = json.loads(response.read())  # NaN would make this fail to parse in a browser
        self.assertEqual(payload["status"], "completed")

        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as raw:
            raw.sendall(b"POST /api/execute HTTP/1.1\r\nHost: x\r\nContent-Length: -5\r\nContent-Type: application/json\r\n\r\n")
            self.assertIn(b" 400 ", raw.recv(1024).split(b"\r\n", 1)[0])


@unittest.skipUnless(backend.FASTAPI_AVAILABLE, "FastAPI not installed")
class FastApiTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:  # httpx missing
            self.skipTest("TestClient unavailable: %s" % exc)
        self.client = TestClient(backend.app)

    def test_nan_program_returns_valid_json_not_a_500(self):
        response = self.client.post("/api/execute", json={"language": "python", "code": "x = float('inf')"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["variables"]["x"], "inf")

    def test_source_files_are_not_served(self):
        for path in ("/backend.py", "/.git/config", "/render.yaml"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
