"""Execute every attachment outcome in the patched graph with synthetic tools."""

import copy
import json
import unittest

import test_workflow as w
from build_attachment_fix import candidate, minimal_patch, DOWNLOAD, DOWNLOAD_CHECK, DOWNLOAD_ERROR, UPLOAD_ERROR, DOWNLOAD_REJECTED
from attachment_file_adapter import single_native_file, SINGLE_FILE


class AttachmentRunner(w.Runner):
    def __init__(self, contract, http=None):
        super().__init__(contract)
        self.http = http or {}

    def get(self, selector):
        # The original interpreter simulates a successful HTTP file download.
        # Add its response body, or override status/body/files for failure tests.
        if selector[0] == DOWNLOAD and DOWNLOAD in self.pool and "status_code" in self.pool[DOWNLOAD]:
            if selector[1] in self.http:
                return self.http[selector[1]]
            if selector[1] == "body":
                return ""
        return super().get(selector)


class AttachmentGraphTests(unittest.TestCase):
    def setUp(self):
        self.old_app = w.APP
        self.old_reference = w.RETRIEVAL_REFERENCE
        self.app = candidate()
        w.APP = self.app
        w.RETRIEVAL_REFERENCE = self.app
        self.addCleanup(setattr, w, "APP", self.old_app)
        self.addCleanup(setattr, w, "RETRIEVAL_REFERENCE", self.old_reference)
        contract_env = next(v for v in self.app["workflow"]["environment_variables"] if v["name"] == "itsm_response_contract")
        self.contract = json.loads(contract_env["value"])

    def runner(self, http=None, enabled=True):
        contract = copy.deepcopy(self.contract)
        contract["upload"]["verified"] = enabled
        return AttachmentRunner(contract, http)

    def ready(self, runner=None, files=None, upload_bodies=None, **kwargs):
        r = runner or self.runner()
        reply = r.run("提交工单，MES测试缺料", w.event("CREATE_TICKET", problem="MES测试缺料", query="MES测试缺料",
                      fields={"factory": "8061", "serviceCode": "MES", "description": "MES测试缺料"}),
                      files=[w.IMAGE] if files is None else files,
                      upload_bodies=upload_bodies or [[{"status": 200, "message": "操作成功", "body": "https://example.invalid/test.png"}]],
                      **kwargs)
        self.assertEqual(r.create_payloads, [])
        return r, reply

    def test_default_mapping_matches_schema_but_remains_unverified(self):
        self.assertEqual(self.contract["upload"], {"verified": False, "source": "json", "body_path": "0",
                         "url_path": "body", "success_path": "status", "success_values": [200]})
        r, reply = self.ready(self.runner(enabled=False))
        self.assertIn("附件服务尚未完成接入", reply)
        self.assertEqual(r.count("download"), 0)
        self.assertEqual(r.count("tool"), 0)

    def test_only_attachment_nodes_edges_and_upload_mapping_are_changed(self):
        from build_attachment_fix import SOURCE
        import yaml
        original = yaml.safe_load(SOURCE.read_text())
        changed_ids = {"1791936000490", "1791936000500", "1791936000520", "1791936000530",
                       "1791936000540", "1791936000550", "1791936000560"}
        old_nodes = {n["id"]: n for n in original["workflow"]["graph"]["nodes"]}
        new_nodes = {n["id"]: n for n in self.app["workflow"]["graph"]["nodes"]}
        self.assertEqual(set(new_nodes) - set(old_nodes), {DOWNLOAD_ERROR, UPLOAD_ERROR, DOWNLOAD_REJECTED, SINGLE_FILE})
        for node_id in set(old_nodes) - changed_ids:
            self.assertEqual(old_nodes[node_id], new_nodes[node_id], node_id)
        for key in set(original) - {"workflow"}:
            self.assertEqual(original[key], self.app[key])
        for key in set(original["workflow"]) - {"graph", "environment_variables"}:
            self.assertEqual(original["workflow"][key], self.app["workflow"][key])
        old_env = {v["name"]: v for v in original["workflow"]["environment_variables"]}
        new_env = {v["name"]: v for v in self.app["workflow"]["environment_variables"]}
        self.assertEqual(old_env["dify_files_base_url"], new_env["dify_files_base_url"])
        self.assertEqual(json.loads(old_env["itsm_response_contract"]["value"])["create"], self.contract["create"])
        old_external_edges = [e for e in original["workflow"]["graph"]["edges"] if not e["data"].get("isInIteration")]
        new_external_edges = [e for e in self.app["workflow"]["graph"]["edges"] if not e["data"].get("isInIteration")]
        self.assertEqual(old_external_edges, new_external_edges)
        self.assertEqual(new_nodes["1791936000520"]["data"]["tool_parameters"]["file"]["value"], [SINGLE_FILE, "first_record"])

    def test_minimal_patch_reconstructs_exact_candidate(self):
        from build_attachment_fix import SOURCE
        import yaml
        original = yaml.safe_load(SOURCE.read_text())
        patch = minimal_patch(original, self.app)
        reconstructed = copy.deepcopy(original)
        graph = reconstructed["workflow"]["graph"]
        nodes = {n["id"]: n for n in graph["nodes"]}
        for updated in patch["updated_node_data"]:
            nodes[updated["id"]]["data"] = updated["data"]
        graph["nodes"].extend(patch["new_nodes"])
        replaced = {e["id"]: e for e in patch["edges"]["replace"]}
        graph["edges"] = [replaced.get(e["id"], e) for e in graph["edges"] if e["id"] not in patch["edges"]["remove_ids"]]
        graph["edges"].extend(patch["edges"]["add"])
        env = {v["id"]: v for v in patch["updated_environment_variables"]}
        reconstructed["workflow"]["environment_variables"] = [env.get(v["id"], v) for v in reconstructed["workflow"]["environment_variables"]]
        for app in (reconstructed, self.app):
            app["workflow"]["graph"]["nodes"].sort(key=lambda n: n["id"])
            app["workflow"]["graph"]["edges"].sort(key=lambda e: e["id"])
        self.assertEqual(reconstructed, self.app)

    def test_declared_selectors_handles_reachability_and_code_arguments(self):
        checks = w.GraphTests()
        checks.test_unique_nodes_edges_and_valid_handles()
        checks.test_all_variable_references_resolve_to_declared_outputs()
        checks.test_all_nodes_reachable_and_no_cycles()
        checks.test_embedded_code_inputs_and_schemas()
        join = next(n["data"] for n in self.app["workflow"]["graph"]["nodes"] if n["data"]["title"] == "汇总单张图片结果")
        self.assertNotIn([DOWNLOAD_CHECK, "result"], join["variables"])

    def test_success_is_not_shadowed_by_the_download_check_result(self):
        r, reply = self.ready()
        self.assertIn("截图：1 张", reply)
        self.assertIn("请确认", reply)
        self.assertEqual(list(r.state["uploads"].values()), ["https://example.invalid/test.png"])
        self.assertEqual(r.count("tool"), 1)
        reply = r.run("补充截图", w.event("TICKET_UPDATE", problem="", query=""))
        self.assertIn("截图：1 张", reply)
        self.assertEqual(r.count("download"), 0)
        self.assertEqual(r.count("tool"), 0)

    def test_http_failure_uses_its_own_result_and_does_not_upload(self):
        r, reply = self.ready(self.runner(http={"status_code": 403, "files": [], "body": '{"message":"签名过期","token":"secret"}'}))
        self.assertIn("HTTP 403", reply)
        self.assertIn("签名过期", reply)
        self.assertNotIn('"secret"', reply)
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.state["failed_uploads"], ["test-file"])
        self.assertEqual(r.state["uploads"], {})

    def test_http_transport_failure_reaches_error_branch(self):
        r, reply = self.ready(fail={"读取已保留的截图"}, error_type="SSLError", error_message="证书校验失败")
        self.assertIn("SSLError", reply)
        self.assertIn("证书校验失败", reply)
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.state["failed_uploads"], ["test-file"])

    def test_upload_transport_failure_reaches_error_branch(self):
        r, reply = self.ready(fail={"上传截图"}, error_type="ToolInvokeError", error_message="HTTP 502 上传超时")
        self.assertIn("ToolInvokeError", reply)
        self.assertIn("HTTP 502 上传超时", reply)
        self.assertEqual(r.state["failed_uploads"], ["test-file"])
        self.assertEqual(r.state["uploads"], {})

    def test_upload_business_failure_keeps_actual_message(self):
        r, reply = self.ready(upload_bodies=[[{"status": 500, "message": "文件超出大小限制", "body": False}]])
        self.assertIn("文件超出大小限制", reply)
        self.assertIn("500", reply)
        self.assertEqual(r.state["uploads"], {})

    def test_mixed_upload_retry_skips_successful_files_and_keeps_count(self):
        second = {**w.IMAGE, "id": "second", "related_id": "second"}
        r, reply = self.ready(files=[w.IMAGE, second], upload_bodies=[
            [{"status": 200, "body": "https://example.invalid/one.png"}],
            [{"status": 500, "message": "第二张上传失败", "body": False}],
        ])
        self.assertIn("1 张截图读取或上传失败", reply)
        self.assertIn("第二张上传失败", reply)
        self.assertEqual(len(r.state["uploads"]), 1)
        reply = r.run("重试上传", w.event("TICKET_UPDATE", problem="", query=""), upload_bodies=[[
            {"status": 200, "body": "https://example.invalid/two.png"},
        ]])
        self.assertIn("截图：2 张", reply)
        self.assertEqual(r.count("tool"), 1)
        self.assertEqual(r.count("download"), 1)
        self.assertEqual(r.create_payloads, [])

    def test_omitting_failed_attachment_and_no_attachment_flow_still_confirm(self):
        r, _ = self.ready(fail={"上传截图"})
        reply = r.run("不附截图提交", w.event("TICKET_UPDATE", problem="", query=""))
        self.assertIn("截图：0 张", reply)
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.conversation["pending_files"], [])
        _, reply = self.ready(files=[])
        self.assertIn("截图：0 张", reply)


