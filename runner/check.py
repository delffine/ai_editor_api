#!/usr/bin/env python3
# Проверка настройки (ничего не публикует, state/ не трогает):
#   python3 check.py          — каждый этап config.ini: модель отвечает, JSON по схеме, кэш;
#                               у select — ещё вызов инструмента; поиск Яндекса; лента сборщика
#   python3 check.py models   — модели, доступные ключу (GET /v1/models)
#   python3 check.py cache    — кэш отбора в боевом виде (5 запросов к Claude, 5–35 ₽)
# Ключи берутся из ~/.config/ai-editor-api.env, если их нет в окружении.
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import llm
import tools

ENV = Path.home() / ".config" / "ai-editor-api.env"
STAGES = ("triage", "select", "rewrite", "factcheck", "fix")


def load_env():
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def models():
    c = llm.config()
    for sec in c.sections():
        if not sec.startswith("provider:"):
            continue
        try:
            p = llm.Provider(sec.split(":", 1)[1], c)
            req = urllib.request.Request(p.base_url + "/models", headers=p.headers(llm._env(p.key_env, "ключа")))
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:  # noqa: BLE001 — нет ключа у запасного провайдера и т.п.
            print(f"{sec}: {e}")
            continue
        print(f"{sec}: {len(data.get('data', []))} моделей")
        for m in sorted(x["id"] for x in data.get("data", [])):
            print("  ", m)


def cache_test():
    """Кэш отбора в боевом виде: правила + select.md + все инструменты + вход последнего прогона.
    A — метка только на системе, два одинаковых запроса; B — метка и на последнем сообщении,
    диалог растёт, как в цикле (последнее сообщение — ответ инструмента). ~5 запросов, 5–35 ₽."""
    import selection as sel
    base = Path(__file__).parent
    st = llm.stage("select")
    st.max_tokens = 400
    inp = base / "run" / ("select_input.md" if (base / "run" / "select_input.md").exists() else "input.md")
    system = (base / "rules.md").read_text(encoding="utf-8") + "\n\n---\n\n# ЗАДАНИЕ\n\n" + \
        (base / "select.md").read_text(encoding="utf-8")
    user = inp.read_text(encoding="utf-8") + "\n\n(Это тест кэша: ответь одним словом «готов», инструменты не вызывай.)"
    page = (base / "rules.md").read_text(encoding="utf-8")
    msgs = [llm.system_message(st, system), {"role": "user", "content": user}]

    def step(label, m, tail):
        st.cache_tail = tail
        _, r = llm.chat(st, m, tools=sel.TOOLS)
        print(f"{label:34} вход {r['in']:6}  чтение {r['cached']:6}  запись {r['cache_write']:6}  {r['rub']:6.2f} ₽")
        time.sleep(2)

    def grow(m, i):  # шаг цикла: модель вызвала fetch_url, наш код вернул страницу
        call = {"id": f"call_test{i}", "type": "function",
                "function": {"name": "fetch_url", "arguments": json.dumps({"url": f"https://example.ru/{i}"})}}
        return m + [{"role": "assistant", "content": None, "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": call["id"], "content": page[i * 4000:(i + 1) * 4000]}]

    print(f"вход: {inp.name}, модель {st.model}, prompt_cache_key {st.cache_key or 'нет'}")
    step("A1 метка на системе", msgs, False)
    step("A2 то же ещё раз", msgs, False)
    step("B1 + метка на user", msgs, True)
    m2 = grow(msgs, 1)
    step("B2 + шаг, метка на tool", m2, True)
    step("B3 + ещё шаг, метка на tool", grow(m2, 2), True)
    print("Норма: A2 читает ≈ систему; B2 читает ≈ вход B1; B3 читает ≈ вход B2.")


