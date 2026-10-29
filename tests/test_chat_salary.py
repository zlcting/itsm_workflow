"""Run the chat-salary candidate with controlled models and tools, never a real ticket."""

import unittest

import test_workflow as workflow
from build_chat_salary import candidate


class ChatSalaryTests(unittest.TestCase):
    def setUp(self):
        self.old_app = workflow.APP
        workflow.APP = candidate()
        self.addCleanup(setattr, workflow, "APP", self.old_app)

    def ready(self, salary=""):
        r = workflow.Runner()
        reply = r.run("WMS 库存不足，工厂1001，提交工单", workflow.event(
            "CREATE_TICKET", fields={"factory": "1001", "serviceCode": "WMS"}), salary=salary)
        return r, reply

    def fill(self, r, query="001234", salary="", event=None):
        # An invalid model result exercises deterministic handling of the reply.
        return r.run(query, {} if event is None else event, salary=salary)

    def confirm(self, r, salary="", **kwargs):
        return r.run("确认提交", workflow.event("CONFIRM", problem="", query=""),
                     salary=salary, **kwargs)

    def test_no_start_form_and_system_identity_is_bound_to_route_and_prepare(self):
        nodes = [n["data"] for n in workflow.APP["workflow"]["graph"]["nodes"]]
        self.assertEqual(next(n for n in nodes if n["type"] == "start")["variables"], [])
        for title in ("更新问题及会话状态", "校验工单与确认版本"):
            variables = next(n for n in nodes if n["title"] == title)["variables"]
            self.assertEqual(next(v["value_selector"] for v in variables if v["variable"] == "salary_id"),
                             ["sys", "salary_id"])
        checks = workflow.GraphTests()
        checks.test_embedded_code_inputs_and_schemas()
        checks.test_all_variable_references_resolve_to_declared_outputs()

    def test_present_system_identity_is_used_without_prompt_and_cannot_be_overridden(self):
        r, reply = self.ready(" 000456 ")
        self.assertIn("请确认", reply)
        self.assertNotIn("请输入你的薪号", reply)
        self.fill(r, "薪号：001234", salary="000456", event=workflow.event(
            "TICKET_UPDATE", fields={"factory": "8061"}, problem="错误的模型摘要"))
        self.confirm(r, "000456")
        self.assertEqual(len(r.create_payloads), 1)
        self.assertEqual(r.create_payloads[0]["createAuthor"], "000456")
        self.assertEqual(r.create_payloads[0]["updateAuthor"], "000456")
        self.assertEqual(r.create_payloads[0]["factoryCode"], "1001")

    def test_missing_system_identity_prompts_in_chat_and_preserves_business_fields(self):
        for salary in (None, "", "  ", "null", "undefined", "None"):
            with self.subTest(salary=salary):
                r, reply = self.ready(salary)
                self.assertIn("请输入你的薪号", reply)
                self.assertTrue(r.state["waiting_salary"])
                self.assertEqual(r.state["draft"]["factory"], "1001")
                self.assertEqual(r.state["draft"]["serviceCode"], "WMS")
                self.assertEqual(r.state["preview"], "")
                self.assertEqual(r.count("retrieval"), 0)
                self.assertEqual(len(r.create_payloads), 0)

    def test_bare_or_labeled_salary_preserves_zeroes_and_requires_confirmation(self):
        for query in ("001234", "薪号：001234", " 001234 "):
            with self.subTest(query=query):
                r, _ = self.ready()
                problem = r.state["problem"]
                reply = self.fill(r, query)
                self.assertIn("请确认", reply)
                self.assertEqual(r.state["draft"]["_manual_salary_id"], "001234")
                self.assertEqual(r.state["problem"], problem)
                self.assertFalse(r.state["waiting_salary"])
                self.assertEqual(len(r.create_payloads), 0)
                self.confirm(r)
                self.assertEqual(len(r.create_payloads), 1)
                self.assertEqual(r.create_payloads[0]["createAuthor"], "001234")
                self.assertEqual(r.create_payloads[0]["updateAuthor"], "001234")

    def test_salary_matching_factory_code_ignores_model_field_and_problem_guesses(self):
        r, _ = self.ready()
        problem = r.state["problem"]
        reply = self.fill(r, "8061", event=workflow.event(
            "QUERY", new_problem=True, problem="8061工厂另一个故障", query="8061工厂", fields={
                "factory": "8061", "code": "8061", "traceId": "8061", "description": "错误摘要"}))
        self.assertIn("请确认", reply)
        self.assertEqual(r.state["draft"]["factory"], "1001")
        self.assertEqual(r.state["draft"]["_manual_salary_id"], "8061")
        self.assertEqual(r.state["problem"], problem)
        self.confirm(r)
        self.assertEqual(r.create_payloads[0]["code"], 0)
        self.assertEqual(r.create_payloads[0]["createAuthor"], "8061")
        self.assertNotEqual(r.create_payloads[0]["traceId"], "8061")

    def test_labeled_salary_correction_replaces_manual_value_and_refreshes_confirmation(self):
        r, _ = self.ready()
        self.fill(r)
        trace, signature = r.state["draft"]["traceId"], r.state["preview"]
        reply = self.fill(r, "薪号：000789")
        self.assertIn("请确认", reply)
        self.assertNotEqual(r.state["preview"], signature)
        self.assertEqual(r.state["draft"]["traceId"], trace)
        self.assertEqual(r.state["draft"]["_manual_salary_id"], "000789")
        self.assertEqual(len(r.create_payloads), 0)
        self.confirm(r)
        self.assertEqual(r.create_payloads[0]["createAuthor"], "000789")

    def test_invalid_salary_never_becomes_business_data_or_allows_submit(self):
        for query in ("薪号：bad id", "薪号：null", "薪号：" + "a" * 129):
            with self.subTest(query=query):
                r, _ = self.ready()
                before = dict(r.state["draft"])
                self.fill(r, query, event=workflow.event("TICKET_UPDATE", fields={"factory": "8061"}))
                self.assertTrue(r.state["waiting_salary"])
                self.assertEqual(r.state["draft"], before)
                self.assertEqual(r.state["preview"], "")
                self.confirm(r)
                self.assertEqual(len(r.create_payloads), 0)

    def test_missing_business_fields_are_requested_before_salary(self):
        r = workflow.Runner()
        reply = r.run("帮我提交工单", workflow.event("CREATE_TICKET"), salary="")
        self.assertIn("工厂", reply)
        self.assertFalse(r.state.get("waiting_salary", False))
        self.assertNotIn("请输入你的薪号", reply)
        reply = r.run("8061 WMS", {}, salary="")
        self.assertEqual(r.state["draft"]["factory"], "8061")
        self.assertEqual(r.state["draft"]["serviceCode"], "WMS")
        self.assertIn("请输入你的薪号", reply)

    def test_invalid_correction_cannot_reuse_old_manual_identity(self):
        r, _ = self.ready()
        self.fill(r)
        self.fill(r, "薪号：bad id")
        self.assertNotIn("_manual_salary_id", r.state["draft"])
        for _ in range(2):
            self.assertIn("请输入你的薪号", self.confirm(r))
        self.assertEqual(len(r.create_payloads), 0)

    def test_bare_switch_problem_without_model_result_discards_old_draft(self):
        r, _ = self.ready()
        reply = r.run("换个问题", {}, salary="")
        self.assertEqual(r.state["draft"], {})
        self.assertFalse(r.state["waiting_salary"])
        self.assertIn("请补充", reply)
        self.assertEqual(r.count("retrieval"), 0)

    def test_explicit_switch_overrides_wrong_model_intent(self):
        r, _ = self.ready()
        r.run("换个问题，MES登录失败", workflow.event("TICKET_UPDATE", new_problem=False,
              problem="MES登录失败", query="MES登录失败"), salary="")
        self.assertFalse(r.state["waiting_salary"])
        self.assertNotIn("factory", r.state["draft"])
        self.assertNotIn("serviceCode", r.state["draft"])
        self.assertEqual(r.state["mode"], "query")

    def test_unknown_submission_does_not_ask_for_replacement_salary(self):
        r, _ = self.ready()
        self.fill(r)
        self.confirm(r, create_body=[{"unrecognized": "模拟超时结果不明"}])
        draft = dict(r.state["draft"])
        reply = self.fill(r, "薪号：bad id")
        self.assertIn("上次提交结果尚待核实", reply)
        self.assertIn("模拟超时结果不明", reply)
        self.assertEqual(r.state["draft"], draft)
        self.assertEqual(len(r.create_payloads), 1)

    def test_confirm_while_waiting_prompts_again_without_consuming_command_as_salary(self):
        r, _ = self.ready()
        reply = self.confirm(r)
        self.assertIn("请输入你的薪号", reply)
        self.assertTrue(r.state["waiting_salary"])
        self.assertNotIn("_manual_salary_id", r.state["draft"])
        self.assertEqual(r.state["preview"], "")
        self.assertEqual(len(r.create_payloads), 0)

    def test_cancel_and_resolved_clear_waiting_and_any_saved_manual_identity(self):
        for command in ("取消工单", "解决了"):
            for filled in (False, True):
                with self.subTest(command=command, filled=filled):
                    r, _ = self.ready()
                    if filled:
                        self.fill(r)
                    r.run(command, {}, salary="")
                    self.assertFalse(r.state.get("waiting_salary", False))
                    self.assertEqual(r.state["draft"], {})
                    self.assertEqual(r.state["preview"], "")
                    self.assertEqual(len(r.create_payloads), 0)

    def test_continue_diagnosis_leaves_salary_wait_and_keeps_current_draft(self):
        r, _ = self.ready()
        reply = r.run("继续排查", {}, salary="")
        self.assertIn("根据知识库", reply)
        self.assertFalse(r.state.get("waiting_salary", False))
        self.assertEqual(r.state["draft"]["factory"], "1001")
        self.assertNotIn("_manual_salary_id", r.state["draft"])
        reply = r.run("提交工单", {}, salary="")
        self.assertIn("请输入你的薪号", reply)

    def test_explicit_new_problem_clears_waiting_and_manual_identity(self):
        for filled in (False, True):
            with self.subTest(filled=filled):
                r, _ = self.ready()
                if filled:
                    self.fill(r)
                r.run("换个问题，MES登录失败", workflow.event("QUERY", new_problem=True,
                      problem="MES登录失败", query="MES登录失败"), salary="")
                self.assertFalse(r.state.get("waiting_salary", False))
                self.assertNotIn("_manual_salary_id", r.state["draft"])
                self.assertNotIn("factory", r.state["draft"])
                self.assertEqual(r.state["problem"], "MES登录失败")
                self.assertEqual(len(r.create_payloads), 0)

    def test_returning_or_changed_system_identity_requires_new_confirmation(self):
        r, _ = self.ready()
        self.fill(r)
        trace = r.state["draft"]["traceId"]
        reply = self.confirm(r, "system-user")
        self.assertIn("请确认", reply)
        self.assertEqual(len(r.create_payloads), 0)
        self.assertEqual(r.state["draft"]["traceId"], trace)
        self.confirm(r, "system-user")
        self.assertEqual(r.create_payloads[0]["createAuthor"], "system-user")

    def test_invalid_nonempty_system_identity_cannot_fall_back_and_query_still_works(self):
        for salary in (123, True, "bad id", "x" * 129):
            with self.subTest(salary=salary):
                r, _ = self.ready()
                self.fill(r)
                reply = self.confirm(r, salary)
                self.assertIn("业务系统传入", reply)
                self.assertEqual(r.state["preview"], "")
                self.assertEqual(len(r.create_payloads), 0)
        r = workflow.Runner()
        self.assertIn("根据知识库", r.run("WMS库存不足", salary=None))
        self.assertFalse(r.state.get("waiting_salary", False))
        self.assertEqual(len(r.create_payloads), 0)

    def test_failed_submit_keeps_manual_identity_success_clears_it_and_prevents_duplicate(self):
        r, _ = self.ready()
        self.fill(r)
        reply = self.confirm(r, create_body=[{"success": False, "message": "工厂维护中"}])
        self.assertIn("工厂维护中", reply)
        self.assertEqual(r.state["draft"]["_manual_salary_id"], "001234")
        self.assertEqual(len(r.create_payloads), 1)
        reply = r.run("重试提交", {}, salary="")
        self.assertIn("请确认", reply)
        self.assertEqual(len(r.create_payloads), 1)
        self.confirm(r)
        self.assertEqual(len(r.create_payloads), 2)
        self.assertEqual(r.state["draft"], {})
        self.assertFalse(r.state.get("waiting_salary", False))
        self.confirm(r)
        self.assertEqual(len(r.create_payloads), 2)


if __name__ == "__main__":
    unittest.main()
