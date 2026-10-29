"""Execute the delivered DSL with controlled LLM/retrieval/tool doubles.

No test connects to a knowledge base, uploads a real file, or creates a ticket.
"""

import copy
import hashlib
import inspect
import json
from pathlib import Path
import re
import sys
import unittest

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow_src"))
import build
import prompts
import runtime as rt
from compatibility import native_retrieval_outputs

rt.FACTORIES = json.loads((ROOT / "workflow_src/factories.json").read_text())
APP = yaml.safe_load((ROOT / "系统异常助手_优化版.yml").read_text())
# Export verification can supply the actual pre-edit draft. This preserves
# deliberate user changes to retrieval configuration made after initial import.
RETRIEVAL_REFERENCE = None
VERIFIED_CONTRACT = {
    "version": 1, "verified": True,
    "create": {"source": "json", "body_path": "0", "success_path": "success", "success_values": [True],
               "failure_path": "success", "failure_values": [False], "ticket_id_path": "data.orderNo"},
    "upload": {"verified": True, "source": "json", "body_path": "0", "url_path": "data.url",
               "success_path": "success", "success_values": [True]},
}
CHUNK = {"content": "WMS 库存不足时先核对可用库存和锁定库存。", "title": "库存异常处理",
         "metadata": {"_source": "knowledge", "document_id": "test-doc", "segment_id": "test-chunk"}}
IMAGE = {"id": "test-file", "related_id": "test-file", "type": "image", "transfer_method": "local_file", "url": "https://example.invalid/dify-file.png"}


def event(intent="QUERY", **updates):
    d = {"intent": intent, "new_problem": False, "sufficient": True, "problem": "WMS 提示库存不足", "query": "WMS 库存不足",
         "clarification": "", "attempted": [], "fields": {}}
    d.update(updates)
    return d


def judgment(status="ANSWER", **updates):
    d = {"status": status, "evidence_ids": [1] if status != "NONE" else [],
         "reason": "测试证据", "alternate_query": "WMS 可用库存不足 报错" if status == "NONE" else ""}
    d.update(updates)
    return d


class NodeFailure(Exception):
    pass


class Runner:
    """Small sequential interpreter for the node types used by this DSL."""

    def __init__(self, contract=VERIFIED_CONTRACT):
        self.nodes = {n["id"]: n for n in APP["workflow"]["graph"]["nodes"]}
        self.edges = APP["workflow"]["graph"]["edges"]
        self.conversation = {x["name"]: copy.deepcopy(x["value"]) for x in APP["workflow"]["conversation_variables"]}
        self.contract = copy.deepcopy(contract)
        self.total_calls = []
        self.create_payloads = []

    def get(self, selector):
        key, *parts = selector
        value = self.pool[key]
        for part in parts:
            value = value[part]
        return value

    def render(self, value):
        def replace(match):
            parts = match.group(1).split(".")
            if parts == ["context"]:
                return "[test context]"
            v = self.get(parts)
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return re.sub(r"\{\{#([^#]+)#\}\}", replace, value)

    def run(self, query, event_data=None, files=None, salary="employee-test", retrievals=None, judgments=None,
            answer="根据知识库，请核对可用库存与锁定库存。", fail=None, create_body=None, upload_bodies=None,
            error_message="test error", error_type="TestError", manual_salary=""):
        self.pool = {"sys": {"query": query, "files": files or [], "salary_id": salary}, "conversation": self.conversation,
                     "env": {"itsm_response_contract": rt.dump(self.contract), "dify_files_base_url": ""}}
        self.event = event_data if event_data is not None else event()
        self.retrievals = iter(retrievals if retrievals is not None else [[CHUNK]])
        self.judgments = iter(judgments if judgments is not None else [judgment()])
        self.answer = answer
        self.fail = fail or set()
        self.error_message, self.error_type = error_message, error_type
        self.create_body = create_body if create_body is not None else [{"success": True, "data": {"orderNo": "TEST-001"}}]
        self.upload_bodies = iter(upload_bodies if upload_bodies is not None else [[{"success": True, "data": {"url": "https://example.invalid/test.png"}}]] * 10)
        self.calls = []
        self.reply = ""
        self.steps = 0
        start = next(n for n in self.nodes.values() if n["data"]["type"] == "start")
        self.pool[start["id"]] = {v["variable"]: manual_salary if v["variable"] == "manual_salary_id" else ""
                                 for v in start["data"]["variables"]}
        self.walk(start["id"])
        self.total_calls.extend(self.calls)
        self.conversation = self.pool["conversation"]
        return self.reply

    def walk(self, node_id, iteration=False):
        while node_id:
            self.steps += 1
            if self.steps > 300:
                raise AssertionError("unexpected workflow loop")
            n = self.nodes[node_id]
            d = n["data"]
            typ = d["type"]
            handle = "source"
            outputs = {}
            try:
                if d["title"] in self.fail:
                    raise NodeFailure("controlled failure")
                if typ == "start":
                    outputs = self.pool[node_id]
                elif typ == "llm":
                    self.calls.append(("llm", d["title"]))
                    if d["title"] == "理解问题与本轮意图":
                        outputs = {"structured_output": self.event, "text": rt.dump(self.event)}
                    elif d["title"] == "判断证据是否支持回答":
                        item = next(self.judgments)
                        outputs = {"structured_output": item, "text": rt.dump(item)}
                    else:
                        self.get(d["context"]["variable_selector"])
                        outputs = {"text": self.answer}
                elif typ == "knowledge-retrieval":
                    self.calls.append(("retrieval", self.get(d["query_variable_selector"])))
                    outputs = {"result": next(self.retrievals)}
                elif typ == "code":
                    scope = {}
                    exec(compile(d["code"], d["title"], "exec"), scope)
                    args = {v["variable"]: copy.deepcopy(self.get(v["value_selector"])) for v in d["variables"]}
                    outputs = scope["main"](**args)
                    for key, spec in d["outputs"].items():
                        if key not in outputs:
                            raise AssertionError(f"missing output {key}: {d['title']}")
                        if spec["type"] == "string" and not isinstance(outputs[key], str):
                            raise AssertionError(f"invalid string output {key}")
                elif typ == "assigner":
                    for item in d["items"]:
                        _, name = item["variable_selector"]
                        op = item["operation"]
                        if op == "clear":
                            # Match Dify v2 validation, including its exact error:
                            # clear never resolves a source-selector value.
                            if item["input_type"] != "variable":
                                raise NodeFailure(f"Input type {item['input_type']} is not supported for operation clear")
                            self.pool["conversation"][name] = []
                        else:
                            value = self.get(item["value"]) if item["input_type"] == "variable" else item["value"]
                            if op == "extend":
                                self.pool["conversation"][name].extend(copy.deepcopy(value))
                            elif op == "over-write":
                                self.pool["conversation"][name] = copy.deepcopy(value)
                            else:
                                raise AssertionError(op)
                elif typ == "if-else":
                    handle = "false"
                    for case in d["cases"]:
                        if all(self.get(c["variable_selector"]) == c["value"] for c in case["conditions"]):
                            handle = case["case_id"]
                            break
                elif typ == "tool":
                    params = {name: self.get(x["value"]) for name, x in d["tool_parameters"].items()}
                    self.calls.append(("tool", d["tool_name"]))
                    if d["tool_name"] == "addAiOrder":
                        if rt.obj(self.conversation["state"])["submission"].get("status") != "pending":
                            raise AssertionError("submission not persisted before API call")
                        self.create_payloads.append(params)
                        body = self.create_body
                    else:
                        body = next(self.upload_bodies)
                    outputs = {"json": body, "text": rt.dump(body[0]) if body else ""}
                elif typ == "http-request":
                    self.calls.append(("download", self.render(d["url"])))
                    outputs = {"status_code": 200, "body": "", "files": [IMAGE]}
                elif typ == "list-operator":
                    # Only the pass-through adapter is used here. Preserve the
                    # original native value when selecting first_record, as the
                    # Dify List Operator does for an ArrayFileSegment.
                    if any(d[key]["enabled"] for key in ("filter_by", "extract_by", "order_by", "limit")):
                        raise AssertionError("The test interpreter only supports the unfiltered native-file adapter")
                    records = self.get(d["variable"])
                    outputs = {"result": records, "first_record": records[0] if records else None,
                               "last_record": records[-1] if records else None}
                elif typ == "iteration":
                    results = []
                    for i, item in enumerate(self.get(d["iterator_selector"])):
                        for child in self.nodes.values():
                            if child.get("parentId") == node_id:
                                self.pool.pop(child["id"], None)
                        self.pool[node_id] = {"item": item, "index": i}
                        self.walk(d["start_node_id"], iteration=True)
                        results.append(self.get(d["output_selector"]))
                    outputs = {"output": results}
                elif typ == "iteration-start":
                    pass
                elif typ == "variable-aggregator":
                    for selector in d["variables"]:
                        if selector[0] in self.pool:
                            outputs = {"output": self.get(selector)}
                            break
                    else:
                        raise AssertionError("no available aggregate input")
                elif typ == "answer":
                    self.reply = self.render(d["answer"])
                else:
                    raise AssertionError("unknown node type: " + typ)
                self.pool[node_id] = outputs
                if typ == "knowledge-retrieval" and d.get("error_strategy") == "fail-branch":
                    # Actual RunAI regression: successful retrieval has no branch
                    # selection, so neither success-branch nor source continues.
                    handle = None
                elif d.get("error_strategy") == "fail-branch":
                    handle = "success-branch"
            except NodeFailure:
                if d.get("error_strategy") == "fail-branch":
                    handle = "fail-branch"
                    self.pool[node_id] = {"error_type": self.error_type, "error_message": self.error_message}
                elif d.get("error_strategy") == "default-value":
                    self.pool[node_id] = {x["key"]: x["value"] for x in d["default_value"]}
                else:
                    raise
            links = [e for e in self.edges if e["source"] == node_id and e["sourceHandle"] == handle]
            if len(links) > 1:
                raise AssertionError("unexpected parallel path")
            node_id = links[0]["target"] if links else None

    @property
    def state(self):
        return rt.obj(self.conversation["state"])

    def count(self, category):
        return sum(x[0] == category for x in self.calls)


