"""Patch only the attachment path in an exported RunAI draft; never call services."""

import copy
import json
from pathlib import Path

import yaml

from build import bundle
from attachment_file_adapter import single_native_file

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "backups/系统异常助手_附件排查前_20261021.yml"
OUTPUT = ROOT / "系统异常助手_附件上传修复版.yml"
ITERATION = "1791936000450"
CACHED = "1791936000470"
DOWNLOAD = "1791936000490"
DOWNLOAD_CHECK = "1791936000500"
DOWNLOAD_SWITCH = "1791936000510"
UPLOAD = "1791936000520"
PARSE_UPLOAD = "1791936000530"
USE_CACHED = "1791936000540"
JOIN = "1791936000550"
MERGE = "1791936000560"
DOWNLOAD_ERROR = "1792536001010"
UPLOAD_ERROR = "1792536001020"
DOWNLOAD_REJECTED = "1792536001030"


def variable(name, selector):
    return {"variable": name, "value_selector": selector}


def candidate(source=None, upload_verified=False):
    app = copy.deepcopy(source if source is not None else yaml.safe_load(SOURCE.read_text()))
    graph = app["workflow"]["graph"]
    by_id = {n["id"]: n for n in graph["nodes"]}
    for node_id in (DOWNLOAD_ERROR, UPLOAD_ERROR, DOWNLOAD_REJECTED):
        if node_id in by_id:
            raise ValueError("Attachment patch already present; use the original backup as baseline.")

    for node_id in (DOWNLOAD, UPLOAD):
        data = by_id[node_id]["data"]
        data["error_strategy"] = "fail-branch"
        data.pop("default_value", None)

    check = by_id[DOWNLOAD_CHECK]["data"]
    check["variables"] += [variable("key", [CACHED, "key"]), variable("body", [DOWNLOAD, "body"])]
    check["outputs"].update({name: {"type": "string", "children": None} for name in ("feedback", "result")})
    for node_id, function in ((DOWNLOAD_CHECK, "downloaded"), (PARSE_UPLOAD, "upload_result"), (MERGE, "merge_uploads")):
        data = by_id[node_id]["data"]
        data["code"] = bundle(function, [v["variable"] for v in data["variables"]])

    def add_code(node_id, title, position, variables, function=None, literals=None, code=None, outputs=None):
        node = copy.deepcopy(by_id[PARSE_UPLOAD])
        node.update(id=node_id, position={"x": position[0], "y": position[1]}, selected=False)
        origin = by_id[ITERATION]["position"]
        node["positionAbsolute"] = {"x": origin["x"] + position[0], "y": origin["y"] + position[1]}
        node["data"] = {
            "type": "code", "title": title, "desc": "", "code_language": "python3", "selected": False,
            "isInIteration": True, "iteration_id": ITERATION, "variables": variables,
            "outputs": {name: {"type": "string", "children": None} for name in (outputs or ["result"])},
            "code": code if code is not None else bundle(function, [v["variable"] for v in variables], literals),
        }
        graph["nodes"].append(node)
        by_id[node_id] = node

    add_code(DOWNLOAD_ERROR, "记录截图读取异常", (1040, 440), [
        variable("key", [CACHED, "key"]), variable("error_message", [DOWNLOAD, "error_message"]),
        variable("error_type", [DOWNLOAD, "error_type"]),
    ], "downloaded", {"status_code": 0, "files": []}, outputs=["ready", "feedback", "result"])
    add_code(UPLOAD_ERROR, "记录截图上传异常", (1990, 440), [
        variable("key", [CACHED, "key"]), variable("contract", ["env", "itsm_response_contract"]),
        variable("error_message", [UPLOAD, "error_message"]), variable("error_type", [UPLOAD, "error_type"]),
    ], "upload_result", {"text": "", "structured": [], "transport_error": True})
    add_code(DOWNLOAD_REJECTED, "记录截图读取失败", (1670, 50), [variable("result", [DOWNLOAD_CHECK, "result"])],
             code="def main(result):\n    return {'result': result}\n")

    # Only a selected branch publishes its aggregate value. In particular,
    # DOWNLOAD_CHECK executes on both success and failure and must not be a
    # direct aggregate input, or its empty result could shadow a real upload.
    by_id[USE_CACHED]["data"]["title"] = "复用已上传附件地址"
    by_id[JOIN]["data"]["variables"] += [[DOWNLOAD_ERROR, "result"], [UPLOAD_ERROR, "result"], [DOWNLOAD_REJECTED, "result"]]

    edges = graph["edges"]
    for edge in edges:
        if edge["source"] in (DOWNLOAD, UPLOAD):
            edge["sourceHandle"] = "success-branch"
            edge["id"] = f"{edge['source']}-success-branch-{edge['target']}-target"
        if edge["source"] == DOWNLOAD_SWITCH and edge["sourceHandle"] == "false":
            edge["target"] = DOWNLOAD_REJECTED
            edge["id"] = f"{DOWNLOAD_SWITCH}-false-{DOWNLOAD_REJECTED}-target"

    def edge(source, target, handle="source"):
        edges.append({
            "id": f"{source}-{handle}-{target}-target", "source": source, "sourceHandle": handle,
            "target": target, "targetHandle": "target", "type": "custom", "zIndex": 1002,
            "data": {"isInIteration": True, "isInLoop": False, "iteration_id": ITERATION,
                     "sourceType": by_id[source]["data"]["type"], "targetType": by_id[target]["data"]["type"]},
        })

    edge(DOWNLOAD, DOWNLOAD_ERROR, "fail-branch")
    edge(UPLOAD, UPLOAD_ERROR, "fail-branch")
    for node_id in (DOWNLOAD_ERROR, UPLOAD_ERROR, DOWNLOAD_REJECTED):
        edge(node_id, JOIN)

    contract_env = next(v for v in app["workflow"]["environment_variables"] if v["name"] == "itsm_response_contract")
    contract = json.loads(contract_env["value"])
    # Confirmed tool schema: successful response has status=200 and body=URL.
    # Do not enable uploads until an actual tool invocation validates it.
    contract["upload"] = {"verified": upload_verified is True, "source": "json", "body_path": "0",
                          "url_path": "body", "success_path": "status", "success_values": [200]}
    contract_env["value"] = json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
    return single_native_file(app)


