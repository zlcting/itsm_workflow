"""Prepare a local candidate; conditional input rendering still needs the host UI.

Patch the last exported draft so its model, retrieval and tool settings survive.
This does not update or publish the live application.
"""

import copy
from pathlib import Path

import yaml

from build import bundle
from compatibility import native_variable_assigner_clears

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "系统异常助手_上游薪号修复版.yml"
OUTPUT = ROOT / "系统异常助手_薪号兜底候选_待页面接入.yml"


def candidate(source=None):
    app = copy.deepcopy(source if source is not None else yaml.safe_load(SOURCE.read_text()))
    nodes = app["workflow"]["graph"]["nodes"]
    start = next(n for n in nodes if n["data"]["type"] == "start")
    prepare = next(n["data"] for n in nodes if n["data"]["title"] == "校验工单与确认版本")
    if start["data"]["variables"]:
        raise ValueError("The baseline now has Start inputs; review them before applying the patch.")
    # Standard text-input schema already exported by this RunAI instance.
    # This declaration alone does NOT make its rendering conditional.
    start["data"]["variables"] = [{
        "label": "薪号（未获取到系统薪号时填写）", "max_length": 128,
        "options": [], "required": False, "type": "text-input", "variable": "manual_salary_id",
    }]
    prepare["variables"].append({"variable": "manual_salary_id", "value_type": "string",
                                  "value_selector": [start["id"], "manual_salary_id"]})
    prepare["code"] = bundle("prepare_ticket_with_salary_fallback", [v["variable"] for v in prepare["variables"]])
    return native_variable_assigner_clears(app)


def write_candidate():
    app = candidate()
    class Dumper(yaml.SafeDumper):
        pass
    Dumper.add_representer(str, lambda d, s: d.represent_scalar(
        "tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))
    OUTPUT.write_text(yaml.dump(app, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=120))
    return OUTPUT


if __name__ == "__main__":
    print(write_candidate())
