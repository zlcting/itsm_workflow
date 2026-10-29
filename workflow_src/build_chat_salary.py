"""Patch an exported RunAI draft to collect missing payroll IDs in chat."""

import copy
from pathlib import Path

import yaml

from build import bundle
from compatibility import native_variable_assigner_clears
import prompts

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "backups/系统异常助手_聊天补薪号前_20261021.yml"
OUTPUT = ROOT / "verification/系统异常助手_聊天补薪号候选.yml"


def candidate(source=None):
    app = copy.deepcopy(source if source is not None else yaml.safe_load(SOURCE.read_text()))
    nodes = app["workflow"]["graph"]["nodes"]
    start = next(n for n in nodes if n["data"]["type"] == "start")
    if start["data"]["variables"]:
        raise ValueError("Unexpected Start fields: review the current draft before patching.")
    for n in nodes:
        d = n["data"]
        if d["title"] == "理解问题与本轮意图":
            next(p for p in d["prompt_template"] if p["role"] == "system")["text"] = prompts.ROUTER
        if d["title"] == "更新问题及会话状态":
            if not any(v["variable"] == "salary_id" for v in d["variables"]):
                d["variables"].append({"variable": "salary_id", "value_selector": ["sys", "salary_id"], "value_type": "string"})
            d["code"] = bundle("route", [v["variable"] for v in d["variables"]])
        if d["title"] == "校验工单与确认版本":
            d["code"] = bundle("prepare_ticket", [v["variable"] for v in d["variables"]])
    return native_variable_assigner_clears(app)


def write_candidate():
    app = candidate()
    class Dumper(yaml.SafeDumper):
        pass
    Dumper.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))
    OUTPUT.write_text(yaml.dump(app, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=120))
    for n in app["workflow"]["graph"]["nodes"]:
        if n["data"]["title"] in ("更新问题及会话状态", "校验工单与确认版本"):
            (ROOT / "verification" / ("聊天补薪号_" + n["id"] + ".py")).write_text(n["data"]["code"])
    (ROOT / "verification/聊天补薪号_意图提示.txt").write_text(prompts.ROUTER)
    return OUTPUT


if __name__ == "__main__":
    print(write_candidate())