def main():
    load_env()
    if sys.argv[1:] == ["models"]:
        return models()
    if sys.argv[1:] == ["cache"]:
        return cache_test()
    ok = True
    schema = {"type": "object", "additionalProperties": False, "required": ["city", "n"],
              "properties": {"city": {"type": "string"}, "n": {"type": "integer"}}}
    for name in STAGES:
        st = llm.stage(name)
        try:
            # системный промпт подлиннее — чтобы было что кэшировать (у Claude от 1024 токенов)
            system = "Отвечай строго по делу.\n" + (Path(__file__).parent / "rules.md").read_text(encoding="utf-8")[:6000]
            out, rec = llm.ask(st, system, "Какой город на Волге называют «Сызрань»? Верни city и n = 2 + 2.", schema)
            print(f"[ok] {name:9} {st.model} ({st.json}, кэш {st.cache}, prompt_cache_key "
                  f"{st.cache_key or 'нет/не принят'}): {out} | {rec['ms']} мс, вход {rec['in']}, "
                  f"кэш чтение {rec['cached']} / запись {rec['cache_write']}, {rec['rub']} ₽")
            if name == "select":
                tool = {"type": "function", "function": {"name": "fetch_url", "description": "Прочитать страницу",
                                                         "parameters": {"type": "object", "required": ["url"],
                                                                        "properties": {"url": {"type": "string"}}}}}
                msg, rec = llm.chat(st, [llm.system_message(st, system), {"role": "user", "content":
                                    "Прочитай http://www.syzran-small.ru/ инструментом."}], tools=[tool])
                calls = msg.get("tool_calls") or []
                print(f"[{'ok' if calls else '!!'}] {name:9} tool calling: "
                      f"{calls[0]['function'] if calls else 'модель не вызвала инструмент'}")
                ok &= bool(calls)
            if st.cache != "off" and st.model.startswith("claude"):
                # второй такой же запрос должен читать системный промпт из кэша
                _, rec = llm.ask(st, system, "Какой город на Волге называют «Сызрань»? Верни city и n = 2 + 2.", schema)
                print(f"[{'ok' if rec['cached'] else '!!'}] {name:9} кэш системы ({st.json}): чтение {rec['cached']}, "
                      f"запись {rec['cache_write']}")
                print(f"           usage шлюза: {json.dumps(rec['usage'], ensure_ascii=False)}")
            if st.cache_tail:
                # хвост диалога: длинное сообщение пользователя, два шага подряд — второй читает его из кэша
                long = (Path(__file__).parent / "rules.md").read_text(encoding="utf-8")[6000:16000]
                msgs = [llm.system_message(st, system), {"role": "user", "content": long}]
                _, r1 = llm.chat(st, msgs, tools=[tool])
                _, r2 = llm.chat(st, msgs + [{"role": "assistant", "content": "Понял."},
                                             {"role": "user", "content": "Ответь одним словом: да."}], tools=[tool])
                print(f"[{'ok' if r2['cached'] > r1['cached'] else '!!'}] {name:9} кэш хвоста: шаг 1 чтение "
                      f"{r1['cached']} / запись {r1['cache_write']}; шаг 2 чтение {r2['cached']} / запись {r2['cache_write']}"
                      f"{'' if st.cache_tail else ' — шлюз не принял, отключён'}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[!!] {name:9} {st.model}: {e}")
    s = tools.web_search("Сызрань новости", dict(llm.config()["search"]))
    print(f"[{'!!' if s.startswith('ОШИБКА') else 'ok'}] поиск: {s[:300]}")
    feed = llm.config()["run"].get("feed") or os.environ.get("AI_EDITOR_FEED_URL", "")
    try:
        raw = urllib.request.urlopen(feed, timeout=30).read() if feed.startswith(("http://", "https://")) else Path(feed).read_bytes()
        d = json.loads(raw)
        print(f"[ok] лента сборщика: {len(d['items'])} материалов, сформирована {d['generated_at']}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"[!!] лента сборщика ({feed or 'не задана'}): {e}")
    web = Path(llm.config()["run"]["web_dir"])
    print(f"[{'ok' if os.access(web if web.exists() else web.parent, os.W_OK) else '!!'}] запись страницы в {web}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
