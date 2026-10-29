"""Upload this turn's native sys.files, then persist only attachment URLs.

This avoids downloading signed file-preview URLs and does not persist File
values into conversation variables. It never invokes an external service.
"""

import copy
import json
from pathlib import Path

import yaml

from build import bundle

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "backups/系统异常助手_附件原生直传前_20261021.yml"
OUTPUT = ROOT / "verification/系统异常助手_附件原生直传候选.yml"
SAVE_GATE = "1792536002010"


def native_candidate(source=None):
    app = copy.deepcopy(source if source is not None else yaml.safe_load(SOURCE.read_text()))
    graph = app["workflow"]["graph"]
    by_id = {n["id"]: n for n in graph["nodes"]}
    if SAVE_GATE in by_id:
        raise ValueError("The native upload patch is already present; use its original input export.")
    required = {"1792536001020", "1791936000420", "1791936000450", "1791936000520"}
    if not required.issubset(by_id):
        raise ValueError("Apply the attachment error-feedback patch before the native upload patch.")
    removed = {"1791936000490", "1791936000500", "1791936000510", "1792536001010", "1792536001030", "1792536001040"}
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] not in removed]
    graph["edges"] = [e for e in graph["edges"] if e["source"] not in removed and e["target"] not in removed]
    for node_id in removed:
        by_id.pop(node_id, None)

    def var(name, selector):
        return {"variable": name, "value_selector": selector}

    def code(node_id, function, variables, outputs):
        data = by_id[node_id]["data"]
        data.update(variables=variables, outputs={key: {"type": "string", "children": None} for key in outputs})
        data["code"] = bundle(function, [v["variable"] for v in variables])

    code("1791936000420", "native_attachment_gate", [
        var("state", ["conversation", "state"]), var("files", ["sys", "files"]),
        var("pending", ["conversation", "pending_files"]), var("contract", ["env", "itsm_response_contract"]),
        var("reset", ["1791936000030", "reset_files"]), var("route", ["1791936000030", "route"]),
    ], ["state", "route", "reply"])
    by_id["1791936000420"]["data"]["title"] = "准备当轮原生截图"
    iteration = by_id["1791936000450"]["data"]
    iteration.update(iterator_selector=["sys", "files"], iterator_input_type="array[file]", title="上传当轮原生截图")
    code("1791936000470", "native_upload_cached", [
        var("state", ["conversation", "state"]),
        var("pending", ["conversation", "pending_files"]), var("index", ["1791936000450", "index"]),
        var("files", ["sys", "files"]),
    ], ["key", "cached", "result"])
    by_id["1791936000520"]["data"]["tool_parameters"]["file"]["value"] = ["1791936000450", "item"]
    code("1791936000560", "merge_uploads", [
        var("results", ["1791936000450", "output"]), var("state", ["conversation", "state"]),
        var("pending", ["conversation", "pending_files"]), var("ticket_route", ["1791936000030", "route"]),
    ], ["state", "route", "reply"])
    by_id["1791936000550"]["data"]["variables"] = [
        ["1791936000540", "result"], ["1791936000530", "result"], ["1792536001020", "result"],
    ]
    save = copy.deepcopy(by_id["1791936000040"])
    save.update(id=SAVE_GATE, selected=False, position={"x": 2800, "y": 1100}, positionAbsolute={"x": 2800, "y": 1100})
    save["data"].update(title="保存截图待处理状态", selected=False)
    save["data"]["items"][0]["value"] = ["1791936000420", "state"]
    graph["nodes"].append(save)
    by_id[SAVE_GATE] = save

    changed_paths = {
        ("1791936000060", "source"): "1791936000420",  # Retain -> universal upload gate.
        ("1791936000420", "source"): SAVE_GATE,
        ("1791936000430", "ready"): "1791936000070",  # Ready -> original query/ticket/reply dispatch.
        ("1791936000580", "ready"): "1791936000070",  # Successfully uploaded -> same dispatch.
        ("1791936000070", "ticket"): "1791936000600",  # Ticket already has prepared URLs.
    }
    for edge in graph["edges"]:
        target = changed_paths.get((edge["source"], edge["sourceHandle"]))
        if target:
            edge["target"] = target
            edge["data"]["targetType"] = by_id[target]["data"]["type"]
            edge["id"] = f"{edge['source']}-{edge['sourceHandle']}-{target}-target"

    def edge(source, target, handle="source", iteration=False):
        data = {"isInIteration": iteration, "isInLoop": False,
                "sourceType": by_id[source]["data"]["type"], "targetType": by_id[target]["data"]["type"]}
        if iteration:
            data["iteration_id"] = "1791936000450"
        graph["edges"].append({
            "id": f"{source}-{handle}-{target}-target", "source": source, "sourceHandle": handle,
            "target": target, "targetHandle": "target", "type": "custom", "zIndex": 1002 if iteration else 0, "data": data,
        })

    edge(SAVE_GATE, "1791936000430")
    edge("1791936000480", "1791936000520", "false", iteration=True)
    # Verified in the actual RunAI upload on 2026-10-21: text is a bare image
    # URL, while json is [{"data": []}]. The OpenAPI example is not the tool's
    # runtime output. Persist this proven mapping when rebuilding the workflow.
    contract_env = next(v for v in app["workflow"]["environment_variables"] if v["name"] == "itsm_response_contract")
    contract = json.loads(contract_env["value"])
    contract["upload"] = {"verified": True, "source": "text", "body_path": "", "url_path": "",
                          "success_path": "", "success_values": []}
    contract_env["value"] = json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
    return app


def write_candidate():
    app = native_candidate()

    class Dumper(yaml.SafeDumper):
        pass

    Dumper.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))
    OUTPUT.write_text(yaml.dump(app, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=120))
    for n in app["workflow"]["graph"]["nodes"]:
        if n["id"] in {"1791936000420", "1791936000470", "1791936000560"}:
            (ROOT / "verification" / ("附件原生直传_" + n["id"] + ".py")).write_text(n["data"]["code"])
    return OUTPUT


if __name__ == "__main__":
    print(write_candidate())
