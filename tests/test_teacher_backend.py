"""Focused coverage for the backend's generic Python Teacher-mode metadata."""

import json
import unittest

import backend


def run(code, language="python"):
    result, status = backend.execute_payload({"language": language, "code": code})
    assert status == 200, (status, result)
    return result


class TeacherBackendTests(unittest.TestCase):
    def test_arithmetic_assignment_has_compact_line_info(self):
        result = run("answer = 6 * 7\nprint(answer)")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["variables"]["answer"], 42)
        line = result["line_info"][1]
        self.assertEqual(line["kind"], "assignment")
        self.assertEqual(line["writes"], ["answer"])
        self.assertEqual(line["reads"], [])
        self.assertIn("answer = 6 * 7", line["expression"])

    def test_augassign_reports_read_and_write(self):
        result = run("total = 1\ntotal += 4")
        item = result["line_info"][2]
        self.assertEqual(item["kind"], "augassign")
        self.assertIn("total", item["reads"])
        self.assertIn("total", item["writes"])

    def test_for_while_and_if_have_actual_loop_metadata(self):
        code = """total = 0
for item in [1, 2]:
    if item:
        total += item
while total < 4:
    total += 1
print(total)
"""
        result = run(code)
        self.assertEqual(result["output"], ["4"])
        self.assertEqual(result["line_info"][2]["kind"], "for")
        self.assertEqual(result["line_info"][3]["kind"], "if")
        self.assertEqual(result["line_info"][5]["kind"], "while")
        loop_events = [event for event in result["events"] if "loop_iteration" in event]
        self.assertTrue(loop_events)
        self.assertTrue(all(event["loop_iteration"] >= 1 for event in loop_events))

    def test_function_return_has_safe_return_value(self):
        result = run("def double(value):\n    return value * 2\nanswer = double(5)")
        returns = [event for event in result["events"] if event["type"] == "return" and event["function"] == "double"]
        self.assertEqual(returns[-1]["return_value"], 10)
        self.assertEqual(result["line_info"][1]["kind"], "function")

    def test_nested_call_keeps_call_names_and_events(self):
        result = run("def inner(value):\n    return value + 1\ndef outer(value):\n    return inner(value)\nprint(outer(2))")
        self.assertEqual(result["output"], ["3"])
        self.assertIn("inner", result["line_info"][4]["calls"])
        self.assertTrue(any(event["type"] == "call" and event["function"] == "inner" for event in result["events"]))

    def test_list_and_dict_values_are_bounded_and_visible(self):
        result = run("items = [1, 2, 3]\nlookup = {'answer': items[-1]}\nprint(lookup['answer'])")
        self.assertEqual(result["output"], ["3"])
        self.assertEqual(result["variables"]["items"], [1, 2, 3])
        self.assertEqual(result["variables"]["lookup"], {"answer": 3})

    def test_try_except_trace_is_honest(self):
        result = run("try:\n    value = 1 / 0\nexcept ZeroDivisionError:\n    value = 9\nprint(value)")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], ["9"])
        self.assertTrue(any(event["type"] == "exception" for event in result["events"]))
        self.assertEqual(result["line_info"][1]["kind"], "try")

    def test_unicode_nan_and_inf_are_json_safe(self):
        result = run("message = 'café ☕'\npositive = float('inf')\nnot_a_number = float('nan')\nprint(message)")
        self.assertEqual(result["output"], ["café ☕"])
        self.assertEqual(result["variables"]["positive"], "inf")
        self.assertEqual(result["variables"]["not_a_number"], "nan")
        json.dumps(result, allow_nan=False)

    def test_syntax_error_still_has_safe_line_info(self):
        result = run("if True print('bad')")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["diagnostics"][0]["kind"], "syntax")
        self.assertEqual(len(result["line_info"]), 1)
        self.assertEqual(result["line_info"][1]["kind"], "unknown")

    def test_runtime_error_keeps_trace_and_metadata(self):
        result = run("value = 1\nraise RuntimeError('boom')")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["diagnostics"][0]["title"], "RuntimeError")
        self.assertTrue(result["events"])
        self.assertEqual(result["line_info"][2]["kind"], "raise")

    def test_long_trace_is_bounded_and_marks_truncation(self):
        result = run("total = 0\nfor index in range(300000):\n    total += index\nprint(total)")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], ["44999850000"])
        self.assertLessEqual(len(result["events"]), backend.MAX_EVENTS)
        self.assertTrue(result["limits"]["events_truncated"])

    def test_line_info_is_syntax_only_and_does_not_execute_expressions(self):
        source = "result = side_effect()\nclass Example:\n    pass\n"
        line_info = backend._analyze_python_source(source)
        self.assertEqual(line_info[1]["calls"], ["side_effect"])
        self.assertEqual(line_info[1]["writes"], ["result"])
        self.assertEqual(line_info[2]["kind"], "class")
        self.assertEqual(line_info[3]["kind"], "pass")
        self.assertEqual(len(line_info), 3)

    def test_output_delta_is_incremental_and_existing_output_remains(self):
        result = run("print('first', end='')\nprint(' second')")
        self.assertEqual(result["stdout"], "first second\n")
        deltas = [event["output_delta"] for event in result["events"]]
        self.assertIn("first", "".join(deltas))
        self.assertIn(" second\n", "".join(deltas))

    def test_trace_has_explicit_before_and_after_snapshots(self):
        result = run("value = 2\nvalue += 3\nprint(value)")
        updates = [event for event in result["events"] if event["type"] == "line" and event["line"] == 2]
        self.assertTrue(updates)
        update = updates[0]
        self.assertEqual(update["before_variables"]["value"], 2)
        self.assertEqual(update["after_variables"]["value"], 5)

    def test_trace_types_preserve_collection_kinds(self):
        result = run("items = [1, 2]\ncoords = (1, 2)\nunique = {1, 2}\nlookup = {'x': 1}\ntext = 'ok'\nprint(items)")
        assignment_events = {event["line"]: event for event in result["events"] if event["type"] == "line"}
        self.assertEqual(assignment_events[1]["after_types"]["items"], "list")
        self.assertEqual(assignment_events[2]["after_types"]["coords"], "tuple")
        self.assertEqual(assignment_events[3]["after_types"]["unique"], "set")
        self.assertEqual(assignment_events[4]["after_types"]["lookup"], "dict")
        self.assertEqual(assignment_events[5]["after_types"]["text"], "str")
        self.assertIn("items", assignment_events[1]["after_types"])

    def test_ten_arbitrary_programs_keep_trace_evidence(self):
        programs = [
            ("answer = 6 * 7\nprint(answer)", "42"),
            ("value = 10\nvalue -= 3\nprint(value)", "7"),
            ("items = [1, 2, 3]\nprint(items[1])", "2"),
            ("total = 0\nfor item in [2, 4]:\n    total += item\nprint(total)", "6"),
            ("n = 5\nif n > 3:\n    print(n)", "5"),
            ("def double(value):\n    return value * 2\nprint(double(4))", "8"),
            ("text = 'hi'\nprint(text + '!')", "hi!"),
            ("lookup = {'x': 9}\nprint(lookup['x'])", "9"),
            ("try:\n    print(1 / 0)\nexcept ZeroDivisionError:\n    print('handled')", "handled"),
            ("count = 0\nwhile count < 2:\n    count += 1\nprint(count)", "2"),
        ]
        for source, expected in programs:
            with self.subTest(source=source):
                result = run(source)
                self.assertTrue(result["events"])
                self.assertTrue(result["line_info"])
                self.assertIn(expected, result["output"])
                self.assertTrue(any(event.get("before_variables") is not None for event in result["events"]))

    def test_javascript_response_states_output_only(self):
        result = run("console.log(42);", language="javascript")
        self.assertEqual(result["teacher_mode"], "output-only")
        self.assertFalse(result["trace_supported"])
        self.assertIn("output-only", result["trace_message"])
        self.assertEqual(result["events"], [])


if __name__ == "__main__":
    unittest.main()
