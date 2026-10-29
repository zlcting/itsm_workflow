"""Optional native File adapter; apply only after confirming List Operator support.

Runtime schema and output semantics:
https://raw.githubusercontent.com/langgenius/dify/1.9.1/api/core/workflow/nodes/list_operator/entities.py
https://raw.githubusercontent.com/langgenius/dify/1.9.1/api/core/workflow/nodes/list_operator/node.py
The node returns the first original File value, preserving its native type.
"""

import copy

SINGLE_FILE = "1792536001040"


def single_native_file(source):
    app = copy.deepcopy(source)
    graph = app["workflow"]["graph"]
    by_id = {n["id"]: n for n in graph["nodes"]}
    upload = by_id["1791936000520"]
    if SINGLE_FILE in by_id:
        if (by_id[SINGLE_FILE]["data"].get("type") == "list-operator"
                and upload["data"]["tool_parameters"]["file"]["value"] == [SINGLE_FILE, "first_record"]):
            return app
        raise ValueError("The reserved File adapter ID is already used by another node.")
    links = [e for e in graph["edges"] if e["source"] == "1791936000510" and e["sourceHandle"] == "yes"]
    if len(links) != 1 or links[0]["target"] != upload["id"]:
        raise ValueError("The single-file readiness check must directly precede the upload tool.")
    if upload["data"]["tool_parameters"]["file"]["value"] != ["1791936000490", "files"]:
        raise ValueError("Unexpected upload file selector: inspect it before applying the adapter.")

    node = copy.deepcopy(by_id["1791936000530"])
    node.update(id=SINGLE_FILE, selected=False, position={"x": 1670, "y": 160})
    origin = by_id[node["parentId"]]["position"]
    node["positionAbsolute"] = {"x": origin["x"] + 1670, "y": origin["y"] + 160}
    node["data"] = {
        "type": "list-operator", "title": "取单个原生截图文件", "desc": "", "selected": False,
        "isInIteration": True, "iteration_id": node["parentId"],
        "var_type": "array[file]", "item_var_type": "file",
        "variable": ["1791936000490", "files"],
        "filter_by": {"enabled": False, "conditions": []},
        "extract_by": {"enabled": False, "serial": "1"},
        "order_by": {"enabled": False, "key": "", "value": "asc"},
        "limit": {"enabled": False, "size": 1},
    }
    graph["nodes"].append(node)
    incoming = links[0]
    incoming["target"] = SINGLE_FILE
    incoming["id"] = f"{incoming['source']}-{incoming['sourceHandle']}-{SINGLE_FILE}-target"
    incoming["data"]["targetType"] = "list-operator"
    outgoing = copy.deepcopy(incoming)
    outgoing.update(id=f"{SINGLE_FILE}-source-{upload['id']}-target", source=SINGLE_FILE,
                    sourceHandle="source", target=upload["id"])
    outgoing["data"].update(sourceType="list-operator", targetType="tool")
    graph["edges"].append(outgoing)
    upload["data"]["tool_parameters"]["file"]["value"] = [SINGLE_FILE, "first_record"]
    return app
