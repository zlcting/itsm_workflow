"""Compatibility fixes for node contracts exposed by the user's RunAI instance."""

import copy


def native_variable_assigner_clears(app):
    """Repair only v2 clear inputs; preserve all other exported configuration.

    Dify v2's helpers reject constant input for clear, while its node skips
    source-value lookup for this operation. The native DSL uses variable/''.
    https://github.com/langgenius/dify/blob/1.9.1/api/core/workflow/nodes/variable_assigner/v2/helpers.py
    """
    app = copy.deepcopy(app)
    for node in app["workflow"]["graph"]["nodes"]:
        data = node["data"]
        if data["type"] == "assigner" and str(data.get("version")) == "2":
            for item in data.get("items", []):
                if item.get("operation") == "clear":
                    item["input_type"] = "variable"
                    item["value"] = ""
    return app


def native_retrieval_outputs(app):
    """Remove unsupported retrieval error branching; keep all other settings intact.

    Observed in RunAI on 2026-10-19: retrieval exposes only `source`. Setting
    fail-branch makes a successful retrieval end the run without a response,
    even when an additional source edge is present.
    """
    app = copy.deepcopy(app)
    graph = app["workflow"]["graph"]
    retrieval_ids = set()
    for node in graph["nodes"]:
        data = node["data"]
        if data["type"] == "knowledge-retrieval":
            retrieval_ids.add(node["id"])
            for field in ("error_strategy", "default_value", "retry_config"):
                data.pop(field, None)
    edges, seen = [], set()
    for edge in graph["edges"]:
        if edge["source"] in retrieval_ids:
            if edge["sourceHandle"] == "fail-branch":
                continue
            edge["sourceHandle"] = "source"
            edge["id"] = f"{edge['source']}-source-{edge['target']}-target"
        key = (edge["source"], edge["sourceHandle"], edge["target"], edge["targetHandle"])
        if key not in seen:
            seen.add(key)
            edges.append(edge)

    # Remove the now unreachable retrieval-error-only chains, retaining iteration
    # subgraphs and any failure reply shared by other nodes.
    by_id = {node["id"]: node for node in graph["nodes"]}
    pending = [n["id"] for n in graph["nodes"] if n["data"]["type"] == "start"]
    reachable = set()
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(e["target"] for e in edges if e["source"] == current)
        if by_id[current]["data"]["type"] == "iteration":
            pending.append(by_id[current]["data"]["start_node_id"])
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] in reachable]
    graph["edges"] = [e for e in edges if e["source"] in reachable and e["target"] in reachable]
    return app