class NativeFileAdapterTests(unittest.TestCase):
    def setUp(self):
        self.old_app = w.APP
        self.app = single_native_file(candidate())
        w.APP = self.app
        self.addCleanup(setattr, w, "APP", self.old_app)

    def test_optional_adapter_is_idempotent_and_binds_native_single_file(self):
        self.assertEqual(single_native_file(self.app), self.app)
        nodes = {n["id"]: n for n in self.app["workflow"]["graph"]["nodes"]}
        self.assertEqual(nodes[SINGLE_FILE]["data"]["variable"], [DOWNLOAD, "files"])
        self.assertEqual(nodes[SINGLE_FILE]["data"]["var_type"], "array[file]")
        self.assertEqual(nodes[SINGLE_FILE]["data"]["item_var_type"], "file")
        self.assertEqual(nodes["1791936000520"]["data"]["tool_parameters"]["file"]["value"], [SINGLE_FILE, "first_record"])
        checks = w.GraphTests()
        checks.test_upload_consumes_native_http_file_output()
        checks.test_all_variable_references_resolve_to_declared_outputs()
        checks.test_unique_nodes_edges_and_valid_handles()
        checks.test_all_nodes_reachable_and_no_cycles()
        incoming = [e for e in self.app["workflow"]["graph"]["edges"] if e["target"] == SINGLE_FILE]
        self.assertEqual([(e["source"], e["sourceHandle"]) for e in incoming], [("1791936000510", "yes")])

    def test_single_file_array_reaches_tool_as_one_file_and_empty_or_multiple_do_not(self):
        contract = json.loads(next(v for v in self.app["workflow"]["environment_variables"] if v["name"] == "itsm_response_contract")["value"])
        contract["upload"]["verified"] = True
        r = AttachmentRunner(contract)
        reply = r.run("提交工单", w.event("CREATE_TICKET", fields={"factory": "8061", "serviceCode": "MES"}),
                      files=[w.IMAGE], upload_bodies=[[{"status": 200, "body": "https://example.invalid/ok.png"}]])
        self.assertIn("截图：1 张", reply)
        self.assertIs(r.pool[SINGLE_FILE]["first_record"], r.pool[DOWNLOAD]["files"][0])
        self.assertNotIsInstance(r.pool[SINGLE_FILE]["first_record"], list)
        self.assertEqual(r.create_payloads, [])
        for records in ([], [w.IMAGE, w.IMAGE]):
            r = AttachmentRunner(contract, http={"files": records})
            reply = r.run("提交工单", w.event("CREATE_TICKET", fields={"factory": "8061", "serviceCode": "MES"}), files=[w.IMAGE])
            self.assertIn("未取得单个可上传文件", reply)
            self.assertNotIn(SINGLE_FILE, r.pool)
            self.assertEqual(r.count("tool"), 0)


if __name__ == "__main__":
    unittest.main()
