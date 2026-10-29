"""Native per-turn file upload boundaries, with synthetic tools only."""

import copy
import json
import unittest

import test_workflow as w
from build_native_attachment_fix import native_candidate


def image(key):
    return {**w.IMAGE, "id": key, "related_id": key, "url": "https://example.invalid/" + key + ".png"}


def uploaded(key):
    return [{"status": 200, "message": "操作成功", "body": "https://example.invalid/uploaded/" + key + ".png"}]


class NativeRunner(w.Runner):
    def get(self, selector):
        value = super().get(selector)
        if selector == ["1791936000520", "text"]:
            # Keep the existing per-image success/failure fixture format, but
            # model the actual tool's successful text output as a plain URL.
            body = self.pool[selector[0]]["json"]
            if body and isinstance(body[0], dict) and body[0].get("status") == 200 and isinstance(body[0].get("body"), str):
                return body[0]["body"]
        return value


class NativeAttachmentFlowTests(unittest.TestCase):
    def setUp(self):
        self.old_app = w.APP
        self.app = native_candidate()
        w.APP = self.app
        self.addCleanup(setattr, w, "APP", self.old_app)
        self.contract = json.loads(next(v for v in self.app["workflow"]["environment_variables"] if v["name"] == "itsm_response_contract")["value"])
        self.contract["upload"]["verified"] = True

    def runner(self):
        return NativeRunner(self.contract)

    def ready(self, r=None, files=None, **kwargs):
        r = r or self.runner()
        reply = r.run("提交工单，WMS库存不足", w.event("CREATE_TICKET", fields={"factory": "8061", "serviceCode": "WMS"}),
                      files=files, **kwargs)
        self.assertEqual(r.create_payloads, [])
        self.assertEqual(r.count("download"), 0)
        return r, reply

    def test_native_file_path_has_no_http_and_no_file_conversation_variable(self):
        nodes = {n["id"]: n["data"] for n in self.app["workflow"]["graph"]["nodes"]}
        self.assertFalse(any(n["type"] in ("http-request", "list-operator") for n in nodes.values()))
        self.assertEqual(nodes["1791936000450"]["iterator_selector"], ["sys", "files"])
        self.assertEqual(nodes["1791936000450"]["iterator_input_type"], "array[file]")
        self.assertEqual(nodes["1791936000520"]["tool_parameters"]["file"]["value"], ["1791936000450", "item"])
        self.assertNotIn("array[file]", [v["value_type"] for v in self.app["workflow"]["conversation_variables"]])
        checks = w.GraphTests()
        checks.test_all_variable_references_resolve_to_declared_outputs()
        checks.test_all_nodes_reachable_and_no_cycles()
        checks.test_unique_nodes_edges_and_valid_handles()
        checks.test_embedded_code_inputs_and_schemas()

    def test_rebuilt_upload_mapping_preserves_actual_verified_text_contract(self):
        expected = {"verified": True, "source": "text", "body_path": "", "url_path": "",
                    "success_path": "", "success_values": []}
        self.assertEqual(self.contract["upload"], expected)
        from pathlib import Path
        import yaml
        final = yaml.safe_load((Path(__file__).resolve().parents[1] / "系统异常助手_附件上传修复版.yml").read_text())
        actual = json.loads(next(v for v in final["workflow"]["environment_variables"] if v["name"] == "itsm_response_contract")["value"])
        self.assertEqual(actual["upload"], expected)

    def test_no_code_input_receives_single_native_iteration_file(self):
        nodes = {n["id"]: n["data"] for n in self.app["workflow"]["graph"]["nodes"]}
        for node in nodes.values():
            if node["type"] == "code":
                for value in node["variables"]:
                    self.assertNotEqual(value["value_selector"], ["1791936000450", "item"], node["title"])
        cached = nodes["1791936000470"]
        self.assertEqual({v["variable"]: v["value_selector"] for v in cached["variables"]}, {
            "state": ["conversation", "state"], "pending": ["conversation", "pending_files"],
            "index": ["1791936000450", "index"], "files": ["sys", "files"],
        })
        self.assertEqual(nodes["1791936000520"]["tool_parameters"]["file"]["value"], ["1791936000450", "item"])

    def test_missing_iteration_result_is_reported_as_incomplete_not_a_previous_image(self):
        pending = [{"key": "current", "url": "https://example.invalid/current.png"}]
        for results in ([None], None, [""]):
            merged = w.rt.merge_uploads(results, "{}", pending, "TICKET")
            self.assertEqual(merged["route"], "FAILED")
            self.assertIn("附件处理未完成，流程没有返回上传结果", merged["reply"])
            self.assertNotIn("之前的截图", merged["reply"])
            self.assertEqual(w.rt.obj(merged["state"])["failed_uploads"], ["current"])

    def test_invalid_iteration_index_returns_explicit_error_without_using_wrong_file(self):
        for index in (-1, 2, "0", None):
            result = w.rt.native_upload_cached("{}", [{"key": "first"}], index, [image("first")])
            self.assertEqual(result["cached"], "yes")
            self.assertIn("未能取得当前截图元数据", w.rt.obj(result["result"])["error"])

    def test_query_uploads_now_and_ticket_reuses_url_without_original_file(self):
        r = self.runner()
        r.run("WMS库存不足", w.event("QUERY"), files=[image("first")], upload_bodies=[uploaded("first")])
        self.assertEqual(r.count("tool"), 1)
        self.assertEqual(r.count("retrieval"), 1)
        self.assertEqual(r.count("download"), 0)
        self.assertEqual(r.state["mode"], "query")
        r, reply = self.ready(r)
        self.assertIn("截图：1 张", reply)
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(list(r.state["uploads"].values()), ["https://example.invalid/uploaded/first.png"])

    def test_only_image_is_saved_without_claiming_to_read_its_content(self):
        r = self.runner()
        reply = r.run("", w.event("QUERY", problem="", query="", sufficient=False), files=[image("only")], upload_bodies=[uploaded("only")])
        self.assertIn("无法直接读取图片", reply)
        self.assertEqual(r.count("tool"), 1)
        self.assertEqual(r.count("retrieval"), 0)
        self.assertEqual(len(r.state["uploads"]), 1)

    def test_cancel_with_a_simultaneous_image_never_uploads_and_clears_references(self):
        r, _ = self.ready(files=[image("old")], upload_bodies=[uploaded("old")])
        reply = r.run("取消工单", w.event("CANCEL"), files=[image("discarded")])
        self.assertIn("已取消", reply)
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.state["uploads"], {})
        self.assertEqual(r.conversation["pending_files"], [])

    def test_omit_with_a_simultaneous_image_never_readds_the_attachment(self):
        r, _ = self.ready(files=[image("old")], upload_bodies=[uploaded("old")])
        reply = r.run("不附截图提交", w.event("TICKET_UPDATE"), files=[image("discarded")])
        self.assertIn("截图：0 张", reply)
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.state["uploads"], {})
        self.assertEqual(r.conversation["pending_files"], [])
        self.assertEqual(r.create_payloads, [])

    def test_resolved_with_image_clears_current_problem_without_upload(self):
        r, _ = self.ready(files=[image("old")], upload_bodies=[uploaded("old")])
        r.run("解决了", w.event("RESOLVED"), files=[image("discarded")])
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.state["problem"], "")
        self.assertEqual(r.state["uploads"], {})
        self.assertEqual(r.conversation["pending_files"], [])

    def test_new_problem_uses_only_new_image_and_clears_old_attachment(self):
        r, _ = self.ready(files=[image("old")], upload_bodies=[uploaded("old")])
        r.run("换个问题，MES桌面死机", w.event("QUERY", new_problem=True, problem="MES桌面死机", query="MES桌面死机"),
              files=[image("new")], upload_bodies=[uploaded("new")])
        self.assertEqual(list(r.state["uploads"]), ["new"])
        self.assertEqual([f["key"] for f in r.conversation["pending_files"]], ["new"])
        self.assertEqual(r.count("tool"), 1)
        self.assertEqual(r.count("retrieval"), 1)

    def test_followup_preserves_uploaded_draft_and_does_not_upload_again(self):
        r, _ = self.ready(files=[image("keep")], upload_bodies=[uploaded("keep")])
        saved = copy.deepcopy(r.state["draft"])
        r.run("继续排查", w.event("FOLLOW_UP", problem="", query="WMS库存不足"))
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.count("retrieval"), 1)
        self.assertEqual(r.state["draft"], saved)
        self.assertEqual(list(r.state["uploads"]), ["keep"])

    def test_tool_error_reaches_user_and_retry_without_file_requires_reattachment(self):
        r, reply = self.ready(files=[image("failed")], fail={"上传截图"}, error_type="ToolInvokeError", error_message="HTTP 503 上传服务不可用")
        self.assertIn("HTTP 503", reply)
        self.assertIn("重新附上", reply)
        self.assertNotIn("你可以回复“重试上传”", reply)
        self.assertEqual(r.state["failed_uploads"], ["failed"])
        reply = r.run("重试上传", w.event("TICKET_UPDATE", problem="", query=""))
        self.assertIn("当前消息没有可重新上传的原文件", reply)
        self.assertIn("替换失败截图", reply)
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.count("download"), 0)
        self.assertEqual(r.create_payloads, [])

    def test_replacing_failed_files_keeps_successful_urls_and_uploads_only_replacement(self):
        r, reply = self.ready(files=[image("good"), image("bad")], upload_bodies=[uploaded("good"), [{"status": 500, "body": False, "message": "第二张失败"}]])
        self.assertIn("第二张失败", reply)
        self.assertEqual(r.state["failed_uploads"], ["bad"])
        reply = r.run("替换失败截图", w.event("TICKET_UPDATE", problem="", query=""), files=[image("replacement")], upload_bodies=[uploaded("replacement")])
        self.assertIn("截图：2 张", reply)
        self.assertEqual(r.count("tool"), 1)
        self.assertEqual(set(r.state["uploads"]), {"good", "replacement"})
        self.assertEqual({f["key"] for f in r.conversation["pending_files"]}, {"good", "replacement"})
        self.assertEqual(r.state["failed_uploads"], [])

    def test_new_image_does_not_silently_drop_an_older_failed_attachment(self):
        r, _ = self.ready(files=[image("old-failed")], fail={"上传截图"})
        reply = r.run("再补充截图", w.event("TICKET_UPDATE", problem="", query=""), files=[image("new-good")], upload_bodies=[uploaded("new-good")])
        self.assertNotIn("请确认以下工单信息", reply)
        self.assertIn("之前的截图未上传成功", reply)
        self.assertEqual(list(r.state["uploads"]), ["new-good"])
        self.assertEqual(r.state["failed_uploads"], ["old-failed"])

    def test_legacy_unuploaded_references_allow_query_but_block_ticket_until_reattached(self):
        r = self.runner()
        r.conversation["pending_files"] = [{"key": "legacy", "url": "https://example.invalid/expired-preview.png", "filename": "old.png"}]
        r.run("WMS库存不足", w.event("QUERY"))
        self.assertEqual(r.count("retrieval"), 1)
        self.assertEqual(r.count("download"), 0)
        self.assertEqual(r.count("tool"), 0)
        r, reply = self.ready(r)
        self.assertIn("重新附上", reply)
        self.assertEqual(r.state["failed_uploads"], ["legacy"])
        reply = r.run("替换失败截图", w.event("TICKET_UPDATE", problem="", query=""), files=[image("replacement")], upload_bodies=[uploaded("replacement")])
        self.assertIn("截图：1 张", reply)
        self.assertEqual([f["key"] for f in r.conversation["pending_files"]], ["replacement"])

    def test_duplicate_current_file_is_uploaded_once(self):
        repeated = image("same")
        r, reply = self.ready(files=[repeated, repeated], upload_bodies=[uploaded("same")])
        self.assertIn("截图：1 张", reply)
        self.assertEqual(r.count("tool"), 1)
        self.assertEqual(r.state["failed_uploads"], [])
        reply = r.run("再附原截图", w.event("TICKET_UPDATE", problem="", query=""), files=[repeated])
        self.assertIn("截图：1 张", reply)
        self.assertEqual(r.count("tool"), 0)

    def test_image_attached_to_confirm_requires_new_confirmation_instead_of_creating(self):
        r, _ = self.ready()
        signature = r.state["preview"]
        reply = r.run("确认提交", w.event("CONFIRM", problem="", query=""), files=[image("added")], upload_bodies=[uploaded("added")])
        self.assertIn("请确认以下工单信息", reply)
        self.assertIn("截图：1 张", reply)
        self.assertNotEqual(r.state["preview"], signature)
        self.assertEqual(r.create_payloads, [])

    def test_unverified_mapping_still_blocks_and_preserves_reference_for_replacement(self):
        self.contract["upload"]["verified"] = False
        r, reply = self.ready(files=[image("not-ready")])
        self.assertIn("附件服务尚未完成接入", reply)
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.state["failed_uploads"], ["not-ready"])
        self.assertEqual([f["key"] for f in r.conversation["pending_files"]], ["not-ready"])


if __name__ == "__main__":
    unittest.main()
