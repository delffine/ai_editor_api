# Этап 2 прогона — отбор (config.ini [stage:select]): свой агентский цикл с function calling
# вместо Claude Code. Модель просит инструменты (tools.py), наш код их исполняет; итог —
# вызов submit_selection, проверенный по схеме, -> run/selected.json (формат как в ai_editor).
# Система — rules.md + select.md (неизменный префикс, кэш 1h у Claude), вход — run/select_input.md.
# Диалог целиком — run/select_log.json (разбор прогонов).
import json
import sys
import time
from pathlib import Path

import llm
import tools

BASE = Path(__file__).resolve().parent
RUN = BASE / "run"

STR = {"type": "string"}
SOURCE = {"type": "object", "required": ["name", "url", "text"], "properties": {"name": STR, "url": STR, "text": STR}}
SELECTION = {"type": "object", "required": ["latest", "log", "drop", "add", "update", "skipped"], "properties": {
    "latest": STR, "log": STR,
    "drop": {"type": "array", "items": {"type": "object", "required": ["id"],
                                        "properties": {"id": STR, "taken_as": STR, "reason": STR}}},
    "add": {"type": "array", "items": {"type": "object", "required": [
        "id", "block", "label", "topic", "sources", "context", "score", "score_why", "sources_html", "note", "expires"],
        "properties": {"id": STR, "block": {"type": "string", "enum": ["urgent", "queue", "reserve"]}, "label": STR,
                       "topic": STR, "sources": {"type": "array", "items": SOURCE, "minItems": 1}, "context": STR,
                       "score": {"type": "number"}, "score_why": STR, "sources_html": STR, "note": STR,
                       "expires": {"type": ["string", "null"]}}}},
    "update": {"type": "array", "items": {"type": "object", "required": ["id", "topic", "sources", "context", "refresh"],
                                          "properties": {"id": STR, "topic": STR, "context": STR,
                                                         "sources": {"type": "array", "items": SOURCE},
                                                         "refresh": {"type": "boolean"}}}},
    "skipped": {"type": "array", "items": STR}}}


def fn(name, desc, props, required=None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required if required is not None else list(props)}}}


TOOLS = [
    fn("fetch_url", "Прочитать страницу (http/https) как текст. find — слово или фраза: вернуть фрагменты вокруг "
                    "(для длинных страниц). Независимые адреса — несколькими вызовами в одном ответе.",
       {"url": STR, "find": STR}, ["url"]),
    fn("web_search", "Поиск Яндекса: заголовки, адреса, фрагменты. Платный, лимит на прогон.", {"query": STR}),
    fn("read_item", "Полный текст материала входа по номеру #n.", {"n": {"type": "integer"}}),
    fn("read_proposals", "Полные поля наших предложений по id (для update).",
       {"ids": {"type": "array", "items": STR}}),
    fn("submit_selection", "Итог прогона (один раз, в конце). Поля — по описанию формата в задании.",
       SELECTION["properties"], SELECTION["required"]),
]


def main():
    st = llm.stage("select")
    max_steps, max_fetches = int(st.opt.get("max_steps", "24")), int(st.opt.get("max_fetches", "25"))
    max_searches, chars = int(st.opt.get("max_searches", "5")), int(st.opt.get("fetch_chars", "6000"))
    search_cfg = dict(llm.config()["search"])
    items = {i["n"]: i for i in json.loads((RUN / "items.json").read_text(encoding="utf-8"))["items"]}
    system = (BASE / "rules.md").read_text(encoding="utf-8") + "\n\n---\n\n# ЗАДАНИЕ\n\n" + \
        (BASE / "select.md").read_text(encoding="utf-8")
    msgs = [llm.system_message(st, system),
            {"role": "user", "content": (RUN / "select_input.md").read_text(encoding="utf-8")}]
    n_fetch = n_search = 0
    result, t0 = None, time.time()

    def run_tool(name, a):
        nonlocal n_fetch, n_search
        if name == "fetch_url":
            n_fetch += 1
            if n_fetch > max_fetches:
                return f"Лимит обращений к сети ({max_fetches}) исчерпан — заверши по тому, что есть."
            return tools.fetch_url(a.get("url", ""), a.get("find") or "", chars) + \
                f"\n\n[обращений к сети: {n_fetch} из {max_fetches}]"
        if name == "web_search":
            n_search += 1
            if n_search > max_searches:
                return f"Лимит поиска ({max_searches}) исчерпан."
            return tools.web_search(a.get("query", ""), search_cfg) + f"\n\n[поисков: {n_search} из {max_searches}]"
        if name == "read_item":
            i = items.get(int(a.get("n") or 0))
            return f"{i['source']} | {i['when']} | {i['title']}\n{i['url']}\n{i['text']}" if i else "нет такого номера"
        if name == "read_proposals":
            return tools.read_proposals(a.get("ids") or [])
        return f"нет инструмента {name}"

    for step in range(1, max_steps + 1):
        last = step == max_steps
        choice = {"type": "function", "function": {"name": "submit_selection"}} if last else None
        msg, rec = llm.chat(st, msgs, tools=TOOLS, tool_choice=choice)
        calls = msg.get("tool_calls") or []
        msgs.append({"role": "assistant", "content": msg.get("content") or (None if calls else ""),
                     **({"tool_calls": calls} if calls else {})})
        print(f"шаг {step}: {', '.join(c['function']['name'] for c in calls) or 'текст'} | вход {rec['in']} "
              f"(кэш {rec['cached']}, запись {rec['cache_write']}) выход {rec['out']} | {rec['rub']} ₽")
        if not calls:
            msgs.append({"role": "user", "content": "Продолжай через инструменты; итог — вызовом submit_selection."})
            continue
        for c in calls:
            name = c["function"]["name"]
            try:
                a = json.loads(c["function"].get("arguments") or "{}")
            except ValueError as e:
                out = f"ОШИБКА: аргументы — не JSON ({e}); повтори вызов"
            else:
                if name == "submit_selection":
                    errs = llm.check(a, SELECTION)
                    out = "Принято." if not errs else "Не принято, исправь и вызови снова: " + "; ".join(errs[:15])
                    if not errs:
                        result = a
                else:
                    out = run_tool(name, a)
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": out})
        if result:
            break

    tools.save_fetched()
    (RUN / "select_log.json").write_text(json.dumps(msgs[1:], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"отбор: {st.model}, шагов {step}, сеть {n_fetch}, поиск {n_search}, {time.time() - t0:.0f} с, "
          f"cost_rub {llm.cost('select')}")
    if not result:
        print("отбор не сдал итог")
        return 1
    triage = RUN / "triage.json"
    if triage.exists():  # отсеянное сортировкой — тоже в «Отсеяно» на странице
        result["skipped"] += json.loads(triage.read_text(encoding="utf-8")).get("skipped_html", [])
    for a in result["add"]:
        a["expires"] = a.get("expires") or None
    (RUN / "selected.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except llm.LLMError as e:  # причина — на страницу (finish.py)
        (RUN / "error.txt").write_text(str(e)[:300], encoding="utf-8")
        raise
