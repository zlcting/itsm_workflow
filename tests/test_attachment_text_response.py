"""Replay the observed upload output shape without uploading another file."""

import ast
import json
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow_src"))
from build import bundle
import runtime as rt


TEXT_CONTRACT = {"version": 1, "upload": {
    "verified": True, "source": "text", "body_path": "", "url_path": "",
    "success_path": "", "success_values": [],
}}
URL = "https://resuat.runner-corp.com.cn:8089/res/attachment-upload-test.png"
STRUCTURED = [{"data": []}]


class AttachmentTextResponseTests(unittest.TestCase):
    def test_observed_plain_text_url_with_empty_json_data_is_successful(self):
        parsed = rt.upload_result(URL, STRUCTURED, "test-file", rt.dump(TEXT_CONTRACT))
        item = rt.obj(parsed["result"])
        self.assertEqual(item["url"], URL)
        self.assertEqual(item["error"], "")
        merged = rt.merge_uploads([parsed["result"]], "{}", [{"key": "test-file"}], "TICKET")
        self.assertEqual(merged["route"], "READY")
        self.assertEqual(rt.obj(merged["state"])["uploads"], {"test-file": URL})

    def test_empty_and_error_text_never_become_an_attachment(self):
        for text in ("", None, "文件大小超出限制", "HTTP 500: upload failed", "https://example.invalid/a.png 上传失败"):
            parsed = rt.upload_result(text, STRUCTURED, "test-file", rt.dump(TEXT_CONTRACT))
            self.assertEqual(rt.obj(parsed["result"])["url"], "", text)
            merged = rt.merge_uploads([parsed["result"]], "{}", [{"key": "test-file"}], "TICKET")
            self.assertEqual(merged["route"], "FAILED")
            self.assertEqual(rt.obj(merged["state"])["uploads"], {})
            if text:
                self.assertIn(text, merged["reply"])

    def test_json_failures_are_reported_without_extracting_an_embedded_url(self):
        failures = [
            {"status": 500, "message": "上传服务报错", "body": False},
            {"status": 500, "message": "上传被拒绝", "body": URL},
            {"success": False, "message": "文件格式不支持", "url": URL},
        ]
        for value in failures:
            parsed = rt.upload_result(rt.dump(value), STRUCTURED, "test-file", rt.dump(TEXT_CONTRACT))
            item = rt.obj(parsed["result"])
            self.assertEqual(item["url"], "")
            self.assertIn(value["message"], item["error"])

    def test_transport_error_overrides_even_a_valid_text_url(self):
        parsed = rt.upload_result(URL, STRUCTURED, "test-file", rt.dump(TEXT_CONTRACT),
                                  error_message="调用中断", error_type="ToolInvokeError", transport_error=True)
        self.assertEqual(rt.obj(parsed["result"])["url"], "")
        self.assertIn("调用中断", rt.obj(parsed["result"])["error"])

    def test_final_graph_mapping_and_embedded_attachment_code_match_runtime(self):
        app = yaml.safe_load((ROOT / "系统异常助手_附件上传修复版.yml").read_text())
        nodes = {n["id"]: n["data"] for n in app["workflow"]["graph"]["nodes"]}
        contract = json.loads(next(v for v in app["workflow"]["environment_variables"] if v["name"] == "itsm_response_contract")["value"])
        self.assertEqual(contract["upload"], TEXT_CONTRACT["upload"])
        functions = {"1791936000420": "native_attachment_gate", "1791936000470": "native_upload_cached",
                     "1791936000530": "upload_result", "1791936000560": "merge_uploads", "1792536001020": "upload_result"}
        for node_id, function in functions.items():
            data = nodes[node_id]
            literals = {"text": "", "structured": [], "transport_error": True} if node_id == "1792536001020" else None
            expected = bundle(function, [v["variable"] for v in data["variables"]], literals)
            self.assertEqual(ast.dump(ast.parse(data["code"])), ast.dump(ast.parse(expected)), node_id)
        self.assertFalse(any(n["type"] == "http-request" for n in nodes.values()))
        for node in nodes.values():
            if node["type"] == "code":
                self.assertNotIn(["1791936000450", "item"], [v["value_selector"] for v in node["variables"]])
        self.assertEqual(nodes["1791936000520"]["tool_parameters"]["file"]["value"], ["1791936000450", "item"])
        # Execute the parser embedded in the actual deliverable, not just the
        # source helper, with the shape observed in the successful tool run.
        scope = {}
        exec(compile(nodes["1791936000530"]["code"], "actual-upload-parser", "exec"), scope)
        parsed = scope["main"](text=URL, structured=STRUCTURED, key="test-file", contract=rt.dump(contract))
        self.assertEqual(rt.obj(parsed["result"])["url"], URL)


if __name__ == "__main__":
    unittest.main()