class GraphTests(unittest.TestCase):
    def test_retrieval_uses_only_native_source_output(self):
        graph = APP['workflow']['graph']
        for node in graph['nodes']:
            if node['data']['type'] == 'knowledge-retrieval':
                self.assertNotIn('error_strategy', node['data'])
                outputs = [e for e in graph['edges'] if e['source'] == node['id']]
                self.assertEqual(len(outputs), 1)
                self.assertEqual(outputs[0]['sourceHandle'], 'source')

    def test_compatibility_patch_is_idempotent_and_preserves_models(self):
        patched = native_retrieval_outputs(APP)
        self.assertEqual(patched, APP)
        self.assertEqual(native_retrieval_outputs(patched), patched)

    def test_original_unchanged(self):
        self.assertEqual(hashlib.sha256((ROOT / "系统异常助手.yml").read_bytes()).hexdigest(),
                         "6fde970a5ffee222611b3799e634087cdb3979b9bfda5afae540ec5d65ef5abe")

    def test_unique_nodes_edges_and_valid_handles(self):
        nodes = {n["id"]: n for n in APP["workflow"]["graph"]["nodes"]}
        self.assertEqual(len(nodes), len(APP["workflow"]["graph"]["nodes"]))
        for edge in APP["workflow"]["graph"]["edges"]:
            self.assertIn(edge["target"], nodes)
            source = nodes[edge["source"]]["data"]
            self.assertEqual(edge["data"]["sourceType"], source["type"])
            valid = ({"false"} | {x["case_id"] for x in source["cases"]}) if source["type"] == "if-else" else ({"fail-branch", "success-branch"} if source.get("error_strategy") == "fail-branch" else {"source"})
            self.assertIn(edge["sourceHandle"], valid)

    def test_embedded_code_inputs_and_schemas(self):
        for n in APP["workflow"]["graph"]["nodes"]:
            d = n["data"]
            if d["type"] == "code":
                scope = {}
                exec(compile(d["code"], d["title"], "exec"), scope)
                self.assertEqual(set(inspect.signature(scope["main"]).parameters), {x["variable"] for x in d["variables"]})
            if d.get("structured_output_enabled"):
                jsonschema.Draft7Validator.check_schema(d["structured_output"]["schema"])

    def test_identity_uses_runai_system_variable_without_user_input(self):
        start = next(n["data"] for n in APP["workflow"]["graph"]["nodes"] if n["data"]["type"] == "start")
        prepare = next(n["data"] for n in APP["workflow"]["graph"]["nodes"] if n["data"]["title"] == "校验工单与确认版本")
        self.assertEqual(start["variables"], [])
        self.assertEqual(next(v["value_selector"] for v in prepare["variables"] if v["variable"] == "salary_id"), ["sys", "salary_id"])
        self.assertFalse(any(n["data"]["type"] == "agent" for n in APP["workflow"]["graph"]["nodes"]))

    def test_conversation_variables_use_supported_persistent_types(self):
        allowed = {"string", "number", "boolean", "object", "array[string]", "array[number]", "array[object]", "array[boolean]"}
        for var in APP["workflow"]["conversation_variables"]:
            self.assertIn(var["value_type"], allowed)

    def test_upload_consumes_native_http_file_output(self):
        nodes = {n["id"]: n for n in APP["workflow"]["graph"]["nodes"]}
        uploader = next(n["data"] for n in nodes.values() if n["data"]["title"] == "上传截图")
        origin, field = uploader["tool_parameters"]["file"]["value"]
        source = nodes[origin]["data"]
        if source["type"] == "iteration":
            self.assertEqual(field, "item")
            self.assertEqual(source["iterator_input_type"], "array[file]")
            self.assertEqual(source["iterator_selector"], ["sys", "files"])
            return
        if source["type"] == "list-operator":
            self.assertEqual(field, "first_record")
            origin, field = source["variable"]
        self.assertEqual(nodes[origin]["data"]["type"], "http-request")
        self.assertEqual(field, "files")

    def test_answer_has_bound_context(self):
        nodes = {n["id"]: n for n in APP["workflow"]["graph"]["nodes"]}
        for n in nodes.values():
            d = n["data"]
            if d["title"] == "依据知识库给出处理建议":
                self.assertTrue(d["context"]["enabled"])
                self.assertIn(d["context"]["variable_selector"][0], nodes)
                self.assertTrue(d["citation_mode"])

    def test_retrieval_keeps_baseline_dataset_and_topk(self):
        original = yaml.safe_load((ROOT / "系统异常助手.yml").read_text())
        old = next(n["data"] for n in original["workflow"]["graph"]["nodes"] if n["data"]["type"] == "knowledge-retrieval")
        reference_nodes = ({n["id"]: n["data"] for n in RETRIEVAL_REFERENCE["workflow"]["graph"]["nodes"]}
                           if RETRIEVAL_REFERENCE is not None else None)
        for n in APP["workflow"]["graph"]["nodes"]:
            if n["data"]["type"] == "knowledge-retrieval":
                baseline = reference_nodes[n["id"]] if reference_nodes is not None else old
                self.assertEqual(n["data"]["dataset_ids"], baseline["dataset_ids"])
                self.assertEqual(n["data"]["multiple_retrieval_config"]["top_k"],
                                 baseline["multiple_retrieval_config"]["top_k"])

    def test_all_variable_references_resolve_to_declared_outputs(self):
        nodes = {n["id"]: n for n in APP["workflow"]["graph"]["nodes"]}
        outputs = {"sys": {"query", "files", "salary_id"},
                   "conversation": {x["name"] for x in APP["workflow"]["conversation_variables"]},
                   "env": {x["name"] for x in APP["workflow"]["environment_variables"]}}
        for node_id, n in nodes.items():
            d = n["data"]
            typ = d["type"]
            outputs[node_id] = ({x["variable"] for x in d["variables"]} if typ == "start" else
                                set(d["outputs"]) if typ == "code" else
                                {"text", "structured_output"} if typ == "llm" else
                                {"result"} if typ == "knowledge-retrieval" else
                                {"text", "json", "files"} if typ == "tool" else
                                {"status_code", "body", "headers", "files"} if typ == "http-request" else
                                {"result", "first_record", "last_record"} if typ == "list-operator" else
                                {"item", "index", "output"} if typ == "iteration" else
                                {"output"} if typ == "variable-aggregator" else set())
            if d.get("error_strategy") == "fail-branch":
                # Confirmed in the live RunAI variable picker for tool nodes.
                outputs[node_id].update({"error_message", "error_type"})
        refs = []

        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ("value_selector", "variable_selector", "query_variable_selector", "iterator_selector", "output_selector") and item:
                        refs.append(item)
                    elif key == "value" and isinstance(item, list) and len(item) == 2 and all(isinstance(x, str) for x in item) and item[0] in outputs:
                        refs.append(item)
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, str):
                refs.extend(x.split(".") for x in re.findall(r"\{\{#([^#]+)#\}\}", value) if x != "context")
        for node in nodes.values():
            visit(node["data"])
        for ref in refs:
            self.assertIn(ref[0], outputs, ref)
            self.assertIn(ref[1], outputs[ref[0]], ref)

    def test_all_nodes_reachable_and_no_cycles(self):
        nodes = {n["id"]: n for n in APP["workflow"]["graph"]["nodes"]}
        edges = {}
        for e in APP["workflow"]["graph"]["edges"]:
            edges.setdefault(e["source"], []).append(e["target"])
        for n in nodes.values():
            if n["data"]["type"] == "iteration":
                edges.setdefault(n["id"], []).append(n["data"]["start_node_id"])
        visited, active = set(), set()

        def walk(node_id):
            self.assertNotIn(node_id, active)
            if node_id in visited:
                return
            active.add(node_id)
            for target in edges.get(node_id, []):
                walk(target)
            active.remove(node_id)
            visited.add(node_id)
        walk(next(n["id"] for n in nodes.values() if n["data"]["type"] == "start"))
        self.assertEqual(visited, set(nodes))


