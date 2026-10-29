"""Pure workflow functions. The builder embeds only reachable functions in Code nodes."""

import hashlib
import json
import re
import uuid

FACTORIES = {}  # Replaced from factories.json when bundled into the DSL.
SERVICES = ["MES", "WMS", "ELM"]
CODES = [0, 6000, 7000, 8001, 8002, 8003, 8004, 8005, 9000, 7001, 7002, 7003, 7004, 6001, 5000, 7005, 5001, 6003]
INTENTS = ["QUERY", "FOLLOW_UP", "CREATE_TICKET", "TICKET_UPDATE", "CONFIRM", "CANCEL", "RESOLVED", "THANKS", "OTHER"]
NO_HIT = "暂未在知识库中查询到与这个问题相关的内容。你可以补充系统名称或完整报错，也可以回复“提交工单”，我会沿用当前问题帮你填写。"
QUERY_ERROR = "知识库暂时查询失败，我已保留当前问题。你可以稍后回复“重试查询”，或回复“提交工单”继续处理。"


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def obj(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def clean(value, limit=3000):
    return value.strip()[:limit] if isinstance(value, str) else ""


def normalized(value):
    return re.sub(r"[\s。！!？?，,]", "", clean(value)).lower()


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def initial_state():
    return {"schema": 1, "mode": "query", "problem": "", "query": "", "attempted": [],
            "followups": 0, "answered": False, "last_answer": "", "draft": {},
            "uploads": {}, "failed_uploads": [], "preview": "", "submission": {}, "history": [], "waiting_salary": False,
            "metrics": {"query_turns": 0, "retrieval_calls": 0, "no_hits": 0, "query_errors": 0,
                        "ticket_attempts": 0, "ticket_successes": 0}}


def state_load(value):
    s = initial_state()
    s.update(obj(value))
    return s


def valid_intent(value):
    if not isinstance(value, dict) or value.get("intent") not in INTENTS:
        return False
    for key in ("new_problem", "sufficient"):
        if type(value.get(key)) is not bool:
            return False
    for key in ("problem", "query", "clarification"):
        if not isinstance(value.get(key), str):
            return False
    if not isinstance(value.get("attempted"), list) or not all(isinstance(x, str) for x in value["attempted"]):
        return False
    fields = value.get("fields")
    allowed = {"traceId", "factory", "serviceCode", "code", "description", "ticketType"}
    return (isinstance(fields, dict) and set(fields).issubset(allowed)
            and all(isinstance(x, str) for x in fields.values()))


def normalize_intent(raw):
    # Some deployed model providers return JSON booleans as strings even with
    # structured output enabled. Convert only the two unambiguous literals.
    data = dict(obj(raw))
    for key in ("new_problem", "sufficient"):
        value = data.get(key)
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            data[key] = value.strip().lower() == "true"
    return data


def explicit_intent(query):
    command = normalized(query)
    commands = {"取消工单": "CANCEL", "取消提交": "CANCEL", "确认提交": "CONFIRM",
                "解决了": "RESOLVED", "已经解决": "RESOLVED", "谢谢": "THANKS",
                "提交工单": "CREATE_TICKET", "帮我提交工单": "CREATE_TICKET", "转人工": "CREATE_TICKET",
                "继续排查": "FOLLOW_UP", "重试查询": "FOLLOW_UP", "还是不行": "FOLLOW_UP",
                "先不提交帮我查一下": "FOLLOW_UP", "先不提交继续排查": "FOLLOW_UP",
                "重试上传": "TICKET_UPDATE", "替换失败截图": "TICKET_UPDATE", "重试提交": "TICKET_UPDATE",
                "不附截图提交": "TICKET_UPDATE", "移除附件": "TICKET_UPDATE", "不需要附件": "TICKET_UPDATE"}
    if command in commands:
        return commands[command]
    # Full-match requests only: a quoted phrase, a question about tickets, or
    # a negated request must never be interpreted as consent to create one.
    request = (r"(?:请|麻烦)?(?:帮我|帮忙|给我|替我|为我|我想|我要|我需要)?"
               r"(?:(?:提交|创建|新建|发起|建)(?:一?[个张])?工单|建(?:一?[个张])?单)"
               r"(?:来?(?:处理|解决)(?:一下)?(?:这个|该|当前|上述|刚才的)?问题)?(?:吧|谢谢|可以吗|好吗)?")
    return "CREATE_TICKET" if re.fullmatch(request, command) else ""


def fallback_intent(intent, state):
    return {"intent": intent, "new_problem": False, "sufficient": bool(state["problem"]),
            "problem": "", "query": state["query"], "clarification": "", "attempted": [], "fields": {}}


def compact_ticket_fields(query, state):
    # Only a complete, unambiguous field reply in an active ticket is eligible.
    # Never mine arbitrary four-digit numbers from problem descriptions.
    if state["mode"] not in ("ticket", "await_confirm"):
        return {}
    text = clean(query, 300).upper()
    labelled = re.fullmatch(r"(?:工厂(?:编号|代码)?|厂区(?:编号|代码)?|FACTORY(?:CODE)?)\s*[:：]?\s*([A-Z0-9]+)[。.!！]?", text)
    if labelled:
        value = labelled.group(1)
        return {"factory": value} if value in FACTORIES else {}
    tokens = [x for x in re.split(r"[\s,，;；、/]+", text.strip("。.!！")) if x]
    if not tokens or len(tokens) > 2 or len(set(tokens)) != len(tokens):
        return {}
    factories = [x for x in tokens if x in FACTORIES]
    services = [x for x in tokens if x in SERVICES]
    if len(factories) + len(services) != len(tokens) or len(factories) > 1 or len(services) > 1:
        return {}
    # A bare number answers a missing-factory question. Changing an already
    # selected factory requires its label or an explicit factory+system pair.
    if factories and not services and factory_resolve(state["draft"].get("factory", ""))[0]:
        return {}
    result = {}
    if factories:
        result["factory"] = factories[0]
    if services:
        result["serviceCode"] = services[0]
    return result


def safe_query(query, problem):
    q = re.sub(r"https?://\S+", "", clean(query, 600))
    q = re.sub(r"(?:提交工单|确认提交|帮我建单|附件URL[:：]?|用户意图[:：]?\w*)", "", q)
    # Preserve error-code tokens; never introduce tokens absent from the problem.
    codes = re.findall(r"\b[A-Z][A-Z_]*[-:]?\d{2,}\b|\b\d{3,5}\b", problem)
    for code in codes:
        if code not in q:
            q += " " + code
    return re.sub(r"\s+", " ", q).strip()[:800]


def chat_salary_value(query, allow_bare):
    """Read only an explicit field reply or an identifier answering our prompt."""
    text = query.strip() if isinstance(query, str) else ""
    labelled = re.fullmatch(r"(?:我的)?(?:薪号|工号|员工编号)\s*(?:[:：]|是|为|改为|修改为)\s*(.*?)\s*[。]?", text, re.S)
    if labelled:
        return True, labelled.group(1)
    # Without a field label, require a digit so 'OK', system names and ordinary
    # conversation are not silently treated as employee identifiers.
    if allow_bare and re.fullmatch(r"[A-Za-z0-9_.-]+", text) and re.search(r"\d", text):
        return True, text
    return False, ""


def route(raw, state, query, files, salary_id=None):
    s = state_load(state)
    result = {"state": dump(s), "route": "REPLY", "reply": "", "reset_files": "no", "intent": "OTHER"}
    data = normalize_intent(raw)
    control = explicit_intent(query)
    ticket_active = s["mode"] in ("ticket", "await_confirm")
    waiting_salary = ticket_active and s.get("waiting_salary") is True
    system_present = not salary_missing(salary_id)
    salary_reply, salary_value = chat_salary_value(query, waiting_salary)
    if ticket_active and not control and (salary_reply or waiting_salary) and s["submission"].get("status") in ("pending", "unknown"):
        result.update(route="TICKET", intent="TICKET_UPDATE")
        return result
    if system_present:
        s["draft"].pop("_manual_salary_id", None)
        s["waiting_salary"] = False
    if ticket_active and not control and salary_reply:
        # Never let an LLM turn a payroll reply into a factory, code or issue.
        if not system_present:
            author, error = login_identity(salary_value)
            if error:
                s["draft"].pop("_manual_salary_id", None)
                s.update(waiting_salary=True, preview="")
                result.update(state=dump(s), reply="薪号未填写或格式不正确。请输入你的薪号，例如“薪号：001234”；不要包含空格或换行，长度不超过128个字符。")
                return result
            s["draft"]["_manual_salary_id"] = author
        s.update(waiting_salary=False, preview="", needs_field_review=False)
        result.update(state=dump(s), route="TICKET", intent="TICKET_UPDATE")
        return result
    explicit_new = bool(re.search(r"(?:换(?:一)?个问题|换个新问题|另一个问题|新的?问题)", clean(query)))
    if waiting_salary and not control and not explicit_new:
        # Unrecognized replies stay in the payroll prompt instead of overwriting
        # the existing issue or starting a knowledge search for an employee ID.
        s["waiting_salary"] = not system_present
        result.update(state=dump(s), route="TICKET", intent="TICKET_UPDATE")
        return result
    if waiting_salary and control:
        data = fallback_intent(control, s)
    if waiting_salary and explicit_new:
        s["waiting_salary"] = False
        if not valid_intent(data) or normalized(query) in ("换个问题", "换一个问题", "换个新问题", "另一个问题", "新问题"):
            data = fallback_intent("QUERY", s)
            data.update(new_problem=True, sufficient=False, problem="", query="")
        else:
            data["new_problem"] = True
            data["intent"] = "CREATE_TICKET" if data["intent"] == "CREATE_TICKET" else "QUERY"
    compact_fields = compact_ticket_fields(query, s) if not control else {}
    if compact_fields:
        # Verified short replies contain only fields, not a new symptom or an
        # error code. Preserve the problem and override the model's guesses.
        data = fallback_intent("TICKET_UPDATE", s)
        data["fields"] = compact_fields
    if not valid_intent(data):
        if control:
            data = fallback_intent(control, s)
        elif s["mode"] in ("ticket", "await_confirm"):
            data = fallback_intent("TICKET_UPDATE", s)
            s["needs_field_review"] = True
            s["preview"] = ""
        else:
            result["reply"] = "暂时没有识别清楚这次补充，之前的问题已保留。请补充系统名称或具体报错；需要人工处理时，也可以直接说“帮我提交一个工单”。"
            return result
    intent = control or data["intent"]
    command = normalized(query)
    if control == "CREATE_TICKET" and s["problem"]:
        # A standalone request referring to this issue supplies no new facts.
        data = fallback_intent(control, s)
    result["intent"] = intent
    new_problem = data["new_problem"] and intent in ("QUERY", "CREATE_TICKET") and bool(clean(query))
    if new_problem:
        old = s
        s = initial_state()
        s["history"] = old["history"]
        if old["submission"].get("status") in ("pending", "unknown", "success"):
            s["history"] = (s["history"] + [old["submission"]])[-10:]
        s["metrics"] = old["metrics"]
        result["reset_files"] = "yes"
    if not clean(query) and files:
        intent = "TICKET_UPDATE" if s["mode"] in ("ticket", "await_confirm") else "QUERY"
        data["sufficient"] = False
        data["problem"] = ""
    if intent == "CANCEL":
        unknown = s["submission"].get("status") in ("pending", "unknown")
        s.update(mode="query", draft={}, uploads={}, preview="", needs_field_review=False, waiting_salary=False)
        result["reset_files"] = "discard"
        result["reply"] = ("已退出工单填写。上次提交结果仍待核实，请先联系 IT 支持核对，退出对话不能撤回已发送的请求。"
                           if unknown else "已取消待提交工单。你可以继续排查当前问题，也可以描述新的问题。")
    elif intent == "RESOLVED":
        s.update(mode="query", draft={}, uploads={}, preview="", problem="", query="",
                 attempted=[], followups=0, answered=False, last_answer="", needs_field_review=False, waiting_salary=False)
        result.update(reset_files="discard", reply="好的，当前问题已结束。有新的报错时直接发给我即可。")
    elif intent == "THANKS":
        result["reply"] = "不客气。如果仍有问题，可以继续补充信息；需要人工处理时回复“提交工单”。"
    elif intent == "OTHER":
        result["reply"] = "请描述遇到的系统异常，最好带上系统名称和报错原文。我可以查询知识库，也可以帮你提交工单。"
    else:
        if data["problem"] and intent != "CONFIRM":
            s["problem"] = clean(data["problem"])
        for step in data["attempted"]:
            step = clean(step, 300)
            if step and step not in s["attempted"]:
                s["attempted"].append(step)
        s["attempted"] = s["attempted"][-12:]
        fields = data["fields"]
        if any(clean(value) for value in fields.values()):
            s["needs_field_review"] = False
        changed = False
        for key, value in fields.items():
            value = clean(value)
            if value and s["draft"].get(key) != value:
                s["draft"][key] = value
                changed = True
        if intent == "CONFIRM" and clean(fields.get("description")):
            s["problem"] = clean(fields["description"])
        if s["problem"]:
            s["draft"]["description"] = s["problem"]
        if files or changed:
            s["preview"] = ""
        if command in ("不附截图提交", "移除附件", "不需要附件"):
            s["uploads"] = {}
            s["preview"] = ""
            result["reset_files"] = "discard"
            intent = "TICKET_UPDATE"
        if intent in ("CREATE_TICKET", "TICKET_UPDATE", "CONFIRM"):
            s["mode"] = "ticket"
            result["route"] = "TICKET"
        else:
            s["mode"] = "query"
            s["waiting_salary"] = False
            if not clean(query) and files:
                result["reply"] = "截图已保留。目前无法直接读取图片中的报错，请补充报错文字或描述异常现象，我再帮你查询。"
            elif not data["sufficient"] or not s["problem"]:
                result["reply"] = "为了找到相关处理方法，请补充：使用的是哪个系统？具体报错文字或异常现象是什么？"
            elif intent == "FOLLOW_UP" and s["answered"] and s["followups"] >= 2:
                result["reply"] = "目前没有足够的新依据继续排查。你可以回复“提交工单”，我会带上问题描述和已尝试的步骤；有新的报错信息也可以继续补充。"
            else:
                if intent == "FOLLOW_UP" and s["answered"]:
                    s["followups"] += 1
                s["query"] = safe_query(data["query"] or s["query"] or s["problem"], s["problem"])
                result["route"] = "QUERY" if s["query"] else "REPLY"
                if result["route"] == "QUERY":
                    s["metrics"]["query_turns"] += 1
                if not s["query"]:
                    result["reply"] = "请补充具体报错文字或异常现象。"
    result["state"] = dump(s)
    return result


def query_input(state):
    s = state_load(state)
    return {"query": s["query"], "problem": s["problem"], "attempted": dump(s["attempted"]),
            "last_answer": s["last_answer"]}


def record_identity(value):
    # Compare complete readable descriptions only. URLs and image markup carry
    # no verified solution; broad keyword overlap is not an identity match.
    text = clean(value, 12000)
    text = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


def assess(raw, chunks, state, second):
    s = state_load(state)
    d = obj(raw)
    status = d.get("status")
    valid = (status in ("ANSWER", "PARTIAL", "NONE") and isinstance(d.get("evidence_ids"), list)
             and all(type(i) is int for i in d["evidence_ids"]) and isinstance(d.get("alternate_query"), str))
    route_name = "ERROR"
    selected = []
    alternate = ""
    reason = clean(d.get("reason"), 400)
    if valid:
        ids = sorted(set(d["evidence_ids"]))
        if any(i < 1 or i > len(chunks) for i in ids):
            valid = False
        else:
            if status == "NONE":
                identities = {record_identity(s["problem"]), record_identity(s["query"])}
                identities = {x for x in identities if len(x) >= 8}
                exact_ids = [i + 1 for i, chunk in enumerate(chunks)
                             if record_identity(chunk.get("content")) in identities]
                if exact_ids:
                    status, ids = "PARTIAL", exact_ids
                    reason = "已召回与当前异常描述一致的记录，但尚无足够依据给出处理办法；请说明资料缺口并展示来源，不得声称没有相关内容。"
            selected = [chunks[i - 1] for i in ids]
            if status in ("ANSWER", "PARTIAL") and selected and any(clean(x.get("content")) for x in selected):
                route_name = status
            elif status == "NONE" or not chunks:
                alternate = safe_query(d["alternate_query"], s["problem"])
                route_name = "RETRY" if not second and alternate and alternate != s["query"] else "NONE"
    if route_name == "NONE":
        s["metrics"]["no_hits"] += 1
    elif route_name == "ERROR":
        s["metrics"]["query_errors"] += 1
    identities = {record_identity(s["problem"]), record_identity(s["query"])}
    identities = {x for x in identities if len(x) >= 8}
    record_only = (route_name == "PARTIAL" and bool(selected)
                   and all(record_identity(x.get("content")) in identities for x in selected))
    sources = []
    for chunk in selected:
        metadata = chunk.get("metadata")
        title = clean((metadata.get("document_name") if isinstance(metadata, dict) else "") or chunk.get("title"), 300)
        if title and title not in sources:
            sources.append(title)
    record_reply = ""
    if record_only:
        source_text = "，来源为" + "、".join("《" + title + "》" for title in sources) if sources else ""
        record_reply = ("找到与“" + clean(s["problem"] or s["query"], 800) + "”相同的问题记录" + source_text
                        + "。但该记录当前可读取的文字只有问题描述，没有原因或处理方法，暂时无法据此提供解决步骤。\n\n"
                        + "你可以补充新的报错文字或操作背景；也可以回复“提交工单”，我会沿用当前问题帮你填写。")
    return {"state": dump(s), "route": route_name, "alternate": alternate,
            "evidence": selected, "assessment": dump({"status": route_name, "reason": reason,
                                                    "record_only": record_only, "sources": sources,
                                                    "record_reply": record_reply})}


def remember_answer(state, text):
    s = state_load(state)
    s["answered"] = True
    s["last_answer"] = clean(text, 6000)
    return {"state": dump(s)}


def query_failure(state):
    s = state_load(state)
    s["metrics"]["retrieval_calls"] += 1
    s["metrics"]["query_errors"] += 1
    return {"state": dump(s), "reply": QUERY_ERROR}


def retrieved(state):
    s = state_load(state)
    s["metrics"]["retrieval_calls"] += 1
    return {"state": dump(s)}


def contract_load(value):
    c = obj(value)
    return c if c.get("version") == 1 else {}


def at_path(value, path):
    if not isinstance(path, str):
        return None
    for part in path.split(".") if path else []:
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def response_body(text, structured, spec):
    if not isinstance(spec, dict):
        return None
    source = spec.get("source")
    if source == "json":
        value = structured
    elif source == "text":
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            value = text
    else:
        return None
    return at_path(value, spec.get("body_path", ""))


def exact_match(value, candidates):
    return any(type(value) is type(x) and value == x for x in candidates)


def create_contract_ready(contract):
    c = contract_load(contract)
    spec = c.get("create", {})
    checks = spec.get("success_checks", []) if isinstance(spec, dict) else None
    return (isinstance(spec, dict) and c.get("verified") is True and spec.get("source") in ("text", "json")
            and isinstance(spec.get("success_path"), str) and bool(spec["success_path"])
            and all(isinstance(spec.get(key, ""), str) for key in ("body_path", "failure_path", "ticket_id_path"))
            and isinstance(spec.get("success_values"), list) and bool(spec["success_values"])
            and isinstance(spec.get("failure_values", []), list)
            and isinstance(checks, list)
            and all(isinstance(check, dict) and isinstance(check.get("path"), str) and bool(check["path"])
                    and isinstance(check.get("values"), list) and bool(check["values"]) for check in checks))


def file_key(file):
    if isinstance(file, dict):
        return clean(file.get("key") or file.get("related_id") or file.get("id") or file.get("url") or file.get("remote_url"), 2000) or digest(file)
    return digest(str(file))


def retain_files(files, pending, state, reset, query):
    # Standard Dify conversation variables support array[object], NOT array[file].
    # Keep only public file references; the HTTP node recreates native File values.
    kept = [] if reset in ("yes", "discard") else list(pending or [])
    if normalized(query) == "替换失败截图" and files:
        failed = state_load(state).get("failed_uploads", [])
        kept = [x for x in kept if x.get("key") not in failed]
    if reset != "discard":
        positions = {x["key"]: i for i, x in enumerate(kept)}
        for file in files or []:
            item = {"key": file_key(file), "url": clean(file.get("url") or file.get("remote_url"), 4000),
                    "filename": clean(file.get("filename"), 200)}
            if item["key"] in positions:
                kept[positions[item["key"]]] = item
            else:
                positions[item["key"]] = len(kept)
                kept.append(item)
    return {"pending": kept}


def attachment_gate(state, files, contract):
    s = state_load(state)
    pending = [f for f in (files or []) if file_key(f) not in s["uploads"]]
    spec = contract_load(contract).get("upload", {})
    ready = (isinstance(spec, dict) and spec.get("verified") is True and spec.get("source") in ("json", "text")
             and all(isinstance(spec.get(key, ""), str) for key in ("body_path", "url_path", "success_path"))
             and isinstance(spec.get("success_values", []), list))
    if pending and not ready:
        return {"route": "BLOCKED", "reply": "截图已保留，但附件服务尚未完成接入。你可以稍后重试，或回复“不附截图提交”继续填写工单。"}
    return {"route": "UPLOAD" if pending else "READY", "reply": ""}


def upload_cached(file, state, base_url):
    key = file_key(file)
    url = state_load(state)["uploads"].get(key, "")
    download_url = clean(file.get("url"), 4000)
    if download_url.startswith("/") and not download_url.startswith("//"):
        download_url = clean(base_url, 2000).rstrip("/") + download_url
    if not re.fullmatch(r"https?://[^\s]+", download_url):
        download_url = ""
    return {"key": key, "cached": "yes" if url else "no", "result": dump({"key": key, "url": url}), "download_url": download_url}


def native_attachment_gate(state, files, pending, contract, reset, route):
    s = state_load(state)
    active = set(file_key(f) for f in (pending or []))
    missing = list(dict.fromkeys(file_key(f) for f in (pending or []) if file_key(f) not in s["uploads"]))
    s["failed_uploads"] = missing
    if reset == "discard":
        return {"state": dump(s), "route": "READY", "reply": ""}
    current = [f for f in (files or []) if file_key(f) in active and file_key(f) not in s["uploads"]]
    if current:
        result = attachment_gate(dump(s), current, contract)
        result["state"] = dump(s)
        return result
    if missing and route == "TICKET":
        s["preview"] = ""
        return {"state": dump(s), "route": "BLOCKED",
                "reply": ("还有 %d 张截图未上传成功，当前消息没有可重新上传的原文件。"
                          "请重新附上这些截图并回复“替换失败截图”，或回复“不附截图提交”继续填写工单。" % len(missing))}
    return {"state": dump(s), "route": "READY", "reply": ""}


def native_upload_cached(state, pending, index, files):
    # RunAI serializes sys.files as metadata for Code inputs, but passing a
    # single native iteration.item directly to the sandbox fails serialization.
    # Keep that native File exclusively on the upload-tool input.
    if not isinstance(files, list) or type(index) is not int or index < 0 or index >= len(files):
        return {"key": "", "cached": "yes", "result": dump({"key": "", "url": "", "error": "附件处理未完成：未能取得当前截图元数据。"})}
    key = file_key(files[index])
    active = set(file_key(f) for f in (pending or []))
    earlier = files[:index]
    if key not in active or any(file_key(f) == key for f in earlier):
        return {"key": key, "cached": "yes", "result": dump({"key": key, "url": "", "skip": True})}
    url = state_load(state)["uploads"].get(key, "")
    return {"key": key, "cached": "yes" if url else "no", "result": dump({"key": key, "url": url})}


def downloaded(status_code, files, key="", body="", error_message="", error_type=""):
    ready = (type(status_code) is int and status_code == 200 and isinstance(files, list)
             and len(files) == 1 and not error_message and not error_type)
    feedback = ""
    if not ready:
        if type(status_code) is int and status_code > 0:
            feedback = "读取截图失败（HTTP %s）。" % status_code
            if status_code == 200:
                feedback += "未取得单个可上传文件。"
        else:
            feedback = "读取截图失败，未收到有效的 HTTP 响应。"
        details = response_feedback(body, [], {"source": "text"}, error_message, error_type)
        if details:
            feedback += "\n" + details
    # Existing callers only consume ready. The serialized result can be wired
    # into the failure branch without exposing a signed download URL as a key.
    return {"ready": "yes" if ready else "no", "feedback": feedback,
            "result": dump({"key": key, "url": "", "error": feedback})}


def upload_result(text, structured, key, contract, error_message="", error_type="", transport_error=False):
    spec = contract_load(contract).get("upload", {})
    spec = spec if isinstance(spec, dict) else {}
    body = response_body(text, structured, spec)
    url = at_path(body, spec.get("url_path", ""))
    reason = ""
    if transport_error or error_message or error_type:
        reason = "附件上传调用报错。"
    elif spec.get("verified") is not True:
        reason = "附件响应映射尚未验证，无法确认上传成功。"
    elif spec.get("success_path") and not exact_match(at_path(body, spec["success_path"]), spec.get("success_values", [])):
        reason = "附件服务未返回成功状态。"
    elif not isinstance(url, str) or not re.fullmatch(r"https?://[^\s]+", url):
        reason = "接口已返回，但未取得有效的附件地址。"
    if reason:
        url = ""
        details = response_feedback(text, structured, spec, error_message, error_type)
        reason += "\n" + (details or "接口没有提供具体报错信息。")
    return {"result": dump({"key": key, "url": url, "error": reason})}


def merge_uploads(results, state, pending=None, ticket_route=""):
    s = state_load(state)
    failed = 0
    failed_keys = []
    errors = []
    attempted = set()
    incomplete = False
    for item in results if isinstance(results, list) else [None]:
        item = obj(item)
        if not item:
            incomplete = True
            failed += 1
            detail = "附件处理未完成，流程没有返回上传结果。"
            if detail not in errors:
                errors.append(detail)
            continue
        if item.get("skip") is True:
            continue
        if item.get("key"):
            attempted.add(item["key"])
        if item.get("key") and item.get("url"):
            s["uploads"][item["key"]] = item["url"]
        else:
            failed += 1
            if item.get("key"):
                failed_keys.append(item["key"])
            detail = feedback_text(item.get("error") or "")
            if detail and detail not in errors:
                errors.append(detail)
    if pending is not None:
        missing = list(dict.fromkeys(file_key(f) for f in pending if file_key(f) not in s["uploads"]))
        stale = [key for key in missing if key not in attempted]
        failed_keys = missing
        if stale and ticket_route == "TICKET":
            failed = max(failed, len(missing))
            if not incomplete:
                errors.append("还有 %d 张之前的截图未上传成功。请重新附上这些截图并回复“替换失败截图”，不能仅凭旧引用重新上传。" % len(stale))
    s["failed_uploads"] = failed_keys
    s["preview"] = ""
    reply = ""
    if failed:
        reply = "有 %d 张截图读取或上传失败，其他截图已保留。" % failed
        if errors:
            detail = "\n\n".join(errors)
            if len(detail) > 6000:
                detail = detail[:6000] + "\n[返回内容较长，后续已截断；完整内容可在本次运行日志查看]"
            reply += "\n\n具体错误：\n\n" + literal_feedback(detail)
        if pending is not None:
            reply += "\n\n请重新附上未上传成功的截图并回复“替换失败截图”；已成功上传的截图会保留。也可以回复“不附截图提交”或“继续排查”。"
        else:
            reply += "\n\n你可以回复“重试上传”；链接过期时请重新附图并回复“替换失败截图”，也可以回复“不附截图提交”。"
    return {"state": dump(s), "route": "FAILED" if failed else "READY", "reply": reply}


def factory_resolve(value):
    value = clean(value, 200)
    if value.upper() in FACTORIES:
        return value.upper(), ""
    candidates = [(code, x["name"]) for code, x in FACTORIES.items() if value and (value == x["name"] or value in x["name"])]
    if len(candidates) == 1:
        return candidates[0][0], ""
    if candidates:
        return "", "该工厂名称对应多个编号，请选择：" + "；".join(code + "（" + name + "）" for code, name in candidates[:8])
    return "", "请提供有效的工厂编号或工厂名称。"


def login_identity(value):
    """Validate the upstream identifier without guessing employee-number rules."""
    missing = "没有获取到当前登录用户的薪号（工号）。请重新登录业务系统；如仍未恢复，请联系 IT 支持检查身份接入。"
    invalid = "业务系统传入的薪号（工号）格式异常。请重新登录；如仍未恢复，请联系 IT 支持检查身份接入。"
    if value is None:
        return "", missing
    if not isinstance(value, str):
        return "", invalid
    author = value.strip()
    if not author or author.lower() in ("null", "none", "undefined"):
        return "", missing
    if len(author) > 128 or re.search(r"[\s\x00-\x1f\x7f]", author):
        return "", invalid
    return author, ""


def salary_missing(value):
    return value is None or (isinstance(value, str) and value.strip().lower() in ("", "null", "none", "undefined"))


def resolve_salary(salary_id, manual_salary_id):
    """Use the upstream identity whenever present; manual input is fallback only."""
    if not salary_missing(salary_id):
        return login_identity(salary_id)
    if salary_missing(manual_salary_id):
        return "", "没有获取到薪号（工号），请在薪号输入框中补充后继续。"
    author, error = login_identity(manual_salary_id)
    if error:
        return "", "手填薪号（工号）格式异常，请检查输入内容；不要包含空格或换行，长度不超过128个字符。"
    return author, ""


def prepare_ticket_with_salary_fallback(state, query, salary_id, contract, manual_salary_id):
    return prepare_ticket(state, query, salary_id, contract,
                          manual_salary_id="" if manual_salary_id is None else manual_salary_id)


def prepare_ticket(state, query, salary_id, contract, manual_salary_id=None):
    s = state_load(state)
    draft = s["draft"]
    # The current workflow collects fallback identity in chat, scoped to this
    # draft. The optional argument only supports the historical form candidate.
    chat_mode = manual_salary_id is None
    manual = draft.get("_manual_salary_id", "") if chat_mode else manual_salary_id
    if not salary_missing(salary_id):
        draft.pop("_manual_salary_id", None)
    author, identity_error = resolve_salary(salary_id, manual)
    s["waiting_salary"] = False
    result = {"state": dump(s), "route": "REPLY", "reply": "", "payload": "{}",
              "traceId": "", "serviceCode": "", "factoryCode": "", "code": 0,
              "description": "", "author": "", "file": "[]"}
    previous = s["submission"]
    if previous.get("status") in ("pending", "unknown"):
        result["reply"] = "上次提交结果尚待核实，为避免重复建单，暂不再次提交。"
        if previous.get("feedback"):
            result["reply"] += "\n\n上次接口返回或调用错误：\n" + literal_feedback(previous["feedback"])
        result["reply"] += "\n\n请联系 IT 支持核对追踪 ID：" + previous.get("traceId", "")
        return result
    if previous.get("status") == "success":
        result["reply"] = "当前问题已提交成功" + ("，工单号：" + previous["ticket_id"] if previous.get("ticket_id") else "") + "。如有新的异常，请描述新问题。"
        return result
    factory, factory_error = factory_resolve(draft.get("factory", ""))
    service = clean(draft.get("serviceCode"), 40).upper()
    description = clean(draft.get("description") or s["problem"])
    missing = []
    if not factory:
        missing.append("工厂：" + (factory_error if draft.get("factory") else "请提供工厂名称或编号。"))
    if service not in SERVICES:
        context = service or ("SAP" if re.search(r"\bSAP\b", description, re.I) else "")
        missing.append("工单所属系统：" + ("已记录报错涉及 %s；当前工单接口支持 MES、WMS、ELM，请确认实际归属，不确定可说明。" % context
                                           if context else "请选择 MES、WMS 或 ELM。"))
    if not description:
        missing.append("问题描述：请补充具体报错文字或异常现象。")
    if missing:
        s["preview"] = ""
        lead = ("已沿用当前问题：" + description[:180] + "\n\n") if description else "可以，我来帮你填写工单。\n\n"
        if s.get("needs_field_review"):
            lead += "这次补充尚未识别清楚，已有工单信息已保留。\n"
        result["reply"] = (lead + "还需要补充：\n" + "\n".join("- " + x for x in missing[:2])
                           + "\n\n可以按“字段名：内容”回复。补齐后我会展示确认清单，确认后再提交；也可以回复“取消工单”。")
    elif s.get("needs_field_review"):
        s["preview"] = ""
        result["reply"] = "工单草稿已保留，但上一条补充尚未识别清楚。请按“工厂：名称或编号”“所属系统：MES/WMS/ELM”或“问题描述：内容”重新说明要修改的字段；修改确认前不会提交。"
    elif identity_error:
        s["preview"] = ""
        if chat_mode and salary_missing(salary_id):
            s["waiting_salary"] = True
            result["reply"] = ("工单信息已保留。尚未获取到你的薪号（工号），请输入你的薪号，例如“薪号：001234”。\n\n"
                               "收到后我会继续填写并展示确认清单，确认后才提交；也可以回复“取消工单”或“继续排查”。")
        else:
            result["reply"] = "工单信息已保留，但" + identity_error
    else:
        try:
            code = int(draft.get("code", "0"))
        except (ValueError, TypeError):
            code = 0
        code = code if code in CODES else 0
        trace = clean(draft.get("traceId"), 200)
        if not trace:
            trace = uuid.uuid4().hex[:16]
            draft["traceId"] = trace
        if any(x.get("traceId") == trace and x.get("status") in ("pending", "unknown", "success") for x in s["history"]):
            result["reply"] = "这个异常数据 ID 已有成功或待核实的提交记录，请先联系 IT 支持核对，暂不重复建单。追踪 ID：" + trace
            return result
        if s["attempted"]:
            description += "\n已尝试：\n" + "\n".join("- " + x for x in s["attempted"])
        if draft.get("ticketType"):
            description += "\n用户补充的工单类型：" + clean(draft["ticketType"], 100)
        urls = list(dict.fromkeys(s["uploads"].values()))
        payload = {"traceId": trace, "serviceCode": service, "factoryCode": factory, "code": code,
                   "description": description, "createAuthor": author,
                   "updateAuthor": author, "file": dump(urls)}
        signature = digest(payload)
        result.update(payload=dump(payload), traceId=trace, serviceCode=service, factoryCode=factory,
                      code=code, description=description, author=author, file=dump(urls))
        if normalized(query) == "确认提交" and s["preview"] == signature:
            # Response mapping controls interpretation, not permission to send
            # the user's confirmed request. Unknown outcomes still block retries.
            result["route"] = "SUBMIT"
            s["submission"] = {"status": "pending", "signature": signature, "traceId": trace}
            s["metrics"]["ticket_attempts"] += 1
        else:
            s["preview"] = signature
            s["mode"] = "await_confirm"
            result["reply"] = ("请确认以下工单信息：\n\n"
                               "- 工厂：%s（%s）\n- 系统：%s\n- 异常数据 ID：%s\n- 异常代码：%s\n"
                               "- 问题描述：%s\n- 截图：%d 张\n\n"
                               "信息正确请回复“确认提交”；需要修改请直接说明，也可以回复“取消工单”。") % (
                                   FACTORIES[factory]["name"], factory, service, trace, code, description, len(urls))
            if chat_mode and salary_missing(salary_id):
                result["reply"] = result["reply"].replace("\n\n信息正确", "\n- 提交人薪号：" + author + "\n\n信息正确")
    result["state"] = dump(s)
    return result


def redact_feedback(value):
    # Keep business error details while removing echoed credentials. The result
    # is displayed as literal text, never passed to an LLM or rendered as HTML.
    secret = r"(?:authorization|proxy-authorization|cookie|set-cookie|password|passwd|secret|client_secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token)"
    if isinstance(value, dict):
        return {str(k): "[已隐藏凭据]" if re.fullmatch(secret, str(k), re.I) else redact_feedback(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_feedback(v) for v in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_./+~=-]+", r"\1 [已隐藏凭据]", value)
    value = re.sub(r"(?i)(\b" + secret + r"\b[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}]+)",
                   r"\1[已隐藏凭据]", value)
    return value


def feedback_text(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
    value = redact_feedback(value)
    return value.strip() if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def response_feedback(text, structured, spec, error_message, error_type):
    parts = []
    if clean(error_type, 200):
        parts.append("错误类型：" + clean(error_type, 200))
    if clean(error_message, 12000):
        parts.append(feedback_text(error_message))
    body = response_body(text, structured, spec)
    if body not in (None, "", [], {}):
        parts.append(feedback_text(body))
    else:
        # A missing/wrong mapping must never hide the actual tool output.
        for value in (structured, text):
            if value not in (None, "", [], {}):
                candidate = feedback_text(value)
                if candidate not in parts:
                    parts.append(candidate)
    feedback = "\n".join(parts)
    return feedback if len(feedback) <= 4000 else feedback[:4000] + "\n[返回内容较长，后续已截断；完整内容可在本次运行日志查看]"


def literal_feedback(value):
    # A fence longer than any run in the response prevents Markdown injection.
    fence = "`" * max(3, 1 + max([len(x) for x in re.findall(r"`+", value)] or [0]))
    return fence + "text\n" + value + "\n" + fence


def submitted(state, text, structured, contract, transport_error, error_message="", error_type=""):
    s = state_load(state)
    spec = contract_load(contract).get("create", {})
    body = response_body(text, structured, spec)
    status = "unknown"
    ticket_id = ""
    if not transport_error and create_contract_ready(contract):
        signal = at_path(body, spec["success_path"])
        failure = at_path(body, spec.get("failure_path") or spec["success_path"])
        if exact_match(failure, spec.get("failure_values", [])):
            status = "failed"
        elif (exact_match(signal, spec["success_values"])
              and all(exact_match(at_path(body, check["path"]), check["values"])
                      for check in spec.get("success_checks", []))):
            status = "success"
            candidate = at_path(body, spec.get("ticket_id_path", "")) if spec.get("ticket_id_path") else None
            if type(candidate) in (str, int):
                ticket_id = str(candidate)[:200]
    feedback = response_feedback(text, structured, spec, error_message, error_type)
    s["submission"].update(status=status, ticket_id=ticket_id, feedback=feedback if status != "success" else "")
    s["history"] = (s["history"] + [dict(s["submission"])])[-10:]
    if status == "success":
        s["metrics"]["ticket_successes"] += 1
        s.update(mode="query", preview="", draft={}, uploads={})
        reply = "工单已提交成功" + ("，工单号：" + ticket_id if ticket_id else "") + "。"
    elif status == "failed":
        s.update(mode="ticket", preview="")
        reply = "工单提交失败。接口返回：\n\n" + literal_feedback(feedback or "接口返回失败状态，但未提供具体报错信息。")
        reply += "\n\n草稿和附件已保留。你可以按报错修改信息，或回复“重试提交”重新查看确认清单。"
    else:
        reply = "工单提交时接口调用报错：" if transport_error else "接口已返回，但当前无法确认是否建单成功。接口原始返回："
        reply += "\n\n" + literal_feedback(feedback or "未收到可读取的接口返回或具体错误信息。")
        reply += "\n\n草稿和附件已保留，提交结果尚待核实，不会自动重试。请联系 IT 支持核对追踪 ID：" + s["submission"].get("traceId", "")
    return {"state": dump(s), "reply": reply, "status": status}
