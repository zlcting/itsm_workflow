"""Execute the actual fallback candidate; no external request or real ticket."""

import copy
import unittest

import yaml

import test_workflow as workflow
from build_salary_fallback import SOURCE, candidate
from compatibility import native_variable_assigner_clears


class SalaryFallbackTests(unittest.TestCase):
    def setUp(self):
        self.baseline = yaml.safe_load(SOURCE.read_text())
        self.old_app = workflow.APP
        workflow.APP = candidate(self.baseline)
        self.addCleanup(setattr, workflow, "APP", self.old_app)

    def ready(self, salary="", manual=""):
        r = workflow.Runner()
        reply = r.run("提交工单", workflow.event("CREATE_TICKET", fields={"factory": "8061", "serviceCode": "WMS"}),
                      salary=salary, manual_salary=manual)
        return r, reply

    def confirm(self, r, salary="", manual=""):
        return r.run("确认提交", workflow.event("CONFIRM"), salary=salary, manual_salary=manual)

    def test_system_identity_always_wins_over_manual(self):
        for manual in ("other-employee", "bad input", None, True):
            with self.subTest(manual=manual):
                r, reply = self.ready(" 001234 ", manual)
                self.assertIn("请确认", reply)
                self.confirm(r, "001234", manual)
                self.assertEqual(len(r.create_payloads), 1)
                self.assertEqual(r.create_payloads[0]["createAuthor"], "001234")
                self.assertEqual(r.create_payloads[0]["updateAuthor"], "001234")

    def test_empty_system_value_uses_manual_preserving_leading_zero(self):
        for salary in (None, "", "  ", "null", "undefined", "None"):
            with self.subTest(salary=salary):
                r, reply = self.ready(salary, " 000456 ")
                self.assertIn("请确认", reply)
                self.assertEqual(len(r.create_payloads), 0)
                self.confirm(r, salary, "000456")
                self.assertEqual(len(r.create_payloads), 1)
                self.assertEqual(r.create_payloads[0]["createAuthor"], "000456")
                self.assertEqual(r.create_payloads[0]["updateAuthor"], "000456")

    def test_empty_both_keeps_draft_and_cannot_submit(self):
        for manual in ("", None, "  ", "null"):
            r, reply = self.ready(None, manual)
            self.assertIn("薪号输入框", reply)
            self.confirm(r, None, manual)
            self.assertEqual(r.state["draft"]["factory"], "8061")
            self.assertEqual(r.state["preview"], "")
            self.assertEqual(len(r.create_payloads), 0)

    def test_invalid_nonempty_system_value_does_not_use_manual(self):
        for salary in (True, 123, [], {}, "bad id", "id\n123", "a" * 129):
            r, reply = self.ready(salary, "000456")
            self.assertIn("业务系统传入", reply)
            self.confirm(r, salary, "000456")
            self.assertEqual(len(r.create_payloads), 0)

    def test_invalid_manual_value_keeps_draft(self):
        for manual in (True, 123, [], {}, "bad id", "id\x00123", "a" * 129):
            r, reply = self.ready("", manual)
            self.assertIn("手填薪号", reply)
            self.confirm(r, "", manual)
            self.assertEqual(len(r.create_payloads), 0)

    def test_filling_missing_identity_requires_preview_before_submission(self):
        r, _ = self.ready()
        reply = self.confirm(r, "", "000456")
        self.assertIn("请确认", reply)
        self.assertEqual(len(r.create_payloads), 0)
        self.confirm(r, "", "000456")
        self.assertEqual(len(r.create_payloads), 1)

    def test_changed_manual_or_returning_system_identity_requires_confirmation(self):
        for salary, manual in (("", "000789"), ("system-user", "000456")):
            r, _ = self.ready("", "000456")
            trace = r.state["draft"]["traceId"]
            reply = self.confirm(r, salary, manual)
            self.assertIn("请确认", reply)
            self.assertEqual(len(r.create_payloads), 0)
            self.assertEqual(r.state["draft"]["traceId"], trace)
            self.confirm(r, salary, manual)
            self.assertEqual(r.create_payloads[0]["createAuthor"], salary or manual)

    def test_removing_identity_invalidates_previous_confirmation(self):
        r, _ = self.ready("", "000456")
        self.confirm(r, "", "")
        self.assertEqual(r.state["preview"], "")
        self.assertEqual(len(r.create_payloads), 0)
        self.assertIn("请确认", self.confirm(r, "", "000456"))
        self.assertEqual(len(r.create_payloads), 0)

    def test_duplicate_confirm_after_manual_submission_creates_once(self):
        r, _ = self.ready("", "000456")
        self.confirm(r, "", "000456")
        reply = self.confirm(r, "", "000456")
        self.assertIn("已提交成功", reply)
        self.assertEqual(len(r.create_payloads), 1)

    def test_knowledge_query_still_works_without_either_identity(self):
        r = workflow.Runner()
        self.assertIn("根据知识库", r.run("WMS库存不足", salary="", manual_salary=""))
        self.assertEqual(len(r.create_payloads), 0)

    def test_only_start_and_prepare_changed_beyond_clear_compatibility(self):
        actual = copy.deepcopy(workflow.APP)
        expected = native_variable_assigner_clears(self.baseline)
        for a, e in zip(actual["workflow"]["graph"]["nodes"], expected["workflow"]["graph"]["nodes"]):
            if e["data"]["type"] == "start":
                self.assertEqual(a["data"]["variables"][0]["variable"], "manual_salary_id")
                self.assertFalse(a["data"]["variables"][0]["required"])
                a["data"]["variables"] = e["data"]["variables"]
            if e["data"]["title"] == "校验工单与确认版本":
                self.assertIn({"variable": "salary_id", "value_selector": ["sys", "salary_id"], "value_type": "string"}, a["data"]["variables"])
                a["data"]["code"] = e["data"]["code"]
                a["data"]["variables"] = e["data"]["variables"]
        self.assertEqual(actual, expected)

    def test_candidate_graph_code_and_variable_wiring(self):
        checks = workflow.GraphTests()
        checks.test_embedded_code_inputs_and_schemas()
        checks.test_all_variable_references_resolve_to_declared_outputs()


if __name__ == "__main__":
    unittest.main()
