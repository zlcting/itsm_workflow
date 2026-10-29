"""Build the standalone Dify DSL; does not call any external service."""

import ast
import copy
import json
from pathlib import Path
import sys
import uuid

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import prompts
import runtime
from compatibility import native_retrieval_outputs

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "系统异常助手.yml"
OUTPUT = ROOT / "系统异常助手_优化版.yml"
# Confirmed create response supplied from an actual ITSM submission on 2026-10-20.
# Upload response remains unverified and is configured independently.
CONTRACT = {"version": 1, "verified": True,
            "create": {"source": "json", "body_path": "0", "success_path": "body", "success_values": [True],
                       "success_checks": [{"path": "status", "values": [200]}],
                       "failure_path": "body", "failure_values": [False], "ticket_id_path": ""},
            "upload": {"verified": False, "source": "json", "body_path": "0", "url_path": "",
                       "success_path": "", "success_values": []}}


def bundle(function, arguments, literals=None):
    """Include only transitive runtime dependencies, retaining editable Python source."""
    tree = ast.parse((ROOT / "workflow_src/runtime.py").read_text())
    definitions = {}
    for item in tree.body:
        if isinstance(item, ast.FunctionDef):
            definitions[item.name] = item
        elif isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = item
    needed = set()

    def include(name):
        if name not in definitions or name in needed:
            return
        needed.add(name)
        for term in ast.walk(definitions[name]):
            if isinstance(term, ast.Name) and isinstance(term.ctx, ast.Load):
                include(term.id)
    include(function)
    out = ["import json\nimport re\nimport hashlib\nimport uuid\n"]
    for item in tree.body:
        if any(item is definitions[x] for x in needed):
            if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "FACTORIES" for t in item.targets):
                out.append("FACTORIES = " + repr(json.loads((ROOT / "workflow_src/factories.json").read_text())))
            else:
                out.append(ast.unparse(item))
    args = ", ".join(arguments)
    call = ", ".join([f"{key}={key}" for key in arguments] + [f"{key}={value!r}" for key, value in (literals or {}).items()])
    out.append(f"def main({args}):\n    return {function}({call})\n")
    return "\n\n".join(out)


class Graph:
    def __init__(self):
        self.nodes = []
        self.edges = []
        self.lookup = {}

    def add(self, key, title, typ, data=None, pos=(0, 0), parent=None):
        # Stable numeric node IDs retain broad Dify compatibility.
        node_id = str(1791936000000 + len(self.nodes) * 10)
        body = {"title": title, "type": typ, "desc": "", "selected": False, **(data or {})}
        node = {"id": node_id, "data": body, "type": "custom", "position": {"x": pos[0], "y": pos[1]},
                "positionAbsolute": {"x": pos[0], "y": pos[1]}, "width": 244, "height": 100,
                "sourcePosition": "right", "targetPosition": "left", "selected": False}
        if parent:
            node.update(parentId=self.lookup[parent]["id"], extent="parent", zIndex=1002)
            body.update(isInIteration=True, iteration_id=self.lookup[parent]["id"])
        self.nodes.append(node)
        self.lookup[key] = node
        return key

    def sel(self, key, field):
        return [self.lookup[key]["id"] if key in self.lookup else key, field]

    def ref(self, key, field):
        return "{{#" + ".".join(self.sel(key, field)) + "#}}"

    def edge(self, source, target, handle="source"):
        a, b = self.lookup[source], self.lookup[target]
        iteration = a.get("parentId")
        self.edges.append({"id": f"{a['id']}-{handle}-{b['id']}-target", "source": a["id"], "target": b["id"],
                           "sourceHandle": handle, "targetHandle": "target", "type": "custom", "zIndex": 1002 if iteration else 0,
                           "data": {"sourceType": a["data"]["type"], "targetType": b["data"]["type"],
                                    "isInIteration": bool(iteration), "isInLoop": False,
                                    **({"iteration_id": iteration} if iteration else {})}})

    def code(self, key, title, function, variables, outputs, pos, literals=None, parent=None):
        return self.add(key, title, "code", {"code_language": "python3", "code": bundle(function, variables, literals),
                        "variables": [{"variable": name, "value_selector": selector} for name, selector in variables.items()],
                        "outputs": {name: {"type": typ, "children": None} for name, typ in outputs.items()}}, pos, parent)

    def assign(self, key, title, items, pos):
        return self.add(key, title, "assigner", {"version": "2", "items": items}, pos)

    def save(self, key, source, pos):
        self.assign(key, "保存会话状态", [self.write("state", self.sel(source, "state"))], pos)
        self.edge(source, key)

    def write(self, variable, source=None, operation="over-write"):
        # Dify v2 accepts clear only with variable input, although clear has no
        # source value and deliberately skips source-selector resolution.
        input_type = "variable" if operation == "clear" or source else "constant"
        return {"variable_selector": ["conversation", variable], "input_type": input_type,
                "operation": operation, "value": "" if operation == "clear" else (source if source else "")}

    def switch(self, key, title, selector, values, pos, parent=None):
        cases = [{"case_id": x.lower(), "id": x.lower(), "logical_operator": "and", "conditions": [
            {"id": f"{key}-{x}", "variable_selector": selector, "varType": "string", "comparison_operator": "is", "value": x}]} for x in values]
        return self.add(key, title, "if-else", {"cases": cases}, pos, parent)

    def answer(self, key, text, pos):
        return self.add(key, "回复用户", "answer", {"answer": text, "variables": []}, pos)

    def llm(self, key, title, system, user, model, pos, schema=None, context=None):
        data = {"model": copy.deepcopy(model), "context": {"enabled": bool(context), "variable_selector": context or []},
                "vision": {"enabled": False}, "citation_mode": bool(context),
                "prompt_template": [{"id": key + "-system", "role": "system", "text": system},
                                    {"id": key + "-user", "role": "user", "text": user}],
                "variables": [], "retry_config": {"retry_enabled": True, "max_retries": 1, "retry_interval": 1000},
                "error_strategy": "fail-branch"}
        data["model"]["completion_params"]["temperature"] = 0.1
        if schema:
            data.update(structured_output_enabled=True, structured_output={"schema": schema})
        return self.add(key, title, "llm", data, pos)


