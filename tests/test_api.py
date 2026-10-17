import os
import json
import unittest
from unittest.mock import patch

import backend


class CodeFlowApiTests(unittest.TestCase):
    def test_health_reports_backend_and_limits(self):
        health = backend.health_payload()
        self.assertEqual(health["status"], "ok")
        self.assertIn("source_bytes", health["limits"])
        self.assertIn("node_available", health)

    def test_python_execution_captures_output_variables_and_trace(self):
        result, status = backend.execute_payload({
            "language": "python",
            "code": "value = 2 + 3\nprint(value)",
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed")
        self.assertIn("5", result["output"])
        self.assertEqual(result["variables"]["value"], 5)
        self.assertTrue(result["events"])
        self.assertTrue(all("type" in event and "line" in event for event in result["events"]))

    def test_python_syntax_diagnostic_has_location(self):
        result, status = backend.execute_payload({"language": "python", "code": "if True print('x')"})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["diagnostics"][0]["kind"], "syntax")
        self.assertIsNotNone(result["diagnostics"][0].get("line"))
        self.assertIsNotNone(result["diagnostics"][0].get("column"))

    def test_python_timeout_is_bounded(self):
        result, status = backend.execute_payload({"language": "python", "code": "while True:\n    pass"})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["diagnostics"][0]["kind"], "timeout")

    def test_source_output_and_event_limits_are_reported(self):
        oversized, status = backend.execute_payload({"language": "python", "code": "x" * (backend.MAX_SOURCE_BYTES + 1)})
        self.assertEqual(status, 400)
        self.assertIn("limit", oversized["error"])
        result, status = backend.execute_payload({
            "language": "python",
            "code": 'print("x" * 20000)\n' + "\n".join(["value = 1"] * 600),
        })
        self.assertEqual(status, 200)
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), backend.MAX_OUTPUT_BYTES)
        self.assertTrue(result["limits"]["stdout_truncated"])
        self.assertTrue(result["limits"]["events_truncated"])

    def test_verify_does_not_execute_python(self):
        result, status = backend.verify_payload({"language": "python", "code": "raise RuntimeError('no run')"})
        self.assertEqual(status, 200)
        self.assertTrue(result["valid"])

    def test_javascript_is_real_node_execution_or_honest_unsupported(self):
        result, status = backend.execute_payload({"language": "javascript", "code": "console.log(6 * 7);"})
        self.assertEqual(status, 200)
        if backend._node_command():
            self.assertEqual(result["status"], "completed")
            self.assertIn("42", result["output"])
        else:
            self.assertEqual(result["status"], "unsupported")
            self.assertFalse(result["supported"])

    def test_ai_is_honest_without_credentials(self):
        old_key = os.environ.pop("CODEFLOW_AI_API_KEY", None)
        try:
            result, status = backend.ai_payload({"mode": "chat", "prompt": "Explain", "language": "python", "code": "x = 1"})
        finally:
            if old_key is not None:
                os.environ["CODEFLOW_AI_API_KEY"] = old_key
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "not-configured")
        self.assertFalse(result["configured"])
        self.assertIsNone(result["answer"])
        self.assertEqual(result["provider_status"], "not-configured")

    def test_ai_sends_context_and_validates_structured_provider_response(self):
        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, _limit):
                return json.dumps({
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "answer": "The selected line assigns the value.",
                                "proposed_code": {
                                    "language": "python",
                                    "code": "value = 2\nprint(value)",
                                    "explanation": "Keep the trace observable.",
                                },
                            }),
                        },
                    }],
                }).encode("utf-8")

        payload = {
            "mode": "chat",
            "prompt": "Explain the selected event",
            "language": "python",
            "code": "value = 1\nprint(value)",
            "diagnostics": [{"kind": "runtime", "title": "Example", "line": 1}],
            "selected_event": {"sequence": 0, "type": "line", "line": 1, "variables": {"value": 1}},
        }
        with patch.dict(os.environ, {
            "CODEFLOW_AI_API_KEY": "test-key",
            "CODEFLOW_AI_BASE_URL": "https://provider.example/v1",
            "CODEFLOW_AI_MODEL": "test-model",
        }, clear=False), patch("backend.urlopen", return_value=MockResponse()) as open_url:
            result, status = backend.ai_payload(payload)

        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider_status"], "remote")
        self.assertEqual(result["answer"], "The selected line assigns the value.")
        self.assertEqual(result["proposed_code"]["code"], "value = 2\nprint(value)")
        request = open_url.call_args.args[0]
        request_body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://provider.example/v1/chat/completions")
        self.assertEqual(request_body["model"], "test-model")
        self.assertIn("selected_event", request_body["messages"][1]["content"])
        self.assertIn("Example", request_body["messages"][1]["content"])

    def test_ai_rejects_unstructured_provider_response(self):
        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, _limit):
                return b'{"choices":[{"message":{"content":"plain text"}}]}'

        with patch.dict(os.environ, {
            "CODEFLOW_AI_API_KEY": "test-key",
            "CODEFLOW_AI_BASE_URL": "https://provider.example/v1",
            "CODEFLOW_AI_MODEL": "test-model",
        }, clear=False), patch("backend.urlopen", return_value=MockResponse()):
            result, status = backend.ai_payload({"prompt": "Explain", "language": "python", "code": "x = 1"})

        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["provider_status"], "error")
        self.assertIsNone(result["answer"])


if __name__ == "__main__":
    unittest.main()
