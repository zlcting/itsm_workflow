"""Regress the post-submit clear failure using controlled tool responses."""

import copy
import unittest
from unittest.mock import patch

import test_workflow as workflow
from build import Graph
from compatibility import native_variable_assigner_clears


class ClearAssignerTests(unittest.TestCase):
    def setUp(self):
        self.original = workflow.APP
        self.fixed = native_variable_assigner_clears(self.original)

    @staticmethod
    def clear_items(app):
        return [item for node in app["workflow"]["graph"]["nodes"]
                if node["data"]["type"] == "assigner"
                for item in node["data"].get("items", []) if item["operation"] == "clear"]

    def ready(self, files=None):
        r = workflow.Runner()
        reply = r.run("提交工单", workflow.event("CREATE_TICKET", fields={
            "factory": "8061", "serviceCode": "WMS"}), files=files)
        self.assertIn("请确认", reply)
        self.assertEqual(len(r.create_payloads), 0)
        return r

    def test_generator_emits_native_clear_without_source(self):
        item = Graph().write("pending_files", operation="clear")
        self.assertEqual(item, {"variable_selector": ["conversation", "pending_files"],
                                "input_type": "variable", "operation": "clear", "value": ""})
        self.assertEqual(Graph().write("state", ["node", "state"])["value"], ["node", "state"])

    def test_compatibility_fix_changes_only_clear_input_and_is_idempotent(self):
        broken = copy.deepcopy(self.original)
        for item in self.clear_items(broken):
            item["input_type"] = "constant"
        before = copy.deepcopy(broken)
        fixed = native_variable_assigner_clears(broken)
        self.assertEqual(broken, before)
        self.assertTrue(self.clear_items(fixed))
        self.assertEqual(native_variable_assigner_clears(fixed), fixed)
        for item in self.clear_items(fixed):
            self.assertEqual(item["input_type"], "variable")
            self.assertEqual(item["value"], "")
            item["input_type"] = "constant"
        self.assertEqual(fixed, before)

    def test_old_constant_clear_reproduces_error_after_success_was_persisted(self):
        broken = copy.deepcopy(self.fixed)
        for item in self.clear_items(broken):
            item["input_type"] = "constant"
        with patch.object(workflow, "APP", broken):
            r = self.ready([workflow.IMAGE])
            pending_before = copy.deepcopy(r.conversation["pending_files"])
            with self.assertRaisesRegex(workflow.NodeFailure,
                                        "Input type constant is not supported for operation clear"):
                r.run("确认提交", workflow.event("CONFIRM", problem="", query=""))
            # This failure occurs after a successful create, so retrying the
            # actual API would be unsafe; no real service is called by this test.
            self.assertEqual(len(r.create_payloads), 1)
            self.assertEqual(r.state["submission"]["status"], "success")
            self.assertEqual(r.reply, "")
            self.assertEqual(r.conversation["pending_files"], pending_before)

    def test_success_reaches_reply_clears_files_and_does_not_resubmit(self):
        for files in ([], [workflow.IMAGE]):
            with self.subTest(files=bool(files)), patch.object(workflow, "APP", self.fixed):
                r = self.ready(files)
                reply = r.run("确认提交", workflow.event("CONFIRM", problem="", query=""))
                self.assertIn("工单已提交成功", reply)
                self.assertEqual(r.conversation["pending_files"], [])
                self.assertEqual(r.state["submission"]["status"], "success")
                self.assertEqual(len(r.create_payloads), 1)
                r.run("确认提交", workflow.event("CONFIRM", problem="", query=""))
                self.assertEqual(len(r.create_payloads), 1)


if __name__ == "__main__":
    unittest.main()