def build():
    original = yaml.safe_load(SOURCE.read_text())
    old = {n["data"]["title"]: n["data"] for n in original["workflow"]["graph"]["nodes"]}
    model = old["异常问题提取 (1)"]["model"]
    g = Graph()
    S = ["conversation", "state"]
    FILES = ["conversation", "pending_files"]
    CONTRACT_VAR = ["env", "itsm_response_contract"]
    # RunAI exposes sys.salary_id in the system-variable picker (verified 2026-10-21).
    # Keep the original upstream identity channel; a Start input adds a user-editable form.
    g.add("start", "开始 · 接收登录工号", "start", {"variables": []}, (0, 0))
    g.llm("intent", "理解问题与本轮意图", prompts.ROUTER,
          "当前消息：\n{{#sys.query#}}\n当前会话状态：\n{{#conversation.state#}}\n附件（仅元数据，不代表看到了图片）：\n{{#sys.files#}}",
          model, (320, 0), prompts.ROUTER_SCHEMA)
    g.edge("start", "intent")
    g.answer("intent_error", "暂时无法理解这条消息，你之前的信息已保留。请稍后重试，或重新描述问题。", (640, -240))
    g.edge("intent", "intent_error", "fail-branch")
    g.code("route", "更新问题及会话状态", "route", {"raw": g.sel("intent", "structured_output"), "state": S,
           "query": ["sys", "query"], "files": ["sys", "files"], "salary_id": ["sys", "salary_id"]},
           {k: "string" for k in ("state", "route", "reply", "reset_files", "intent")}, (640, 0))
    g.edge("intent", "route", "success-branch")
    g.save("save_route", "route", (960, 0))
    g.code("retain", "保存可跨轮使用的截图引用", "retain_files", {"files": ["sys", "files"], "pending": FILES,
           "state": S, "reset": g.sel("route", "reset_files"), "query": ["sys", "query"]}, {"pending": "array[object]"}, (1280, 0))
    g.edge("save_route", "retain")
    g.assign("append_files", "保留当前问题截图", [g.write("pending_files", g.sel("retain", "pending"))], (1600, 0))
    g.edge("retain", "append_files")
    g.switch("dispatch", "进入查询 / 工单 / 直接回复", g.sel("route", "route"), ["QUERY", "TICKET"], (2240, 0))
    g.edge("append_files", "dispatch")
    g.answer("simple_reply", g.ref("route", "reply"), (2560, -240))
    g.edge("dispatch", "simple_reply", "false")

    # Knowledge branch. At most two serial retrievals; PARTIAL asks for useful context.
    g.code("query_input", "准备干净检索问题", "query_input", {"state": S},
           {k: "string" for k in ("query", "problem", "attempted", "last_answer")}, (2560, 0))
    g.edge("dispatch", "query_input", "query")
    for number, x in ((1, 2880), (2, 4800)):
        k = str(number)
        query_selector = g.sel("query_input", "query") if number == 1 else g.sel("assess1", "alternate")
        retrieval = copy.deepcopy(old["知识检索 (1)"])
        for field in ("title", "type", "selected"):
            retrieval.pop(field, None)
        retrieval.update(query_variable_selector=query_selector, error_strategy="fail-branch",
                         retry_config={"retry_enabled": False, "max_retries": 0, "retry_interval": 0})
        g.add("retrieve" + k, "知识库检索" + ("" if number == 1 else " · 改写后再试一次"), "knowledge-retrieval", retrieval, (x, 0))
        if number == 1:
            g.edge("query_input", "retrieve1")
        else:
            g.edge("assess_switch1", "retrieve2", "retry")
        g.code("retrieval_error" + k, "记录查询服务异常", "query_failure", {"state": S}, {"state": "string", "reply": "string"}, (x + 320, -450))
        g.edge("retrieve" + k, "retrieval_error" + k, "fail-branch")
        g.save("save_retrieval_error" + k, "retrieval_error" + k, (x + 640, -450))
        g.answer("reply_retrieval_error" + k, g.ref("retrieval_error" + k, "reply"), (x + 960, -450))
        g.edge("save_retrieval_error" + k, "reply_retrieval_error" + k)
        user = ("用户原始消息：{{#sys.query#}}\n完整问题：" + g.ref("query_input", "problem")
                + "\n已尝试：" + g.ref("query_input", "attempted") + "\n上一回答：" + g.ref("query_input", "last_answer")
                + "\n这是第%d次检索。以下数组顺序即文档序号（从1开始）：\n" % number + g.ref("retrieve" + k, "result"))
        g.llm("judge" + k, "判断证据是否支持回答", prompts.ASSESS, user, model, (x + 320, 0), prompts.ASSESS_SCHEMA)
        g.code("count_retrieval" + k, "记录检索次数", "retrieved", {"state": S}, {"state": "string"}, (x + 150, -160))
        g.edge("retrieve" + k, "count_retrieval" + k, "success-branch")
        g.save("save_retrieval_count" + k, "count_retrieval" + k, (x + 450, -160))
        g.edge("save_retrieval_count" + k, "judge" + k)
        g.answer("judge_error" + k, "已完成检索，但暂时无法可靠整理结果。你可以稍后重试，或回复“提交工单”。", (x + 640, -240))
        g.edge("judge" + k, "judge_error" + k, "fail-branch")
        g.code("assess" + k, "校验证据与检索结果", "assess", {"raw": g.sel("judge" + k, "structured_output"),
               "chunks": g.sel("retrieve" + k, "result"), "state": S},
               {"state": "string", "route": "string", "alternate": "string", "evidence": "array[object]", "assessment": "string"},
               (x + 640, 0), {"second": number == 2})
        g.edge("judge" + k, "assess" + k, "success-branch")
        g.save("save_assess" + k, "assess" + k, (x + 960, 0))
        values = ["ANSWER", "PARTIAL", "NONE"] + (["RETRY"] if number == 1 else [])
        g.switch("assess_switch" + k, "回答 / 补充 / 未命中", g.sel("assess" + k, "route"), values, (x + 1280, 0))
        g.edge("save_assess" + k, "assess_switch" + k)
        g.answer("no_hit" + k, runtime.NO_HIT, (x + 1600, 200))
        g.edge("assess_switch" + k, "no_hit" + k, "none")
        g.edge("assess_switch" + k, "judge_error" + k, "false")
        g.llm("answer_llm" + k, "依据知识库给出处理建议", prompts.ANSWER,
              "用户当前消息：{{#sys.query#}}\n问题和排查记录：{{#conversation.state#}}\n证据评估：" + g.ref("assess" + k, "assessment") + "\n知识库证据：\n{{#context#}}",
              model, (x + 1600, 450), context=g.sel("assess" + k, "evidence"))
        g.edge("assess_switch" + k, "answer_llm" + k, "answer")
        g.edge("assess_switch" + k, "answer_llm" + k, "partial")
        g.edge("answer_llm" + k, "judge_error" + k, "fail-branch")
        g.code("remember" + k, "记住处理建议", "remember_answer", {"state": S, "text": g.sel("answer_llm" + k, "text")}, {"state": "string"}, (x + 2240, 450))
        g.save("save_answer" + k, "remember" + k, (x + 2560, 450))
        g.answer("knowledge_reply" + k, g.ref("answer_llm" + k, "text"), (x + 1920, 450))
        # Answer connects directly to the LLM so Dify can stream; persist afterward.
        g.edge("answer_llm" + k, "knowledge_reply" + k, "success-branch")
        g.edge("knowledge_reply" + k, "remember" + k)

    # Persist public references as array[object]; HTTP restores native File values.
    g.code("attachments", "检查待上传截图", "attachment_gate", {"state": S, "files": FILES, "contract": CONTRACT_VAR},
           {"route": "string", "reply": "string"}, (2560, 1300))
    g.edge("dispatch", "attachments", "ticket")
    g.switch("attachment_switch", "是否需要上传截图", g.sel("attachments", "route"), ["UPLOAD", "READY"], (2880, 1300))
    g.edge("attachments", "attachment_switch")
    g.answer("attachment_blocked", g.ref("attachments", "reply"), (3200, 1100))
    g.edge("attachment_switch", "attachment_blocked", "false")
    g.add("upload_iter", "上传未处理的截图", "iteration", {"iterator_selector": FILES, "iterator_input_type": "array[object]",
          "output_type": "array[string]", "is_parallel": False, "parallel_nums": 1, "error_handle_mode": "continue-on-error",
          "width": 3300, "height": 600}, (3200, 1800))
    g.lookup["upload_iter"].update(width=3300, height=600)
    g.edge("attachment_switch", "upload_iter", "upload")
    g.add("iter_start", "", "iteration-start", {}, (40, 200), "upload_iter")
    g.lookup["iter_start"].update(type="custom-iteration-start", width=44, height=48)
    g.lookup["upload_iter"]["data"]["start_node_id"] = g.lookup["iter_start"]["id"]
    g.code("cached", "检查截图是否已上传", "upload_cached", {"file": g.sel("upload_iter", "item"), "state": S, "base_url": ["env", "dify_files_base_url"]},
           {"key": "string", "cached": "string", "result": "string", "download_url": "string"}, (130, 200), parent="upload_iter")
    g.edge("iter_start", "cached")
    g.switch("cached_switch", "复用已上传图片", g.sel("cached", "cached"), ["yes"], (430, 200), "upload_iter")
    g.edge("cached", "cached_switch")
    g.add("download", "读取已保留的截图", "http-request", {
        "method": "get", "url": g.ref("cached", "download_url"), "authorization": {"type": "no-auth"},
        "headers": "", "params": "", "body": {"type": "none", "data": []}, "ssl_verify": True,
        "timeout": {"connect": 10, "read": 30, "write": 10},
        "error_strategy": "default-value", "default_value": [
            {"key": "status_code", "type": "number", "value": 0}, {"key": "files", "type": "array[file]", "value": []}],
        "retry_config": {"retry_enabled": False, "max_retries": 0, "retry_interval": 0}}, (730, 280), "upload_iter")
    g.edge("cached_switch", "download", "false")
    g.code("download_check", "检查截图链接和文件", "downloaded", {"status_code": g.sel("download", "status_code"), "files": g.sel("download", "files")},
           {"ready": "string"}, (1040, 280), parent="upload_iter")
    g.edge("download", "download_check")
    g.switch("download_switch", "截图是否可读取", g.sel("download_check", "ready"), ["yes"], (1350, 280), "upload_iter")
    g.edge("download_check", "download_switch")
    upload = copy.deepcopy(old["itomuploadimg_post"])
    for key in ("title", "type", "iteration_id", "isInIteration", "selected"):
        upload.pop(key, None)
    upload["tool_parameters"]["file"]["value"] = g.sel("download", "files")
    upload.update(error_strategy="default-value", default_value=[{"key": "text", "type": "string", "value": ""}, {"key": "json", "type": "array[object]", "value": []}],
                  retry_config={"retry_enabled": False, "max_retries": 0, "retry_interval": 0})
    g.add("upload_tool", "上传截图", "tool", upload, (1670, 280), "upload_iter")
    g.edge("download_switch", "upload_tool", "yes")
    g.code("parse_upload", "读取真实附件地址", "upload_result", {"text": g.sel("upload_tool", "text"), "structured": g.sel("upload_tool", "json"),
           "key": g.sel("cached", "key"), "contract": CONTRACT_VAR}, {"result": "string"}, (1990, 280), parent="upload_iter")
    g.edge("upload_tool", "parse_upload")
    # Cached output must exist ONLY on its own branch so the aggregator cannot select stale input.
    g.add("use_cached", "复用地址或记录链接失效", "code", {"code_language": "python3", "code": "def main(result):\n    return {'result': result}\n",
          "variables": [{"variable": "result", "value_selector": g.sel("cached", "result")}], "outputs": {"result": {"type": "string", "children": None}}}, (1990, 50), "upload_iter")
    g.edge("cached_switch", "use_cached", "yes")
    g.edge("download_switch", "use_cached", "false")
    g.add("upload_join", "汇总单张图片结果", "variable-aggregator", {"output_type": "string", "variables": [g.sel("use_cached", "result"), g.sel("parse_upload", "result")]}, (2320, 180), "upload_iter")
    g.edge("use_cached", "upload_join")
    g.edge("parse_upload", "upload_join")
    g.lookup["upload_iter"]["data"]["output_selector"] = g.sel("upload_join", "output")
    g.code("merge_uploads", "保存上传结果", "merge_uploads", {"results": g.sel("upload_iter", "output"), "state": S},
           {"state": "string", "route": "string", "reply": "string"}, (5500, 1500))
    g.edge("upload_iter", "merge_uploads")
    g.save("save_uploads", "merge_uploads", (5820, 1500))
    g.switch("uploads_done", "截图是否全部就绪", g.sel("merge_uploads", "route"), ["READY"], (6140, 1500))
    g.edge("save_uploads", "uploads_done")
    g.answer("upload_failed", g.ref("merge_uploads", "reply"), (6460, 1700))
    g.edge("uploads_done", "upload_failed", "false")

    g.code("prepare", "校验工单与确认版本", "prepare_ticket", {"state": S, "query": ["sys", "query"],
           "salary_id": ["sys", "salary_id"], "contract": CONTRACT_VAR},
           {**{x: "string" for x in ("state", "route", "reply", "payload", "traceId", "serviceCode", "factoryCode", "description", "author", "file")}, "code": "number"}, (6780, 1300))
    g.edge("attachment_switch", "prepare", "ready")
    g.edge("uploads_done", "prepare", "ready")
    g.save("save_prepare", "prepare", (7100, 1300))
    g.switch("submit_gate", "仅确认过的版本允许提交", g.sel("prepare", "route"), ["SUBMIT"], (7420, 1300))
    g.edge("save_prepare", "submit_gate")
    g.answer("ticket_reply", g.ref("prepare", "reply"), (7740, 1050))
    g.edge("submit_gate", "ticket_reply", "false")
    old_tool = old["提交工单流程"]["agent_parameters"]["tools"]["value"][0]
    tool = {"provider_id": old_tool["provider_name"], "provider_name": old_tool["provider_show_name"], "provider_type": "api",
            "tool_name": old_tool["tool_name"], "tool_label": old_tool["tool_label"], "tool_description": old_tool["tool_description"],
            "tool_node_version": "2", "tool_configurations": {}, "is_team_authorization": True,
            "paramSchemas": old_tool["schemas"], "params": {name: "" for name in old_tool["parameters"]},
            "tool_parameters": {name: {"type": "variable", "value": g.sel("prepare", "author" if name in ("createAuthor", "updateAuthor") else name)} for name in old_tool["parameters"]},
            "error_strategy": "fail-branch", "retry_config": {"retry_enabled": False, "max_retries": 0, "retry_interval": 0}}
    g.add("create_tool", "创建 ITSM 工单 · 禁止自动重试", "tool", tool, (7740, 1300))
    g.edge("submit_gate", "create_tool", "submit")
    g.code("created", "按接口结果判断提交状态", "submitted", {"state": S, "text": g.sel("create_tool", "text"),
           "structured": g.sel("create_tool", "json"), "contract": CONTRACT_VAR},
           {"state": "string", "reply": "string", "status": "string"}, (8060, 1300), {"transport_error": False})
    g.edge("create_tool", "created", "success-branch")
    g.code("create_unknown", "提交异常 · 标记待核实", "submitted", {"state": S, "contract": CONTRACT_VAR,
           "error_message": g.sel("create_tool", "error_message"), "error_type": g.sel("create_tool", "error_type")},
           {"state": "string", "reply": "string", "status": "string"}, (8060, 1650), {"text": "", "structured": [], "transport_error": True})
    g.edge("create_tool", "create_unknown", "fail-branch")
    for name, y in (("created", 1300), ("create_unknown", 1650)):
        g.save("save_" + name, name, (8380, y))
        if name == "created":
            g.switch("created_status", "提交成功后清理附件", g.sel(name, "status"), ["success"], (8700, y))
            g.edge("save_" + name, "created_status")
            g.assign("clear_created_files", "清理已提交附件引用", [g.write("pending_files", operation="clear")], (9020, y - 150))
            g.edge("created_status", "clear_created_files", "success")
            g.answer("reply_" + name, g.ref(name, "reply"), (9340, y))
            g.edge("clear_created_files", "reply_" + name)
            g.edge("created_status", "reply_" + name, "false")
        else:
            g.answer("reply_" + name, g.ref(name, "reply"), (8700, y))
            g.edge("save_" + name, "reply_" + name)

    app = copy.deepcopy(original)
    app["app"].update(name="系统异常助手 · 优化版", description="先查询知识库，按证据回答；用户选择后确认建单。")
    wf = app["workflow"]
    wf["conversation_variables"] = [
        {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "exception-helper/state")), "name": "state", "description": "问题、工单草稿、确认签名、提交状态和计数",
         "selector": S, "value": runtime.dump(runtime.initial_state()), "value_type": "string"},
        {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "exception-helper/files")), "name": "pending_files", "description": "当前问题保留的截图，只有建单时上传",
         "selector": FILES, "value": [], "value_type": "array[object]"}]
    wf["environment_variables"] = [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "exception-helper/contract")), "name": "itsm_response_contract",
                                    "description": "ITSM创建成功需status=200且body=true；失败展示原文，未知返回保留待核实；附件上传映射独立配置。",
                                    "selector": CONTRACT_VAR, "value": runtime.dump(CONTRACT), "value_type": "string"},
                                   {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "exception-helper/files-base")), "name": "dify_files_base_url",
                                    "description": "仅当图片地址为 /files/... 相对路径时填写 Dify 文件服务的 HTTPS 域名，末尾不带斜杠。",
                                    "selector": ["env", "dify_files_base_url"], "value": "", "value_type": "string"}]
    features = wf["features"]
    features["opening_statement"] = "你好，我是系统异常助手。请描述遇到的问题，最好带上系统名称和报错原文，我会先查询知识库。需要人工处理时，也可以直接说“提交工单”。截图可以作为工单附件，请同时提供报错文字。"
    features["suggested_questions"] = ["WMS 提示库存不足，应该怎么排查？", "ELM 提示模具穴数不能为空，是什么原因？", "我想提交异常处理工单"]
    features["suggested_questions_after_answer"]["enabled"] = False
    features["file_upload"]["allowed_file_extensions"] = [".JPG", ".JPEG", ".PNG", ".GIF", ".WEBP"]
    features["file_upload"]["image"]["enabled"] = False
    features["file_upload"]["fileUploadConfig"]["image_file_size_limit"] = 10
    # Keep the exported canvas legible when optional error/counter nodes share a lane.
    placed = []
    for node in g.nodes:
        if node.get("parentId"):
            continue
        while any(node["position"]["x"] < other["position"]["x"] + other["width"] + 24
                  and node["position"]["x"] + node["width"] + 24 > other["position"]["x"]
                  and node["position"]["y"] < other["position"]["y"] + other["height"] + 24
                  and node["position"]["y"] + node["height"] + 24 > other["position"]["y"] for other in placed):
            node["position"]["y"] += 180
        node["positionAbsolute"] = dict(node["position"])
        placed.append(node)
    by_id = {n["id"]: n for n in g.nodes}
    for node in g.nodes:
        if node.get("parentId"):
            parent = by_id[node["parentId"]]
            node["positionAbsolute"] = {axis: parent["position"][axis] + node["position"][axis] for axis in ("x", "y")}
    wf["graph"] = {"nodes": g.nodes, "edges": g.edges, "viewport": {"x": 40, "y": 100, "zoom": 0.45}}
    app = native_retrieval_outputs(app)

    class Dumper(yaml.SafeDumper):
        pass

    def string_representer(dumper, value):
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)
    Dumper.add_representer(str, string_representer)
    OUTPUT.write_text(yaml.dump(app, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=120))
    graph = app['workflow']['graph']
    print(f"Generated {OUTPUT.name}: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")
    return app


if __name__ == "__main__":
    build()
