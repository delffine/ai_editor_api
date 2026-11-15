# Этапы 2–3 прогона: рерайт и фактчек каждой отобранной новости.
# run/selected.json (этап 1, отбор) + run/sources.json (полные тексты входа, fetch.py)
#   -> run/changes.json (формат finish.py) + run/stages.json (вердикты, время, стоимость).
# Рерайт, фактчек и правка — отдельные запросы к API (llm.ask, модели — config.ini
# [stage:rewrite], [stage:factcheck], [stage:fix]) со структурированным ответом:
# фактчекер видит только материал и готовый текст (чистый контекст).
# Взято из ai_editor (server/runner/stages.py); фактура дополняется run/fetched.json
# (страницы, прочитанные отбором через fetch_url).
# Правила: rewrite_rules.md (общие с «Рерайтом (ИИ)» админки) + cats/tags/fotos.md,
# factcheck_rules.md, fix_rules.md — исходники в server/rewrite/. Схема с 16.10.2026.
import datetime as dt
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import llm
from textutil import fix_quotes

BASE = Path(__file__).resolve().parent
RUN, STATE = BASE / "run", BASE / "state"
SAM = dt.timezone(dt.timedelta(hours=4))
CFG = llm.config()
WORKERS = int(CFG["run"].get("stage_workers", "3"))
ROUNDS = int(CFG["run"].get("rounds", "3"))  # проверок фактчекера; между ними — точечная правка по замечаниям
ST = {n: llm.Stage(n, CFG) for n in ("rewrite", "factcheck", "fix")}
SOURCE_MAX = 6000  # символов на один источник


def schema(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props),
            "additionalProperties": False}


STR = {"type": "string"}
REWRITE_SCHEMA = schema({"titles": {"type": "array", "items": {"type": "string", "minLength": 1},
                                    "minItems": 3, "maxItems": 3},
                         "lead": STR, "body": STR, "notes": STR, "cat": {"type": "integer"},
                         "tags": {"type": "array", "items": STR, "maxItems": 3}, "foto": STR})
EDIT_SCHEMA = schema({"edits": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["where", "old", "new"],
    "properties": {"where": {"type": "string", "enum": ["title1", "title2", "title3", "lead", "body"]},
                   "old": STR, "new": STR}}}})
FACTCHECK_SCHEMA = schema({
    "verdict": {"type": "string", "enum": ["ok", "fix", "stop"]},
    "issues": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                          "required": ["kind", "quote", "source", "fix"],
                                          "properties": {"kind": STR, "quote": STR, "source": STR, "fix": STR}}}})


SYSTEM = {}  # имя файла -> текст системного промпта (читается один раз)


def call(stage, system_file, prompt, sch):
    """Запрос к модели этапа -> (dict по схеме, ₽, мс)."""
    name = Path(system_file).name
    if name not in SYSTEM:
        SYSTEM[name] = Path(system_file).read_text(encoding="utf-8")
    out, rec = llm.ask(ST[stage], SYSTEM[name], prompt, sch, stage)
    return out, rec["rub"], rec["ms"]


def norm_url(u):
    return re.sub(r"^https?://(www\.)?", "", (u or "").strip()).rstrip("/")


def material_of(item, sources):
    """Фактура для рерайта и фактчека: источники (текст из входа по url или выдержки
    этапа отбора) + контекст. Возвращает (текст, предупреждения)."""
    parts, warn = [], []
    for i, s in enumerate(item.get("sources") or [], 1):
        src = sources.get(norm_url(s.get("url")))
        text = (s.get("text") or "").strip() or (src or {}).get("text", "")
        if not text:
            warn.append(f"нет текста источника {s.get('url')}")
            continue
        head = f"Источник {i}{' (главный)' if i == 1 else ''}: {s.get('name') or (src or {}).get('name', '')}"
        if src and src.get("published"):
            head += f" | опубликовано {src['published']}"
        title = (src or {}).get("title", "")
        parts.append(f"{head} | {s.get('url')}\n{title + chr(10) if title else ''}{text[:SOURCE_MAX]}")
    if (item.get("context") or "").strip():
        parts.append(f"Контекст редактора: {item['context'].strip()}")
    return "\n\n".join(parts), warn


def clean(v):
    return fix_quotes(str(v or "").strip())


def as_text(r):
    return "\n".join(f"Заголовок {i}: {t}" for i, t in enumerate(r["titles"], 1)) + \
        f"\n\nЛид: {r['lead']}\n\nТекст: {r['body'] or '(нет)'}"


def issues_text(issues):
    return "\n".join(f"{n}. [{x['kind']}] «{x['quote']}» — в материале: {x['source']}; правка: {x['fix']}"
                     for n, x in enumerate(issues, 1))


