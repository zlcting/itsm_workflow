"""Attachment failure feedback with synthetic responses; no network or ticket calls."""

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflow_src"))
import runtime as rt


CONTRACT = {"version": 1, "upload": {
    "verified": True, "source": "json", "body_path": "0", "url_path": "data.url",
    "success_path": "success", "success_values": [True],
}}


class AttachmentFeedbackTests(unittest.TestCase):
    def upload(self, response, **kwargs):
        return rt.upload_result("", response, "fixture-file", rt.dump(CONTRACT), **kwargs)["result"]

    def test_business_error_reaches_user_and_preserves_draft(self):
        state = rt.initial_state()
        state["draft"] = {"factory": "8061", "description": "测试问题"}
        state["preview"] = "old-confirmation"
        result = self.upload([{"success": False, "message": "文件大小不能超过10MB", "code": "FILE_TOO_LARGE"}])
        merged = rt.merge_uploads([result], rt.dump(state))
        self.assertEqual(merged["route"], "FAILED")
        self.assertIn("文件大小不能超过10MB", merged["reply"])
        self.assertIn("FILE_TOO_LARGE", merged["reply"])
        self.assertIn("重试上传", merged["reply"])
        saved = rt.obj(merged["state"])
        self.assertEqual(saved["draft"], state["draft"])
        self.assertEqual(saved["failed_uploads"], ["fixture-file"])
        self.assertEqual(saved["preview"], "")

    def test_transport_error_cannot_be_mistaken_for_success(self):
        result = self.upload([{"success": True, "data": {"url": "https://example.invalid/stale.png"}}],
                             error_message="HTTP 502: 上传服务不可用", error_type="ToolInvokeError")
        item = rt.obj(result)
        self.assertEqual(item["url"], "")
        reply = rt.merge_uploads([result], "{}")["reply"]
        self.assertIn("HTTP 502", reply)
        self.assertIn("ToolInvokeError", reply)

    def test_actual_error_details_are_redacted_before_serializing_or_display(self):
        result = self.upload([{"success": False, "message": "token=body-secret 文件格式错误",
                               "authorization": "Bearer body-token", "nested": {"password": "private-password"}}],
                             error_message="Authorization: Bearer invoke-secret")
        reply = rt.merge_uploads([result], "{}")["reply"]
        for value in (result, reply):
            for secret in ("body-secret", "body-token", "private-password", "invoke-secret"):
                self.assertNotIn(secret, value)
        self.assertIn("文件格式错误", reply)
        self.assertIn("[已隐藏凭据]", reply)

    def test_bad_mapping_does_not_hide_raw_tool_output_or_guess_an_address(self):
        result = self.upload([{"status": 500, "message": "对象存储写入失败", "body": {"url": "https://example.invalid/unmapped.png"}}])
        item = rt.obj(result)
        self.assertEqual(item["url"], "")
        self.assertIn("对象存储写入失败", item["error"])
        c = copy.deepcopy(CONTRACT)
        c["upload"]["body_path"] = "missing"
        unmapped = rt.upload_result("", [{"message": "实际错误仍在外层"}], "x", rt.dump(c))
        self.assertIn("实际错误仍在外层", rt.obj(unmapped["result"])["error"])

    def test_unverified_upload_contract_stays_blocked(self):
        c = copy.deepcopy(CONTRACT)
        c["upload"]["verified"] = False
        contract = rt.dump(c)
        self.assertEqual(rt.attachment_gate("{}", [{"key": "x"}], contract)["route"], "BLOCKED")
        result = rt.upload_result("", [{"success": True, "data": {"url": "https://example.invalid/test.png"}}], "x", contract)
        self.assertEqual(rt.obj(result["result"])["url"], "")
        self.assertIn("尚未验证", rt.obj(result["result"])["error"])

    def test_successful_uploads_and_cached_legacy_results_remain_compatible(self):
        result = self.upload([{"success": True, "data": {"url": "https://example.invalid/test.png"}}])
        state = rt.initial_state()
        state["draft"] = {"description": "需要保留"}
        state["failed_uploads"] = ["previous-file"]
        merged = rt.merge_uploads([result, rt.dump({"key": "cached", "url": "https://example.invalid/cached.png"})], rt.dump(state))
        self.assertEqual(merged["route"], "READY")
        self.assertEqual(merged["reply"], "")
        self.assertEqual(len(rt.obj(merged["state"])["uploads"]), 2)
        self.assertEqual(rt.obj(merged["state"])["failed_uploads"], [])

    def test_mixed_results_keep_successes_for_retry(self):
        good = self.upload([{"success": True, "data": {"url": "https://example.invalid/test.png"}}])
        bad = rt.dump({"key": "bad-file", "url": "", "error": "HTTP 403: 签名已过期"})
        first = rt.merge_uploads([good, bad], "{}")
        self.assertIn("1 张截图读取或上传失败", first["reply"])
        self.assertIn("签名已过期", first["reply"])
        retry = rt.merge_uploads([rt.dump({"key": "bad-file", "url": "https://example.invalid/retry.png"})], first["state"])
        self.assertEqual(retry["route"], "READY")
        self.assertEqual(len(rt.obj(retry["state"])["uploads"]), 2)

    def test_download_status_and_response_reach_user(self):
        checked = rt.downloaded(403, [], key="expired-file", body='{"message":"截图链接签名已过期","token":"secret"}')
        self.assertEqual(checked["ready"], "no")
        merged = rt.merge_uploads([checked["result"]], "{}")
        self.assertIn("HTTP 403", merged["reply"])
        self.assertIn("截图链接签名已过期", merged["reply"])
        self.assertNotIn('"secret"', merged["reply"])
        self.assertEqual(rt.obj(merged["state"])["failed_uploads"], ["expired-file"])

    def test_download_exception_preserves_specific_error_and_legacy_ready(self):
        result = rt.downloaded(0, [], error_message="TLS certificate verification failed", error_type="SSLError")
        self.assertEqual(result["ready"], "no")
        self.assertIn("SSLError", result["feedback"])
        self.assertIn("TLS certificate verification failed", result["feedback"])
        self.assertEqual(rt.downloaded(200, [{"id": "one"}])["ready"], "yes")
        self.assertEqual(rt.downloaded(200, [])["ready"], "no")
        self.assertEqual(rt.downloaded(200, [{"id": "one"}, {"id": "two"}])["ready"], "no")

    def test_download_200_without_a_file_is_not_called_a_success(self):
        result = rt.downloaded(200, [], body="登录状态已过期，请重新登录")
        self.assertEqual(result["ready"], "no")
        self.assertIn("HTTP 200", result["feedback"])
        self.assertIn("未取得单个可上传文件", result["feedback"])
        self.assertIn("登录状态已过期", result["feedback"])

    def test_failure_feedback_is_literal_bounded_and_deduplicated(self):
        malicious = "业务提示\n```\n[点击](javascript:bad)\n```\n" + "过长" * 5000
        result = rt.dump({"key": "x", "url": "", "error": malicious})
        reply = rt.merge_uploads([result, result], "{}")["reply"]
        self.assertIn("````text\n", reply)
        self.assertEqual(reply.count("业务提示"), 1)
        self.assertLess(len(reply), 6500)
        self.assertIn("已截断", reply)

    def test_empty_or_malformed_upload_contract_does_not_crash(self):
        for contract in ("{}", "broken", rt.dump({"version": 1, "upload": None})):
            result = rt.upload_result("上传被拒绝", [], "x", contract)
            self.assertEqual(rt.obj(result["result"])["url"], "")
            self.assertIn("上传被拒绝", rt.obj(result["result"])["error"])


if __name__ == "__main__":
    unittest.main()
