#!/usr/bin/env python3
# Расход в рублях:
#   python3 cost.py --run          — итог текущего прогона (run/usage.jsonl), копит его в logs/usage_ГГГГ-ММ-ДД.jsonl
#   python3 cost.py [ГГГГ-ММ-ДД]   — за день по этапам и моделям (по умолчанию сегодня)
#   python3 cost.py --fact [дата]  — пересчитать день по истории PlusVibe (для записей без факта)
# ₽ в записи — сначала оценка шлюза (x-pv-cost-rub / cost_rub) или по price. Для PlusVibe --run
# заменяет её фактическим списанием из GET /v1/generations (как в ЛК); оценка остаётся в rub_est,
# у сверенных — "fact": true. Сверка — по request_id (llm.py пишет его в req), у старых записей —
# по модели и токенам. История недоступна — остаётся оценка (на 09.11.2026 завышала Sonnet до ×1,69).
import datetime as dt
import json
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(path):
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def table(recs):
    by = defaultdict(lambda: [0, 0, 0, 0, 0.0, 0])
    for r in recs:
        s = by[(r["stage"], r["model"])]
        s[0] += 1
        s[1] += r["in"]
        s[2] += r["cached"]
        s[3] += r["out"]
        s[4] += r["rub"]
        s[5] += r.get("cache_write", 0)
    for (stage, model), (n, i, c, o, rub, w) in sorted(by.items()):
        print(f"  {stage:9} {model:20} запросов {n:3}  вход {i:7} (кэш {c:7}, запись {w:6})  выход {o:6}  {rub:7.2f} ₽")
    fact = sum(1 for r in recs if r.get("fact"))
    est = sum(r.get("rub_est", r["rub"]) for r in recs)
    print(f"  итого {sum(r['rub'] for r in recs):.2f} ₽" +
          (f" (факт PlusVibe у {fact} из {len(recs)}; оценка шлюза была {est:.2f} ₽)" if fact else ""))


def plusvibe():
    """(base_url, ключ) PlusVibe из config.ini и окружения; нет — None."""
    try:
        from llm import config
        p = config()["provider:plusvibe"]
        key = os.environ.get(p.get("key_env", "PLUSVIBE_API_KEY"), "")
        return (p["base_url"].rstrip("/"), key) if key else None
    except Exception:  # noqa: BLE001 — учёт не должен ронять прогон
        return None


def generations(since):
    """Записи истории PlusVibe с момента since (UTC, ISO); недоступно — []."""
    pv = plusvibe()
    if not pv:
        return []
    out, page = [], 1
    while page <= 30:
        url = f"{pv[0]}/generations?page={page}&limit=100&dateFrom={since}"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + pv[1], "User-Agent": "ai-editor-api"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except (OSError, ValueError) as e:
            print("история PlusVibe недоступна:", e)
            return out
        out += d.get("data") or []
        if not d.get("data") or page * int(d.get("limit") or 100) >= int(d.get("total") or 0):
            return out
        page += 1
    return out


def reconcile(recs):
    """Заменить оценку ₽ фактом из истории PlusVibe. Возвращает число сверенных записей."""
    todo = [r for r in recs if not r.get("fact")]
    if not todo:
        return 0
    first = min(dt.datetime.strptime(r["t"], "%Y-%m-%d %H:%M:%S") for r in todo)  # местное время сервера (TZ прогона)
    # запас 2 ч: при ручном запуске без TZ=Europe/Samara время записей сдвинуто на час
    since = (first.astimezone(dt.timezone.utc) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pv = plusvibe()
    prefix = pv[1][:12] if pv else ""  # история общая на аккаунт — только запросы нашего ключа
    gens = [g for g in generations(since) if g.get("cost_rub") is not None and g.get("status", "completed") == "completed"
            and (not g.get("api_key_prefix") or prefix.startswith(g["api_key_prefix"]) or g["api_key_prefix"].startswith(prefix))]
    by_req = {g["request_id"]: g for g in gens if g.get("request_id")}
    by_tok = defaultdict(list)
    for g in gens:
        by_tok[(g["prompt_tokens"], g["completion_tokens"])].append(g)
    used, n = set(), 0
    for r in todo:
        g = by_req.get(r.get("req"))
        if g is None and not r.get("req"):  # старые записи без request_id — по токенам
            g = next((x for x in by_tok.get((r["in"], r["out"]), []) if x["id"] not in used), None)
        if g is None or g["id"] in used:
            continue
        used.add(g["id"])
        r["rub_est"], r["rub"], r["fact"], r["route"] = r["rub"], round(float(g["cost_rub"]), 6), True, g.get("route")
        n += 1
    return n


def main():
    args = sys.argv[1:]
    if args == ["--run"]:
        recs = read(BASE / "run" / "usage.jsonl")
        if recs:
            reconcile(recs)
            print("стоимость прогона:")
            table(recs)
            (BASE / "logs").mkdir(exist_ok=True)
            with open(BASE / "logs" / f"usage_{dt.date.today():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
                f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
        return
    if args[:1] == ["--fact"]:
        day = args[1] if len(args) > 1 else f"{dt.date.today():%Y-%m-%d}"
        path = BASE / "logs" / f"usage_{day}.jsonl"
        recs = read(path)
        n = reconcile(recs)
        if n:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
            tmp.replace(path)
        print(f"{day}: сверено с историей PlusVibe {n}")
        table(recs)
        return
    day = args[0] if args else f"{dt.date.today():%Y-%m-%d}"
    recs = read(BASE / "logs" / f"usage_{day}.jsonl")
    print(f"{day}:")
    table(recs)


if __name__ == "__main__":
    main()