def apply_edits(r, edits):
    """Точечные замены правщика; текст вне замен не меняется, так что правка не вносит
    новых ошибок в другие места (16.10.2026: при переписывании целиком появлялись новые
    выдумки). Пустой new для заголовка — вариант выбрасывается. Возвращает число применённых."""
    n = 0
    for e in edits:
        old, new, where = e["old"], clean(e["new"]), e["where"]
        if where.startswith("title"):
            i = int(where[-1]) - 1
            if i < len(r["titles"]) and old and old in r["titles"][i]:
                r["titles"][i] = r["titles"][i].replace(old, new, 1).strip() if new else ""
                n += 1
        elif old and old in r[where]:
            r[where] = re.sub(r"  +", " ", r[where].replace(old, new, 1)).strip()
            n += 1
    r["titles"] = [t for t in r["titles"] if t]
    return n


def drop_flagged_titles(r, issues):
    """Остались замечания только к вариантам заголовка — выбросить эти варианты (их три,
    хоть один должен остаться). Возвращает замечания, которые остались в силе."""
    bad = [t for t in r["titles"] if any(x["quote"] and x["quote"] in t for x in issues)]
    if bad and len(bad) < len(r["titles"]):
        r["titles"] = [t for t in r["titles"] if t not in bad]
        issues = [x for x in issues if not any(x["quote"] and x["quote"] in t for t in bad)]
    return issues


def process(item, sources, kind, prev=None):
    """Рерайт -> фактчек -> (правка -> фактчек). Возвращает поля для changes.json и статистику."""
    stat = {"id": item["id"], "kind": kind, "cost": 0.0, "ms": 0, "checks": []}
    material, warn = material_of(item, sources)
    if prev:  # update: прежняя версия нашего предложения — тоже материал (проверена раньше)
        material = (prev.get("material") or f"Прежняя версия новости (проверена ранее):\n{as_text({'titles': prev['headlines'], 'lead': prev['lead'], 'body': prev.get('body', '')})}") \
            + "\n\nНОВЫЕ МАТЕРИАЛЫ\n\n" + material
    stat["warn"] = warn
    if not material.strip():
        return None, stat | {"error": "пустая фактура"}
    task = (f"Обнови новость по новым материалам: собери одну новость по состоянию на сейчас.\n\n" if prev else "") + \
        f"Сейчас {dt.datetime.now(SAM):%d.%m.%Y %H:%M} (Самара).\nО чём новость: {item.get('topic', '')}\n\nМАТЕРИАЛ\n\n{material}"
    out, cost, ms = call("rewrite", RUN / "rewrite_system.md", task, REWRITE_SCHEMA)
    stat["cost"] += cost
    stat["ms"] += ms
    # markdown по краям заголовка (aliceai-llm: «**Заголовок**») — снять
    titles = [re.sub(r"^(\*\*|__)\s*|\s*(\*\*|__)$", "", clean(t)).strip() for t in out["titles"]]
    r = {"titles": [t for t in titles if t][:3], "lead": clean(out["lead"]),
         "body": clean(out["body"]), "notes": clean(out["notes"]), "cat": out.get("cat") or 0,
         "tags": out.get("tags") or [], "foto": (out.get("foto") or "").strip()}
    fc, failed = None, ""
    for rnd in range(ROUNDS):
        # сбой фактчека/правки не губит готовый рерайт (08.11.2026: 5 текстов пропали из-за 502
        # бесплатного DeepSeek) — новость идёт с последней версией текста и пометкой
        try:
            fc_new, cost, ms = call("factcheck", BASE / "factcheck_rules.md",
                                    f"МАТЕРИАЛ\n\n{material}\n\nНОВОСТЬ ДЛЯ ПРОВЕРКИ\n\n{as_text(r)}", FACTCHECK_SCHEMA)
        except Exception as e:  # noqa: BLE001
            failed = f"фактчек не выполнен ({type(e).__name__}: {str(e)[:200]})"
            break
        fc = fc_new
        stat["cost"] += cost
        stat["ms"] += ms
        stat["checks"].append({"verdict": fc["verdict"], "issues": fc["issues"]})
        if fc["verdict"] == "ok" or rnd == ROUNDS - 1:
            break
        # правка — точечными заменами (текст целиком не переписывается)
        try:
            ed, cost, ms = call("fix", BASE / "fix_rules.md", f"МАТЕРИАЛ\n\n{material}\n\nНОВОСТЬ\n\n{as_text(r)}"
                                f"\n\nЗАМЕЧАНИЯ ФАКТЧЕКЕРА\n\n{issues_text(fc['issues'])}", EDIT_SCHEMA)
        except Exception as e:  # noqa: BLE001 — замечания последней проверки остаются в силе
            failed = f"правка по замечаниям не выполнена ({type(e).__name__}: {str(e)[:200]})"
            break
        stat["cost"] += cost
        stat["ms"] += ms
        stat["checks"][-1]["edits"] = f"{apply_edits(r, ed['edits'])}/{len(ed['edits'])}"
    if failed:
        stat["error"] = failed
    if fc is None:  # ни одной проверки
        fc = {"verdict": "none", "issues": []}
    if fc["verdict"] == "fix":
        fc["issues"] = drop_flagged_titles(r, fc["issues"])
        if not fc["issues"]:
            fc["verdict"] = "ok"
            stat["checks"][-1]["titles_dropped"] = True
    note = " ".join(x for x in [item.get("note", ""), r["notes"] and f"Рерайт: {r['notes']}"] if x)
    if failed:
        note = (note + " " if note else "") + f"⚠ ИИ-редактор: {failed} — текст проверить вручную."
    if fc["verdict"] not in ("ok", "none"):
        note = (note + " " if note else "") + f"⚠ Фактчек ({fc['verdict']}" + \
            (", до последней правки" if failed and "edits" in stat["checks"][-1] else "") + "): " + \
            "; ".join(f"[{x['kind']}] «{x['quote']}» — {x['source']}" for x in fc["issues"])
    if warn:
        note = (note + " " if note else "") + "⚠ " + "; ".join(warn)
    res = {"headlines": r["titles"], "lead": r["lead"], "body": r["body"], "cat": r["cat"], "tags": r["tags"],
           "foto": r["foto"], "note": note, "material": material,
           "factcheck": {"verdict": fc["verdict"], "rounds": len(stat["checks"]), "issues": fc["issues"]}}
    return res, stat