class KnowledgeTests(unittest.TestCase):
    def test_same_error_with_images_is_related_even_without_solution(self):
        r = Runner()
        issue = "模具领用失败，模具台账重复"
        record = {**CHUNK, "content": issue + "![image.png](https://example.invalid/one, https://example.invalid/two)"}
        r.run(issue, event(problem=issue, query=issue), retrievals=[[record]],
              judgments=[judgment("NONE")], answer="找到相同问题记录，但缺少处理步骤。")
        self.assertIn("找到相同问题记录", r.reply)
        self.assertEqual(r.count("retrieval"), 1)
        self.assertEqual(r.state["metrics"]["no_hits"], 0)
        self.assertEqual(r.count("tool"), 0)

    def test_partial_fallback_keeps_only_exact_record_and_source(self):
        s = rt.initial_state()
        s.update(problem="模具领用失败，模具台账重复", query="模具领用失败 模具台账重复")
        relevant = {**CHUNK, "content": s["problem"]}
        unrelated = {**CHUNK, "content": "ELM 模具穴数不能为空，请填写穴数"}
        for second in (False, True):
            result = rt.assess(judgment("NONE"), [unrelated, relevant], rt.dump(s), second)
            self.assertEqual(result["route"], "PARTIAL")
            self.assertEqual(result["evidence"], [relevant])
            self.assertEqual(result["alternate"], "")
            self.assertIn("不得声称没有相关内容", result["assessment"])
            self.assertTrue(json.loads(result["assessment"])["record_only"])
            self.assertIn(CHUNK["title"], json.loads(result["assessment"])["record_reply"])

    def test_record_only_reply_uses_document_metadata_and_no_made_up_source(self):
        s = rt.initial_state()
        s.update(problem="模具领用失败，模具台账重复", query="模具领用失败 模具台账重复")
        record = {"content": s["problem"], "title": "段落标题",
                  "metadata": {"document_name": "真实工单报告.xls"}}
        out = json.loads(rt.assess(judgment("PARTIAL"), [record], rt.dump(s), False)["assessment"])
        self.assertEqual(out["sources"], ["真实工单报告.xls"])
        self.assertIn("《真实工单报告.xls》", out["record_reply"])
        self.assertNotIn("《段落标题》", out["record_reply"])
        self.assertNotIn("清理", out["record_reply"])
        without_source = {"content": s["problem"]}
        out = json.loads(rt.assess(judgment("PARTIAL"), [without_source], rt.dump(s), False)["assessment"])
        self.assertNotIn("来源为", out["record_reply"])

    def test_record_only_flag_excludes_records_with_actual_steps(self):
        s = rt.initial_state()
        s.update(problem="模具领用失败，模具台账重复", query="模具领用失败 模具台账重复")
        for suffix, expected in [("", True), ("![截图](https://example.invalid/image.png)", True),
                                 ("。请按文档所列流程提交修复申请。", False)]:
            result = rt.assess(judgment("PARTIAL"), [{**CHUNK, "content": s["problem"] + suffix}],
                               rt.dump(s), False)
            self.assertEqual(json.loads(result["assessment"])["record_only"], expected)

    def test_keyword_overlap_or_short_generic_text_does_not_trigger_fallback(self):
        for issue, content in [("模具领用失败，模具台账重复", "模具穴数不能为空，请填写穴数"),
                               ("系统异常", "系统异常"),
                               ("模具领用失败，模具台账重复", "模具领用失败，模具台账不重复")]:
            s = rt.initial_state()
            s.update(problem=issue, query=issue)
            result = rt.assess(judgment("NONE", alternate_query=""), [{**CHUNK, "content": content}], rt.dump(s), False)
            self.assertEqual(result["route"], "NONE")
            self.assertEqual(result["evidence"], [])

    def test_empty_results_still_report_no_hit_for_same_issue(self):
        s = rt.initial_state()
        s.update(problem="模具领用失败，模具台账重复", query="模具领用失败 模具台账重复")
        result = rt.assess(judgment("NONE", alternate_query=""), [], rt.dump(s), False)
        self.assertEqual(result["route"], "NONE")

    def test_hit_answers_once_without_tools(self):
        r = Runner()
        self.assertIn("知识库", r.run("WMS 库存不足"))
        self.assertEqual(r.count("retrieval"), 1)
        self.assertEqual(r.count("tool"), 0)
        self.assertTrue(r.state["answered"])
        self.assertEqual(r.state["metrics"]["query_turns"], 1)
        self.assertEqual(r.state["metrics"]["retrieval_calls"], 1)

    def test_empty_then_empty_is_no_hit_not_ticket(self):
        r = Runner()
        reply = r.run("WMS 库存不足", retrievals=[[], []], judgments=[judgment("NONE"), judgment("NONE")])
        self.assertIn("暂未在知识库", reply)
        self.assertEqual(r.count("retrieval"), 2)
        self.assertEqual(r.state["mode"], "query")
        self.assertEqual(r.count("tool"), 0)

    def test_second_query_finds_evidence(self):
        r = Runner()
        r.run("WMS 库存不足", retrievals=[[], [CHUNK]], judgments=[judgment("NONE"), judgment()])
        self.assertEqual(r.count("retrieval"), 2)
        self.assertTrue(r.state["answered"])

    def test_unrelated_chunks_are_not_answered(self):
        r = Runner()
        reply = r.run("WMS 库存不足", retrievals=[[CHUNK], [CHUNK]], judgments=[judgment("NONE"), judgment("NONE")])
        self.assertIn("暂未", reply)
        self.assertFalse(r.state["answered"])

    def test_partial_evidence_can_ask_clarification(self):
        r = Runner()
        answer = "资料说明锁定库存会影响可用量。当前是否有未完成出库单？也可以提交工单。"
        self.assertEqual(r.run("WMS 库存不足", judgments=[judgment("PARTIAL")], answer=answer), answer)
        self.assertEqual(r.count("retrieval"), 1)

    def test_bad_assessment_never_becomes_answer_or_ticket(self):
        for bad in ({}, judgment(evidence_ids=[999]), judgment(status="CAN_ANSWER")):
            r = Runner()
            reply = r.run("WMS 库存不足", judgments=[bad])
            self.assertIn("可靠整理", reply)
            self.assertEqual(r.count("tool"), 0)

    def test_invalid_intent_safe_reply(self):
        r = Runner()
        self.assertIn("还需要补充", r.run("确认提交", event_data={"intent": "CONFIRM"}))
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.count("retrieval"), 0)

    def test_retrieval_failure_is_not_no_hit(self):
        r = Runner()
        # This platform has no retrieval failure outlet. Its native error is
        # surfaced; do not simulate unsupported friendly fallback as passing.
        with self.assertRaises(NodeFailure):
            r.run("WMS 库存不足", fail={"知识库检索"})
        self.assertEqual(r.reply, "")
        self.assertEqual(r.count("tool"), 0)

    def test_unsupported_retrieval_branch_reproduces_empty_reply(self):
        r = Runner()
        r.nodes = copy.deepcopy(r.nodes)
        for node in r.nodes.values():
            if node['data']['type'] == 'knowledge-retrieval':
                node['data']['error_strategy'] = 'fail-branch'
        self.assertEqual(r.run('WMS 库存不足'), '')
        self.assertEqual(r.count('retrieval'), 1)
        self.assertEqual(r.count('llm'), 1)

    def test_model_failures_have_user_reply(self):
        for title in ("理解问题与本轮意图", "判断证据是否支持回答", "依据知识库给出处理建议"):
            with self.subTest(title=title):
                r = Runner()
                self.assertTrue(r.run("WMS 库存不足", fail={title}))
                self.assertEqual(r.count("tool"), 0)

    def test_two_followups_then_offer_ticket(self):
        r = Runner()
        r.run("WMS 库存不足")
        for i in range(2):
            r.run("还是不行", event("FOLLOW_UP", attempted=["核对库存"]))
            self.assertEqual(r.count("retrieval"), 1)
        reply = r.run("还是不行", event("FOLLOW_UP"))
        self.assertIn("没有足够的新依据", reply)
        self.assertEqual(r.count("retrieval"), 0)
        self.assertEqual(r.count("tool"), 0)

    def test_thanks_preserves_problem(self):
        r = Runner()
        r.run("WMS 库存不足")
        old = r.state["problem"]
        r.run("谢谢", event("THANKS", problem=""))
        self.assertEqual(r.state["problem"], old)
        self.assertEqual(r.count("retrieval"), 0)

    def test_vague_problem_does_not_search(self):
        r = Runner()
        self.assertIn("请补充", r.run("系统坏了", event(sufficient=False, problem="系统坏了")))
        self.assertEqual(r.count("retrieval"), 0)

    def test_picture_only_requests_text(self):
        r = Runner()
        reply = r.run("", event(sufficient=False, problem="", query=""), files=[IMAGE])
        self.assertIn("无法直接读取", reply)
        self.assertEqual(len(r.conversation["pending_files"]), 1)
        self.assertEqual(r.count("tool"), 0)

    def test_picture_only_with_other_classification_still_requests_text(self):
        r = Runner()
        reply = r.run("", event("OTHER", sufficient=False, problem="", query=""), files=[IMAGE])
        self.assertIn("无法直接读取", reply)

    def test_text_with_picture_queries_without_uploading(self):
        r = Runner()
        r.run("WMS 库存不足", files=[IMAGE])
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(len(r.conversation["pending_files"]), 1)