def minimal_patch(source, target):
    """Reviewable diff against the exact exported baseline; not a platform API."""
    old_graph, new_graph = source["workflow"]["graph"], target["workflow"]["graph"]
    old_nodes = {n["id"]: n for n in old_graph["nodes"]}
    new_nodes = {n["id"]: n for n in new_graph["nodes"]}
    if set(old_nodes) - set(new_nodes):
        raise ValueError("Attachment patch must not remove existing nodes.")
    changed = []
    for node_id in set(old_nodes) & set(new_nodes):
        old_node, new_node = old_nodes[node_id], new_nodes[node_id]
        if {k: v for k, v in old_node.items() if k != "data"} != {k: v for k, v in new_node.items() if k != "data"}:
            raise ValueError("Existing-node layout or metadata changed unexpectedly: " + node_id)
        if old_node["data"] != new_node["data"]:
            changed.append({"id": node_id, "data": new_node["data"]})
    old_edges = {e["id"]: e for e in old_graph["edges"]}
    new_edges = {e["id"]: e for e in new_graph["edges"]}
    old_env = {v["id"]: v for v in source["workflow"]["environment_variables"]}
    new_env = {v["id"]: v for v in target["workflow"]["environment_variables"]}
    return {
        "baseline": str(SOURCE), "output": str(OUTPUT),
        "instructions": "仅供核对和通过平台编辑界面应用，不是平台API。上传verified仍为false，待真实上传验证。",
        "updated_node_data": sorted(changed, key=lambda n: n["id"]),
        "new_nodes": [new_nodes[k] for k in sorted(set(new_nodes) - set(old_nodes))],
        "edges": {
            "remove_ids": sorted(set(old_edges) - set(new_edges)),
            "add": [new_edges[k] for k in sorted(set(new_edges) - set(old_edges))],
            "replace": [new_edges[k] for k in sorted(set(new_edges) & set(old_edges)) if new_edges[k] != old_edges[k]],
        },
        "updated_environment_variables": [new_env[k] for k in sorted(new_env) if new_env[k] != old_env.get(k)],
    }


def write_candidate():
    app = candidate()

    class Dumper(yaml.SafeDumper):
        pass

    Dumper.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))
    OUTPUT.write_text(yaml.dump(app, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=120))
    changed = {DOWNLOAD_CHECK, PARSE_UPLOAD, MERGE, DOWNLOAD_ERROR, UPLOAD_ERROR, DOWNLOAD_REJECTED}
    for node in app["workflow"]["graph"]["nodes"]:
        if node["id"] in changed:
            (ROOT / "verification" / ("附件修复_" + node["id"] + ".py")).write_text(node["data"]["code"])
    patch = minimal_patch(yaml.safe_load(SOURCE.read_text()), app)
    (ROOT / "verification/附件修复_最小补丁.json").write_text(json.dumps(patch, ensure_ascii=False, indent=2) + "\n")
    return OUTPUT


if __name__ == "__main__":
    print(write_candidate())