def load_selected(path):
    """Частый сбой — неэкранированная кавычка внутри текста: экранируем ту, на которой
    споткнулся разбор, и пробуем снова (как load_changes в finish.py)."""
    s = path.read_text(encoding="utf-8")
    for _ in range(50):
        try:
            return json.loads(s)
        except json.JSONDecodeError as e:
            q = s.rfind('"', 0, e.pos)
            if q <= 0 or s[q - 1] == "\\" or "delimiter" not in e.msg:
                raise
            s = s[:q] + '\\"' + s[q + 1:]
    raise ValueError("selected.json: слишком много ошибок")


def main():
    sel = load_selected(RUN / "selected.json")
    sources = {norm_url(k): v for k, v in json.loads((RUN / "sources.json").read_text(encoding="utf-8")).items()} \
        if (RUN / "sources.json").exists() else {}
    fetched = RUN / "fetched.json"  # страницы, прочитанные отбором: url без выдержек -> полный текст
    if fetched.exists():
        for u, v in json.loads(fetched.read_text(encoding="utf-8")).items():
            sources.setdefault(norm_url(u), {"name": "", "title": v["title"], "text": v["text"], "published": ""})
    props = {p["id"]: p for p in json.loads((STATE / "proposals.json").read_text(encoding="utf-8"))} \
        if (STATE / "proposals.json").exists() else {}
    (RUN / "rewrite_system.md").write_text("\n\n".join(
        (BASE / f).read_text(encoding="utf-8") for f in ("rewrite_rules.md", "cats.md", "tags.md", "fotos.md")), encoding="utf-8")

    jobs = [(a, "add", None) for a in sel.get("add", [])] + \
           [(u, "update", props[u["id"]]) for u in sel.get("update", []) if u["id"] in props]
    t0 = time.time()

    def run(job):
        item, kind, prev = job
        try:
            return job, *process(item, sources, kind, prev)
        except Exception as e:  # noqa: BLE001 — сбой одной новости не валит прогон
            return job, None, {"id": item["id"], "kind": kind, "error": f"{type(e).__name__}: {e}"[:300]}

    # первая новость — одна: её вызовы кладут системные промпты в кэш (Claude), остальные читают оттуда
    results = [run(j) for j in jobs[:1]]
    with ThreadPoolExecutor(WORKERS) as ex:
        results += list(ex.map(run, jobs[1:]))

    ch = {k: sel.get(k) for k in ("latest", "log", "drop", "skipped")}
    ch["add"], ch["update"] = [], []
    for (item, kind, prev), res, stat in results:
        if res is None:  # рерайт не удался — предложение всё равно показываем, с пометкой
            res = {"headlines": [item.get("topic") or item["id"]], "lead": item.get("topic", ""), "body": "",
                   "note": f"⚠ ИИ-редактор не смог написать текст ({stat.get('error')}). {item.get('note', '')}".strip()}
        if kind == "add":
            ch["add"].append({k: item.get(k) for k in ("id", "block", "label", "score", "score_why", "sources_html",
                                                         "expires")} | res)
        else:
            ch["update"].append({"id": item["id"], "refresh": bool(item.get("refresh"))} | res)
    (RUN / "changes.json").write_text(json.dumps(ch, ensure_ascii=False, indent=1), encoding="utf-8")
    stats = [s for _, _, s in results]
    # по учёту llm, а не по stat: стоимость рерайта новости, у которой потом упал этап, тоже в итоге
    total = llm.cost()
    (RUN / "stages.json").write_text(json.dumps({"sec": round(time.time() - t0), "cost": total, "items": stats,
                                                 "usage": llm.USAGE},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    # «cost_rub N» — суммирует cost.py вместе с остальными этапами
    print(f"рерайт+фактчек: {len(jobs)} новостей за {time.time() - t0:.0f} с, cost_rub {total};",
          ", ".join(f"{s['id']}: " + " ".join(x for x in ('/'.join(c['verdict'] for c in s.get('checks', [])),
                                                           s.get('error') or '') if x) for s in stats))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except llm.LLMError as e:  # причина — на страницу (finish.py)
        (RUN / "error.txt").write_text(str(e)[:300], encoding="utf-8")
        raise
