# Этап 1 прогона — сортировка (дешёвая модель, config.ini [stage:triage]).
# run/items.json (fetch.py) -> run/triage.json (вердикт на каждый материал)
#   + run/select_input.md (вход отбора: кандидаты целиком, отсеянное — одной строкой).
# Код выхода: 0 — нужен отбор; 4 — кандидатов нет, run/changes.json уже записан
# (finish.py обновит страницу, отбор и рерайт не вызываются).
# Сортировка не справилась — отбор получает весь вход (run/input.md), как в версии на Claude Code.
import html
import json
import sys
from pathlib import Path

import llm

BASE = Path(__file__).resolve().parent
RUN = BASE / "run"
PREVIEW = 500  # символов текста материала для сортировки
GONE_CHARS = 300  # начало текста отсеянного во входе отбора: по одному заголовку ошибку сортировки не видно

# Сортировка отсеивает только проверяемое по фактам: дубли и рекламу. «Не Сызрань», «не новость»,
# «устарело» решает отбор по полному тексту — дешёвая модель выбрасывала нужные новости (30.10.2026)
VERDICTS = ["candidate", "update", "dup_site", "dup_proposal", "dup_item", "ad"]
WHY = {"dup_site": "Уже на сайте", "dup_proposal": "Уже предложено", "dup_item": "Пересказ", "ad": "Реклама"}
STR = {"type": "string"}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items", "taken"], "properties": {
    "items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                         "required": ["n", "verdict", "ref", "why"],
                                         "properties": {"n": {"type": "integer"},
                                                        "verdict": {"type": "string", "enum": VERDICTS},
                                                        "ref": STR, "why": STR}}},
    "taken": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                         "required": ["id", "news", "why"],
                                         "properties": {"id": STR, "news": STR, "why": STR}}}}}


def item_text(i, chars):
    # dup — подсказки dupcheck «↳ ПОХОЖЕ НА САЙТЕ» (fetch.py); в превью не обрезаются
    return f"### #{i['n']} [{i['kind']}] {i['source']} | {i['when']} | {i['title']}\n{i['url']}\n{i.get('dup', '')}{i['text'][:chars]}\n"


def main():
    inp = json.loads((RUN / "items.json").read_text(encoding="utf-8"))
    items = inp["items"]
    prompt = "\n".join(["# Материалы\n"] + [item_text(i, PREVIEW) for i in items] + [""] + inp["site"] + [""] + inp["props"])
    st = llm.stage("triage")
    try:
        if not items and not inp.get("site_new"):
            out = {"items": [], "taken": []}
        else:  # и без материалов, если на сайте новое: сверить, что из наших предложений взято
            out, rec = llm.ask(st, (BASE / "triage.md").read_text(encoding="utf-8"), prompt, SCHEMA, "triage")
            print(f"сортировка: {st.model}, {rec['ms'] / 1000:.0f} с, {rec['rub']} ₽")
    except Exception as e:  # noqa: BLE001 — без сортировки отбор получает всё
        print(f"сортировка не удалась ({e}) — отбор получит весь вход")
        (RUN / "select_input.md").write_text((RUN / "input.md").read_text(encoding="utf-8"), encoding="utf-8")
        return 0

    by_n = {v["n"]: v for v in out["items"]}
    missing = [i["n"] for i in items if i["n"] not in by_n]
    for n in missing:  # не размечено — пусть смотрит отбор
        by_n[n] = {"n": n, "verdict": "candidate", "ref": "", "why": "сортировка пропустила"}
    keep = [i for i in items if by_n[i["n"]]["verdict"] in ("candidate", "update")]
    gone = [i for i in items if i not in keep]
    skipped = [f"<strong>{WHY[by_n[i['n']]['verdict']]}:</strong> {html.escape(i['title'])} ({html.escape(i['source'])})"
               f" — {html.escape(by_n[i['n']]['why'])}" for i in gone]
    (RUN / "triage.json").write_text(json.dumps({"model": st.model, "verdicts": list(by_n.values()),
                                                 "taken": out["taken"], "skipped_html": skipped},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    stat = {v: sum(1 for x in by_n.values() if x["verdict"] == v) for v in VERDICTS}
    print("сортировка:", ", ".join(f"{k} {v}" for k, v in stat.items() if v), "| взято сайтом:", len(out["taken"]))

    if not keep and not out["taken"]:
        (RUN / "changes.json").write_text(json.dumps({
            "latest": f"ИИ-редактор: нового для предложения нет — разобрано материалов: {len(items)}, все отсеяны "
                      f"(дубли и реклама; список внизу).",
            "log": f"ИИ-редактор: разобрано {len(items)}, кандидатов нет — отбор не вызывался",
            "drop": [], "add": [], "update": [], "skipped": skipped}, ensure_ascii=False), encoding="utf-8")
        print("кандидатов нет — отбор не нужен")
        return 4

    lines = inp["head"] + [f"## Кандидаты после сортировки ({len(keep)} из {len(items)}) — полные тексты\n"]
    for i in keep:
        v = by_n[i["n"]]
        mark = f"Сортировка: обновление к {v['ref']} — {v['why']}\n" if v["verdict"] == "update" else ""
        lines.append(item_text(i, 10 ** 6).replace("\n", "\n" + mark, 1) if mark else item_text(i, 10 ** 6))
    if out["taken"]:
        lines.append("## Сортировка считает, что сайт уже взял наши предложения (проверь по фактам)\n")
        lines += [f"- {t['id']} → {t['news']}: {t['why']}" for t in out["taken"]]
    if gone:
        lines.append("\n## Отсеяно сортировкой (дешёвая модель, ошибается; под строкой — начало текста, "
                     "полный текст по номеру — read_item)\n")
        lines += [f"- #{i['n']} {WHY[by_n[i['n']]['verdict']]} {by_n[i['n']]['ref']} | {i['source']} | {i['title']} — "
                  f"{by_n[i['n']]['why']}\n  > {' '.join((i['text'] or '').split())[:GONE_CHARS]}" for i in gone]
    lines += [""] + inp["site"] + [""] + inp["props"]
    (RUN / "select_input.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