class TicketTests(unittest.TestCase):
    def test_factory_short_reply_corrects_live_code_misclassification(self):
        issue = "SAP系统报错：短缺转移中的SL库存"
        for query in ("8061 WMS", "WMS，8061", "8061/wms"):
            r = Runner()
            r.run("帮我提交工单", event("CREATE_TICKET", problem=issue))
            reply = r.run(query, event("TICKET_UPDATE", problem=issue + "，涉及WMS系统8061",
                                      fields={"code": "8061", "serviceCode": "WMS"}))
            self.assertIn("百霖（8061）", reply)
            self.assertIn("系统：WMS", reply)
            self.assertEqual(r.state["problem"], issue)
            self.assertEqual(r.state["draft"]["description"], issue)
            self.assertNotIn("code", r.state["draft"])
            self.assertEqual(r.count("retrieval"), 0)
            self.assertEqual(r.count("tool"), 0)

    def test_bare_factory_reply_fills_missing_factory(self):
        r = Runner()
        r.run("帮我提交工单", event("CREATE_TICKET", fields={"serviceCode": "WMS"}))
        reply = r.run("8061", event("TICKET_UPDATE", fields={"code": "8061"}))
        self.assertIn("百霖（8061）", reply)
        self.assertIn("请确认", reply)
        self.assertEqual(r.count("tool"), 0)

    def test_factory_then_service_short_reply_asks_only_missing(self):
        r = Runner()
        r.run("帮我提交工单", event("CREATE_TICKET"))
        reply = r.run("8061", {})
        self.assertNotIn("工厂：请提供", reply)
        self.assertIn("工单所属系统", reply)
        reply = r.run("wms", event("OTHER", problem="无关内容"))
        self.assertIn("百霖（8061）", reply)
        self.assertIn("请确认", reply)
        self.assertEqual(r.state["problem"], "WMS 提示库存不足")

    def test_short_fields_require_active_ticket_and_unambiguous_whole_message(self):
        s = rt.initial_state()
        self.assertEqual(rt.compact_ticket_fields("8061 WMS", s), {})
        s["mode"] = "ticket"
        for text in ("错误码8061", "异常代码：8061", "8061是什么", "库存8061 WMS",
                     "不是8061", "8061 1001 WMS", "8061 WMS MES", "9999 WMS",
                     "取消工单8061", "工厂不要改为8061", "追踪ID：8061", "8061 WMS 确认提交"):
            self.assertEqual(rt.compact_ticket_fields(text, s), {}, text)

    def test_labeled_factory_edit_invalidates_previous_confirmation(self):
        r, _ = self.ready()
        old_preview = r.state["preview"]
        reply = r.run("工厂代码：8061", {})
        self.assertIn("百霖（8061）", reply)
        self.assertNotEqual(r.state["preview"], old_preview)
        self.assertIn("确认提交", reply)
        self.assertEqual(r.count("tool"), 0)

    def test_bare_number_does_not_change_existing_factory(self):
        s = rt.initial_state()
        s.update(mode="await_confirm", draft={"factory": "1001", "serviceCode": "WMS"})
        self.assertEqual(rt.compact_ticket_fields("8061", s), {})
        self.assertEqual(rt.compact_ticket_fields("8061 WMS", s), {"factory": "8061", "serviceCode": "WMS"})
        s["draft"]["factory"] = "大客户事业体"
        self.assertEqual(rt.compact_ticket_fields("8061", s), {"factory": "8061"})

    def test_live_boolean_string_regression_preserves_problem(self):
        r = Runner()
        problem = "SAP 报错：没有为公司代码xxxx和xxxx间的公司代码清帐维护科目"
        r.run(problem, event(problem=problem))
        reply = r.run("帮我提交一个工单，处理这个问题", event("CREATE_TICKET", new_problem="false", sufficient="true", problem=problem))
        self.assertIn("工厂", reply)
        self.assertIn("工单所属系统", reply)
        self.assertIn(problem, reply)
        self.assertNotIn("没有理解", reply)
        self.assertEqual(r.count("retrieval"), 0)
        self.assertEqual(r.count("tool"), 0)
        self.assertEqual(r.state["draft"]["description"], problem)

    def test_literal_boolean_normalization_without_truthiness(self):
        data = rt.normalize_intent(event(new_problem="false", sufficient="true"))
        self.assertIs(data["new_problem"], False)
        self.assertIs(data["sufficient"], True)
        for value in ("yes", "1", 1, [], None):
            self.assertFalse(rt.valid_intent(rt.normalize_intent(event(new_problem=value))))

    def test_explicit_request_works_even_if_model_output_is_invalid(self):
        for query in ("帮我提交一个工单，处理这个问题", "请帮我创建一张工单", "麻烦帮我建个工单", "帮我建单", "提交工单"):
            r = Runner()
            r.run("WMS 库存不足")
            reply = r.run(query, {})
            self.assertIn("工厂", reply)
            self.assertIn("WMS 提示库存不足", reply)
            self.assertEqual(r.state["mode"], "ticket")
            self.assertEqual(r.count("tool"), 0)
            self.assertEqual(r.count("retrieval"), 0)

    def test_ticket_mentions_and_negations_do_not_trigger_fallback(self):
        for query in ("不要帮我提交工单", "提交工单是什么意思", "怎么提交工单", "知识库说‘提交工单’", "还是不行"):
            r = Runner()
            r.run("WMS 库存不足")
            r.run(query, {})
            self.assertEqual(r.state["mode"], "query")
            self.assertEqual(r.count("tool"), 0)

    def test_failed_ticket_update_asks_for_missing_fields(self):
        r = Runner()
        r.run("提交工单", event("CREATE_TICKET", fields={"serviceCode": "WMS"}))
        reply = r.run("补充一些信息", {})
        self.assertIn("工厂", reply)
        self.assertNotIn("工单所属系统", reply)
        self.assertNotIn("补充完整报错", reply)
        self.assertEqual(r.count("retrieval"), 0)

    def test_failed_modification_blocks_stale_confirmation(self):
        r, _ = self.ready()
        r.run("工厂需要改一下", {})
        self.assertEqual(r.state["preview"], "")
        reply = self.confirm(r)
        self.assertIn("上一条补充", reply)
        self.assertEqual(r.count("tool"), 0)
        r.run("工厂：1001", event("TICKET_UPDATE", fields={"factory": "1001"}))
        self.confirm(r)
        self.assertEqual(len(r.create_payloads), 1)

    def test_cancel_works_with_invalid_model_output(self):
        r, _ = self.ready()
        reply = r.run("取消工单", {})
        self.assertIn("已取消", reply)
        self.assertEqual(r.state["draft"], {})
        self.assertEqual(r.count("tool"), 0)

    def ready(self, runner=None, **kw):
        r = runner or Runner()
        reply = r.run("WMS 库存不足，1001工厂，提交工单", event("CREATE_TICKET", fields={"factory": "1001", "serviceCode": "WMS"}), **kw)
        return r, reply

    def confirm(self, r, **kw):
        return r.run("确认提交", event("CONFIRM", problem="", query=""), **kw)

    def test_explicit_ticket_skips_knowledge(self):
        r, reply = self.ready()
        self.assertIn("请确认", reply)
        self.assertEqual(r.count("retrieval"), 0)
        self.assertEqual(r.count("tool"), 0)

    def test_preview_and_confirm_create_with_correct_author(self):
        r, preview = self.ready()
        trace = r.state["draft"]["traceId"]
        self.assertRegex(trace, "^[0-9a-f]{16}$")
        reply = self.confirm(r)
        self.assertIn("TEST-001", reply)
        payload = r.create_payloads[0]
        self.assertEqual(payload["traceId"], trace)
        self.assertEqual(payload["createAuthor"], "employee-test")
        self.assertEqual(payload["updateAuthor"], "employee-test")
        self.assertEqual(payload["file"], "[]")

    def test_repeat_confirmation_never_duplicates(self):
        r, _ = self.ready()
        self.confirm(r)
        self.confirm(r)
        self.assertEqual(len(r.create_payloads), 1)

    def test_confirm_before_preview_only_shows_preview(self):
        r = Runner()
        r.conversation["state"] = rt.dump({**rt.initial_state(), "problem": "库存不足", "draft": {"factory": "1001", "serviceCode": "WMS"}})
        self.assertIn("请确认", self.confirm(r))
        self.assertEqual(len(r.create_payloads), 0)

    def test_modification_requires_new_confirmation(self):
        r, _ = self.ready()
        trace = r.state["draft"]["traceId"]
        reply = r.run("确认提交，系统改成 MES", event("CONFIRM", fields={"serviceCode": "MES"}))
        self.assertIn("系统：MES", reply)
        self.assertEqual(len(r.create_payloads), 0)
        self.confirm(r)
        self.assertEqual(r.create_payloads[0]["serviceCode"], "MES")
        self.assertEqual(r.create_payloads[0]["traceId"], trace)

    def test_cannot_use_generic_yes_to_submit(self):
        r, _ = self.ready()
        reply = r.run("好的", event("CONFIRM"))
        self.assertIn("请确认", reply)
        self.assertEqual(len(r.create_payloads), 0)

    def test_author_change_invalidates_confirmation(self):
        r, _ = self.ready()
        reply = self.confirm(r, salary="another-employee")
        self.assertIn("请确认", reply)
        self.assertEqual(len(r.create_payloads), 0)

    def test_missing_identity_retains_draft(self):
        r, reply = self.ready(salary="")
        self.assertIn("工号", reply)
        self.assertEqual(r.state["draft"]["factory"], "1001")
        self.assertEqual(len(r.create_payloads), 0)

    def test_upstream_identity_rejects_missing_placeholders_and_invalid_values(self):
        for value in (None, "", "  ", "null", "NULL", "undefined", "None", 123, True, [], {}, "ab cd", "ab\ncd", "ab\x00cd", "x" * 129):
            with self.subTest(value=value):
                r, _ = self.ready()
                reply = self.confirm(r, salary=value)
                self.assertIn("工号", reply)
                self.assertIn("工单信息已保留", reply)
                self.assertEqual(r.state["preview"], "")
                self.assertEqual(r.state["draft"]["factory"], "1001")
                self.assertEqual(len(r.create_payloads), 0)

    def test_upstream_identity_preserves_leading_zeros_and_trims_outer_spaces(self):
        r, _ = self.ready(salary="  001234  ")
        self.confirm(r, salary="001234")
        self.assertEqual(r.create_payloads[0]["createAuthor"], "001234")
        self.assertEqual(r.create_payloads[0]["updateAuthor"], "001234")

    def test_chat_text_and_model_fields_cannot_override_upstream_identity(self):
        r, _ = self.ready(salary="upstream-user")
        reply = r.run("我的薪号是 other-user", event("TICKET_UPDATE", fields={"salary_id": "other-user", "createAuthor": "other-user"}), salary="upstream-user")
        self.assertEqual(len(r.create_payloads), 0)
        r.run("工厂1001，WMS", event("TICKET_UPDATE", fields={"factory": "1001", "serviceCode": "WMS"}), salary="upstream-user")
        self.confirm(r, salary="upstream-user")
        self.assertEqual(r.create_payloads[0]["createAuthor"], "upstream-user")

    def test_knowledge_query_without_identity_remains_available(self):
        r = Runner()
        reply = r.run("WMS 库存不足", salary=None)
        self.assertIn("根据知识库", reply)
        self.assertEqual(r.count("tool"), 0)

    def test_factory_ambiguity_has_candidates(self):
        r = Runner()
        reply = r.run("大客户事业体 WMS 提交工单", event("CREATE_TICKET", fields={"factory": "大客户事业体", "serviceCode": "WMS"}))
        self.assertIn("多个编号", reply)
        self.assertIn("1001", reply)
        self.assertIn("1008", reply)

    def test_fill_missing_fields_without_search(self):
        r = Runner()
        r.run("提交工单", event("CREATE_TICKET"))
        reply = r.run("1001，WMS", event("TICKET_UPDATE", fields={"factory": "1001", "serviceCode": "WMS"}))
        self.assertIn("请确认", reply)
        self.assertEqual(r.count("retrieval"), 0)

    def test_cancel_exits_ticket_mode(self):
        r, _ = self.ready()
        reply = r.run("取消工单", event("CANCEL"))
        self.assertIn("已取消", reply)
        self.assertEqual(r.state["draft"], {})
        self.assertEqual(r.state["mode"], "query")
        self.confirm(r)
        self.assertEqual(len(r.create_payloads), 0)

    def test_back_to_query_keeps_draft(self):
        r, _ = self.ready()
        r.run("继续排查", event("FOLLOW_UP"))
        self.assertEqual(r.state["mode"], "query")
        self.assertEqual(r.state["draft"]["factory"], "1001")
        self.assertEqual(r.count("retrieval"), 1)

    def test_new_problem_does_not_reuse_old_fields_or_images(self):
        r, _ = self.ready(files=[IMAGE])
        r.run("换个问题，ELM 页面打不开", event(new_problem=True, problem="ELM 页面打不开", query="ELM 页面打不开"))
        self.assertEqual(r.state["draft"], {"description": "ELM 页面打不开"})
        self.assertEqual(r.state["uploads"], {})
        self.assertEqual(r.conversation["pending_files"], [])

    def test_submit_failure_retains_draft_and_needs_preview_again(self):
        r, _ = self.ready()
        self.assertIn("提交失败", self.confirm(r, create_body=[{"success": False}]))
        self.assertTrue(r.state["draft"])
        self.assertIn("请确认", self.confirm(r))
        self.assertEqual(len(r.create_payloads), 1)

    def test_unknown_response_is_not_success_and_blocks_retry(self):
        r, _ = self.ready()
        reply = self.confirm(r, create_body=[{"message": "success", "data": {"id": "123"}}])
        self.assertIn("待核实", reply)
        self.assertEqual(r.state["submission"]["status"], "unknown")
        self.confirm(r)
        self.assertEqual(len(r.create_payloads), 1)

    def test_transport_error_blocks_retry(self):
        r, _ = self.ready()
        reply = self.confirm(r, fail={"创建 ITSM 工单 · 禁止自动重试"})
        self.assertIn("待核实", reply)
        self.assertIn(r.state["submission"]["status"], ("unknown", "pending"))
        self.confirm(r)
        self.assertEqual(r.count("tool"), 0)

    def test_unverified_contract_returns_actual_response_and_blocks_duplicate(self):
        contract = copy.deepcopy(build.CONTRACT)
        contract["verified"] = False
        r, _ = self.ready(Runner(contract))
        reply = self.confirm(r, create_body=[{"code": 403, "msg": "当前用户无权创建该工厂工单"}])
        self.assertIn("当前用户无权创建该工厂工单", reply)
        self.assertIn("403", reply)
        self.assertNotIn("尚未完成接入验证", reply)
        self.assertEqual(len(r.create_payloads), 1)
        self.assertEqual(r.state["submission"]["status"], "unknown")
        again = self.confirm(r)
        self.assertIn("当前用户无权创建该工厂工单", again)
        self.assertEqual(len(r.create_payloads), 1)

    def test_business_error_details_reach_user_without_llm_rewrite(self):
        r, _ = self.ready()
        reply = self.confirm(r, create_body=[{"success": False, "code": "E_FACTORY", "message": "工厂1002未配置工单处理人"}])
        self.assertIn("工厂1002未配置工单处理人", reply)
        self.assertIn("E_FACTORY", reply)
        self.assertEqual(r.state["submission"]["status"], "failed")
        self.assertTrue(r.state["draft"])
        self.assertIn("请确认", self.confirm(r))
        self.assertEqual(len(r.create_payloads), 1)

    def test_tool_exception_is_wired_and_retained_on_repeated_confirmation(self):
        r, _ = self.ready()
        reply = self.confirm(r, fail={"创建 ITSM 工单 · 禁止自动重试"},
                             error_type="ToolInvokeError", error_message='HTTP 500: {"message":"处理人配置缺失"}')
        self.assertIn("HTTP 500", reply)
        self.assertIn("处理人配置缺失", reply)
        self.assertIn("ToolInvokeError", reply)
        self.assertIn("待核实", reply)
        self.assertIn("处理人配置缺失", self.confirm(r))
        self.assertEqual(len(r.create_payloads), 0)

    def test_timeout_reports_real_cause_without_claiming_definite_failure(self):
        r, _ = self.ready()
        reply = self.confirm(r, fail={"创建 ITSM 工单 · 禁止自动重试"},
                             error_type="ReadTimeout", error_message="Read timed out after 30 seconds")
        self.assertIn("Read timed out after 30 seconds", reply)
        self.assertIn("不会自动重试", reply)
        self.assertNotIn("工单提交失败", reply)

    def test_empty_response_is_explicit_not_invented_error(self):
        r, _ = self.ready()
        reply = self.confirm(r, create_body=[])
        self.assertIn("未收到可读取的接口返回", reply)
        self.assertEqual(r.state["submission"]["status"], "unknown")

    def test_wrong_mapping_still_displays_raw_response(self):
        spec = {"source": "json", "body_path": "missing"}
        result = rt.response_feedback('{"msg":"服务暂不可用"}', [], spec, "", "")
        self.assertIn("服务暂不可用", result)

    def test_plain_text_response_and_nested_error_remain_visible(self):
        self.assertIn("网关维护中", rt.response_feedback("网关维护中", [], {}, "", ""))
        result = rt.response_feedback("", [{"data": {"errors": [{"field": "factoryCode", "detail": "工厂不存在"}]}}],
                                      {"source": "json", "body_path": "0"}, "", "")
        self.assertIn("工厂不存在", result)
        self.assertIn("factoryCode", result)

    def test_error_output_redacts_credentials_and_quotes_markup(self):
        raw = {"message": "凭据失效", "access_token": "do-not-display", "data": {"password": "secret-pass"}}
        text = rt.response_feedback("", [raw], {"source": "json", "body_path": "0"},
                                    'HTTP 401 Authorization: Bearer abc123 password="secret words"', "AuthError")
        for hidden in ("do-not-display", "secret-pass", "abc123", "secret words"):
            self.assertNotIn(hidden, text)
        self.assertIn("凭据失效", text)
        quoted = rt.literal_feedback('```\n![x](https://example.invalid/pixel)')
        self.assertTrue(quoted.startswith("````text\n"))

    def test_long_response_is_bounded_and_truncation_declared(self):
        result = rt.response_feedback("错误" * 3000, [], {}, "", "")
        self.assertIn("已截断", result)
        self.assertLess(len(result), 4100)

    def test_success_without_ticket_number_does_not_invent_number(self):
        r, _ = self.ready()
        reply = self.confirm(r, create_body=[{"success": True}])
        self.assertEqual(reply, "工单已提交成功。")
        self.assertNotIn("TEST-001", reply)

    def test_actual_itsm_success_response_is_simple_and_not_resubmitted(self):
        r, _ = self.ready(Runner(build.CONTRACT))
        reply = self.confirm(r, create_body=[{"status": 200, "message": "操作成功", "body": True,
                                            "timestamp": 1792485262116}])
        self.assertEqual(reply, "工单已提交成功。")
        self.assertEqual(r.state["submission"]["status"], "success")
        self.assertEqual(r.state["metrics"]["ticket_successes"], 1)
        self.assertEqual(r.state["draft"], {})
        self.assertEqual(r.state["uploads"], {})
        self.assertEqual(r.conversation["pending_files"], [])
        self.assertIn("已提交成功", self.confirm(r))
        self.assertEqual(len(r.create_payloads), 1)

    def test_itsm_success_requires_both_exact_status_and_boolean_body(self):
        for body in ({"status": 200}, {"body": True}, {"status": "200", "body": True},
                     {"status": 500, "body": True}, {"status": 200, "body": "true"},
                     {"status": 200, "body": 1}, {"message": "操作成功"}):
            r, _ = self.ready(Runner(build.CONTRACT))
            reply = self.confirm(r, create_body=[body])
            self.assertEqual(r.state["submission"]["status"], "unknown", body)
            self.assertNotEqual(reply, "工单已提交成功。")

    def test_itsm_false_body_keeps_error_and_draft(self):
        r, _ = self.ready(Runner(build.CONTRACT))
        reply = self.confirm(r, create_body=[{"status": 200, "message": "工厂未配置处理人", "body": False}])
        self.assertEqual(r.state["submission"]["status"], "failed")
        self.assertIn("工厂未配置处理人", reply)
        self.assertTrue(r.state["draft"])

    def test_itsm_transport_error_never_becomes_success(self):
        s = rt.initial_state()
        s["submission"] = {"status": "pending", "traceId": "test"}
        result = rt.submitted(rt.dump(s), "", [{"status": 200, "body": True}], rt.dump(build.CONTRACT),
                              True, "Read timed out", "TimeoutError")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("Read timed out", result["reply"])

    def test_malformed_success_checks_fail_closed(self):
        for checks in (None, {}, [{"path": "", "values": [200]}], [{"path": "status", "values": []}]):
            c = copy.deepcopy(build.CONTRACT)
            c["create"]["success_checks"] = checks
            self.assertFalse(rt.create_contract_ready(rt.dump(c)))

    def test_business_code_not_used_as_traceid(self):
        r = Runner()
        r.run("提交工单，异常代码6000，工厂1001，系统WMS", event("CREATE_TICKET", fields={"factory": "1001", "serviceCode": "WMS", "code": "6000"}))
        self.confirm(r)
        self.assertEqual(r.create_payloads[0]["code"], 6000)
        self.assertNotEqual(r.create_payloads[0]["traceId"], "6000")

    def test_attachment_transfers_to_ticket_without_reupload(self):
        r = Runner()
        r.run("WMS 库存不足", files=[IMAGE])
        r, preview = self.ready(r)
        self.assertIn("截图：1 张", preview)
        self.confirm(r)
        self.assertEqual(r.count("tool"), 1)  # create only; cached attachment skips upload
        self.assertEqual(json.loads(r.create_payloads[0]["file"]), ["https://example.invalid/test.png"])
        self.assertEqual(r.conversation["pending_files"], [])

    def test_upload_failure_retry_preserves_successes(self):
        r = Runner()
        other = {**IMAGE, "id": "second-file", "related_id": "second-file"}
        r, reply = self.ready(r, files=[IMAGE, other], upload_bodies=[
            [{"success": True, "data": {"url": "https://example.invalid/one.png"}}], [{"success": False}]])
        self.assertIn("1 张截图读取或上传失败", reply)
        self.assertEqual(len(r.state["uploads"]), 1)
        reply = r.run("重试上传", event("TICKET_UPDATE"), upload_bodies=[[{"success": True, "data": {"url": "https://example.invalid/two.png"}}]])
        self.assertEqual(r.count("tool"), 1)
        self.assertIn("截图：2 张", reply)

    def test_upload_transport_failure_does_not_break_chat(self):
        r, reply = self.ready(files=[IMAGE], fail={"上传截图"})
        self.assertIn("上传失败", reply)
        self.assertEqual(len(r.conversation["pending_files"]), 1)

    def test_can_omit_attachments_after_failure(self):
        r, _ = self.ready(files=[IMAGE], upload_bodies=[[{"success": False}]])
        reply = r.run("不附截图提交", event("TICKET_UPDATE"))
        self.assertIn("截图：0 张", reply)
        self.assertEqual(r.conversation["pending_files"], [])
        self.assertEqual(r.count("tool"), 0)

    def test_new_attachment_invalidates_preview(self):
        r, _ = self.ready()
        reply = self.confirm(r, files=[IMAGE])
        self.assertIn("请确认", reply)
        self.assertEqual(len(r.create_payloads), 0)

    def test_expired_picture_can_be_replaced(self):
        r, reply = self.ready(files=[IMAGE], fail={"读取已保留的截图"})
        self.assertIn("链接过期", reply)
        self.assertEqual(r.count("tool"), 0)
        replacement = {**IMAGE, "related_id": "replacement"}
        reply = r.run("替换失败截图", event("TICKET_UPDATE"), files=[replacement])
        self.assertIn("截图：1 张", reply)
        self.assertEqual([x["key"] for x in r.conversation["pending_files"]], ["replacement"])

    def test_omit_with_simultaneous_image_does_not_readd_image(self):
        r, _ = self.ready()
        reply = r.run("不附截图提交", event("TICKET_UPDATE"), files=[IMAGE])
        self.assertIn("截图：0 张", reply)
        self.assertEqual(r.conversation["pending_files"], [])
        self.assertEqual(r.count("tool"), 0)

    def test_followup_details_are_in_ticket_description(self):
        r = Runner()
        r.run("WMS 库存不足", event(fields={"description": "WMS 库存不足"}))
        r.run("锁定量也检查过，仍报错", event("FOLLOW_UP", problem="WMS 库存不足，锁定量检查后仍报错", attempted=["核对锁定量"]))
        self.ready(r)
        # The ticket request's classifier must retain the full accumulated problem.
        r.run("锁定量已检查仍报错", event("TICKET_UPDATE", problem="WMS 库存不足，锁定量检查后仍报错"))
        self.confirm(r)
        self.assertIn("锁定量检查后仍报错", r.create_payloads[0]["description"])
        self.assertIn("已尝试", r.create_payloads[0]["description"])

    def test_explicit_description_edit_changes_confirmed_payload(self):
        r, _ = self.ready()
        reply = r.run("确认提交，内容改为无法出库", event("CONFIRM", fields={"description": "无法出库"}))
        self.assertIn("无法出库", reply)
        self.assertEqual(len(r.create_payloads), 0)
        self.confirm(r)
        self.assertEqual(r.create_payloads[0]["description"], "无法出库")

    def test_new_problem_cannot_reuse_submitted_trace(self):
        r, _ = self.ready()
        trace = r.state["draft"]["traceId"]
        self.confirm(r)
        reply = r.run("新问题，提交工单", event("CREATE_TICKET", new_problem=True, fields={"factory": "1001", "serviceCode": "WMS", "traceId": trace}))
        self.assertIn("已有成功或待核实", reply)
        self.assertEqual(len(r.create_payloads), 1)

    def test_resolution_clears_current_problem_and_attachments(self):
        r, _ = self.ready(files=[IMAGE])
        r.run("解决了", event("RESOLVED"))
        self.assertEqual(r.state["problem"], "")
        self.assertEqual(r.state["draft"], {})
        self.assertEqual(r.conversation["pending_files"], [])


class ResponseContractTests(unittest.TestCase):
    def test_bool_and_numeric_success_codes_not_confused(self):
        self.assertFalse(rt.exact_match(1, [True]))
        self.assertFalse(rt.exact_match("200", [200]))

    def test_conflicting_failure_overrides_success(self):
        c = copy.deepcopy(VERIFIED_CONTRACT)
        c["create"].update(failure_path="rejected", failure_values=[True])
        s = rt.initial_state()
        s["submission"] = {"status": "pending", "traceId": "test"}
        result = rt.submitted(rt.dump(s), "", [{"success": True, "rejected": True}], rt.dump(c), False)
        self.assertEqual(result["status"], "failed")

    def test_text_json_contract(self):
        spec = {"source": "text", "body_path": "data"}
        self.assertEqual(rt.response_body('{"data":{"ok":true}}', [], spec), {"ok": True})

    def test_invalid_upload_url_not_passed_to_ticket(self):
        result = rt.upload_result("", [{"success": True, "data": {"url": "javascript:alert(1)"}}], "x", rt.dump(VERIFIED_CONTRACT))
        self.assertEqual(rt.obj(result["result"])["url"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
