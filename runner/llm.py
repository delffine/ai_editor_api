# Клиент OpenAI-совместимого API (/chat/completions) для всех этапов прогона.
# Только stdlib. Настройки этапов — config.ini (stage(name)), ключи — из окружения
# (run.sh загружает ~/.config/ai-editor-api.env).
#
#   st = stage("rewrite")
#   data, msg = ask(st, system, prompt, schema=REWRITE_SCHEMA)   # один запрос -> dict по схеме
#   msg = chat(st, messages, tools=TOOLS)                         # шаг агентского цикла
#
# Провайдеры — [provider:…] в config.ini: kind = openai (Beget, Рег.облако) | yandex (Yandex AI Studio),
# различия — в классе Provider. Переключение всех этапов — provider в [DEFAULT].
# Учёт: каждый ответ -> USAGE (токены, кэш, ₽: cost_rub шлюза или по price) и строка в run/usage.jsonl.
import configparser
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("AI_EDITOR_CONFIG") or BASE / "config.ini")  # другой файл — для песочницы A/B
USAGE = []
_lock = threading.Lock()


class LLMError(RuntimeError):
    pass


def config():
    c = configparser.ConfigParser(inline_comment_prefixes=None, interpolation=None)
    c.read(CONFIG, encoding="utf-8")
    return c


def _env(name, what):
    v = os.environ.get(name or "", "").strip()
    if not v:
        raise LLMError(f"нет {what}: переменная {name or '(не указана в config.ini)'} не задана")
    return v


class Provider:
    """Секция [provider:…]. kind:
      openai — обычный OpenAI-совместимый шлюз (Beget, Рег.облако): Authorization: Bearer <ключ>,
               id модели как есть, рубли — cost_rub из usage (или по price).
      yandex — Yandex AI Studio: Authorization: Api-Key <ключ>, каталог — заголовок OpenAI-Project,
               id модели — gpt://<каталог>/<модель>; prompt_cache_key и cache_control не шлются,
               рубли — по price (в ответе их нет)."""

    def __init__(self, name, c):
        p = c[f"provider:{name}"]
        self.name, self.kind = name, p.get("kind", "openai").strip()
        if self.kind not in ("openai", "yandex"):
            raise LLMError(f"provider:{name}: kind = {self.kind} — допустимо openai | yandex")
        self.base_url = p["base_url"].rstrip("/")
        self.key_env = p.get("key_env", "")
        self.folder_env = p.get("folder_env", "")
        self.auth = p.get("auth", "api-key" if self.kind == "yandex" else "bearer").strip()
        self.timeout = int(p.get("timeout", "240"))
        # цены, ₽ за 1 млн токенов: price.<модель> = вход [кэш [выход]]
        self.prices = {k[6:]: [float(x.replace(",", ".")) for x in v.split()]
                       for k, v in p.items() if k.startswith("price.") and v.strip()}

    def folder(self):
        return _env(self.folder_env, "каталога Yandex Cloud") if self.folder_env else ""

    def model_id(self, model):
        if self.kind == "yandex" and "://" not in model:
            return f"gpt://{self.folder()}/{model}"
        return model

    def headers(self, key):
        h = {"Authorization": ("Api-Key " if self.auth == "api-key" else "Bearer ") + key,
             "Content-Type": "application/json"}
        if self.folder_env:
            h["OpenAI-Project"] = self.folder()
        return h


class Stage:
    def __init__(self, name, c=None):
        c = c or config()
        s = c[f"stage:{name}"]
        prov = Provider(s["provider"], c)

        def opt(k, default=None):
            # параметр этапа для конкретного провайдера: model.yandex = … важнее model = …
            return s.get(f"{k}.{prov.name}", s.get(k, default))

        self.name, self.prov, self.model = name, prov, opt("model")
        self.base_url, self.timeout = prov.base_url, prov.timeout
        self.key_env = opt("key_env") or prov.key_env
        self.max_tokens = int(opt("max_tokens"))
        self.json = opt("json", "schema")
        # cache_control — только Claude и только через openai-шлюз
        claude = self.model.startswith("claude") and prov.kind == "openai"
        self.cache = opt("cache", "off") if claude else "off"
        # кэш и на последнем сообщении диалога (агентский цикл: растущий хвост читается из кэша)
        self.cache_tail = opt("cache_tail", "no") == "yes" and self.cache in ("5m", "1h")
        # prompt_cache_key — постоянный на этап: по совету поддержки Beget (30.10.2026) провайдер
        # с ним стабильнее отправляет одинаковый префикс на сервер с тем же кэшем. Нет в config.ini
        # (старый на сервере — update.sh его не трогает) — auto; пусто — не слать. Яндексу — не слать
        key = opt("cache_key", "auto").strip() if prov.kind == "openai" else ""
        self.cache_key = f"ai-editor-api-{name}" if key == "auto" else key
        self.extra = json.loads(opt("extra") or "{}")
        self.price = prov.prices.get(self.model.lower())
        # запасная модель того же провайдера: основная не ответила (HTTP-ошибка после повторов, обрыв) —
        # этап переходит на неё до конца прогона (09.11.2026: бесплатный DeepSeek PlusVibe отвечал 502)
        self.fallback = (opt("fallback") or "").strip()
        self.opt = s  # прочие параметры этапа (max_steps и т.п.)

    def use_fallback(self, failed, err):
        """Модель failed не ответила — перейти на запасную (раз за прогон). True — повторить запрос
        моделью self.model (запасной; её мог уже включить другой поток)."""
        with _lock:
            if self.model != failed:
                return True
            if not self.fallback or self.model == self.fallback:
                return False
            print(f"{self.name}: {self.model} не ответила ({str(err)[:200]}) — дальше запасная {self.fallback}")
            self.model = self.fallback
            self.price = self.prov.prices.get(self.model.lower())
            if not (self.model.startswith("claude") and self.prov.kind == "openai"):
                self.cache, self.cache_tail = "off", False
            return True

    def key(self):
        return _env(self.key_env, "ключа")

    def rub(self, u, n_in, cached, out):
        """Рубли за ответ: cost_rub шлюза, если он его отдаёт, иначе по price из config.ini."""
        if u.get("cost_rub") is not None:
            return float(u["cost_rub"])
        if not self.price:
            return 0.0
        p_in, p_cache, p_out = (self.price + self.price[-1:] * 2)[:3]
        return ((n_in - cached) * p_in + cached * p_cache + out * p_out) / 1e6


def stage(name):
    return Stage(name)


def _cc(ttl):
    return {"type": "ephemeral"} | ({"ttl": "1h"} if ttl == "1h" else {})


def _system(st, text):
    """Системный промпт; cache = 5m/1h — частью с cache_control (так кэш включается у Claude в Beget)."""
    if st.cache in ("5m", "1h") and st.model.startswith("claude"):
        return {"role": "system", "content": [{"type": "text", "text": text, "cache_control": _cc(st.cache)}]}
    return {"role": "system", "content": text}


def _tail(messages):
    """Копия диалога с cache_control на последнем сообщении (5 мин: шаги цикла идут подряд).
    Метка только на последнем — старые не копятся (у Anthropic не больше 4 точек кэша).
    Все сообщения user/tool — списком частей, с меткой и без: строка и список у шлюза дают
    разный префикс, и следующий шаг не читал кэш сообщения, помеченного на прошлом (30.10.2026)."""
    out = [dict(m, content=[{"type": "text", "text": m["content"]}])
           if m.get("role") in ("user", "tool") and isinstance(m.get("content"), str) and m["content"] else m
           for m in messages]
    # метка и на первом user (вход прогона) — постоянно: на шаге 1 она там стоит как хвостовая, и
    # если снять её на шаге 2, шлюз, похоже, отдаёт другой префикс (шаг 2 читал только систему, 30.10.2026)
    marks = [len(out) - 1] + [i for i, m in enumerate(out[:-1]) if m.get("role") == "user"][:1]
    for i in marks:
        if isinstance(out[i].get("content"), list) and out[i] is not messages[i]:
            out[i] = dict(out[i], content=[dict(out[i]["content"][0], cache_control=_cc("5m"))])
    return out


def _post(st, body):
    body = dict(body, model=st.prov.model_id(body["model"]))
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(st.base_url + "/chat/completions", data=data, headers=st.prov.headers(st.key()))
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=st.timeout) as r:
                d = json.loads(r.read())
                # PlusVibe отдаёт стоимость ответа в заголовке (оценка шлюза, ₽), не в usage. Оценка бывает
                # по цене другого маршрута (09.11.2026: 61 из 118 — по 59/294 вместо 35/174) — факт берёт
                # cost.py --run из GET /v1/generations по request_id
                rid = r.headers.get("x-plusvibe-request-id")
                if rid:
                    d["_request_id"] = rid
                c = r.headers.get("x-pv-cost-rub")
                if c and isinstance(d.get("usage"), dict) and d["usage"].get("cost_rub") is None:
                    try:
                        d["usage"]["cost_rub"] = float(c.replace(",", "."))
                    except ValueError:
                        pass
                return d
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:500]
            # 429 и 5xx — шлюз не дошёл до модели или перегружен: повторить; остальное — сразу ошибка
            if e.code != 429 and e.code < 500 or attempt == 4:
                raise LLMError(f"{st.name}/{st.model}: HTTP {e.code} {text}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 4:
                raise LLMError(f"{st.name}/{st.model}: {type(e).__name__}: {e}")
        time.sleep(6 * attempt)


def _account(st, d, ms):
    u = d.get("usage") or {}
    det = u.get("prompt_tokens_details") or {}

    def n(*v):  # Яндекс может отдать числа строками и в своих именах (inputTextTokens)
        return int(next((x for x in v if x not in (None, "")), 0))

    n_in = n(u.get("prompt_tokens"), u.get("inputTextTokens"))
    cached = n(det.get("cached_tokens"), u.get("cache_read_input_tokens"))
    out = n(u.get("completion_tokens"), u.get("completionTokens"))
    rec = {"t": time.strftime("%F %T"), "stage": st.name, "model": st.model, "ms": ms,
           "in": n_in, "cached": cached,
           "cache_write": n(det.get("cache_write_tokens"), u.get("cache_write_tokens"),
                            det.get("cache_creation_tokens"), u.get("cache_creation_input_tokens")), "out": out,
           "rub": round(st.rub(u, n_in, cached, out), 4)}
    if d.get("_request_id"):
        rec["req"] = d["_request_id"]
    with _lock:
        USAGE.append(rec)
        try:
            (BASE / "run").mkdir(exist_ok=True)
            with open(BASE / "run" / "usage.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return rec | {"usage": u}  # сырой usage — только в памяти (check.py), в файл не пишется


def chat(st, messages, tools=None, tool_choice=None, response_format=None):
    """Один запрос. Возвращает (message, запись учёта). Обрезанный ответ — ошибка."""
    tail = st.cache_tail and messages[-1].get("role") in ("user", "tool")
    body = {"model": st.model, "messages": _tail(messages) if tail else messages, "max_tokens": st.max_tokens} | st.extra
    if st.cache_key:
        body["prompt_cache_key"] = st.cache_key
    if tools:
        body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
    if response_format:
        body["response_format"] = response_format
    t0 = time.time()
    while True:
        try:
            d = _post(st, body)
            break
        except LLMError as e:
            if "HTTP 400" not in str(e):
                # 429/5xx после повторов, обрыв, 401/404 — запасная модель этапа, если задана
                if st.use_fallback(body["model"], e):
                    body["model"] = st.model
                    continue
                raise
            # шлюз (или модель за ним) не принял необязательное — дальше без него, до конца прогона
            if "prompt_cache_key" in body:
                print(f"{st.name}: prompt_cache_key не принят ({str(e)[:160]}) — отключён до конца прогона")
                st.cache_key = ""
                del body["prompt_cache_key"]
            elif tail and body["messages"] is not messages:
                print(f"{st.name}: кэш хвоста диалога не принят ({str(e)[:160]}) — отключён до конца прогона")
                st.cache_tail = False
                body["messages"] = messages
            elif st.use_fallback(body["model"], e):  # модель не приняла сам запрос (формат и т.п.)
                body["model"] = st.model
            else:
                raise
    rec = _account(st, d, int((time.time() - t0) * 1000))
    ch = (d.get("choices") or [{}])[0]
    if ch.get("finish_reason") == "length":
        # обрезанный ответ — в run/ для разбора: длинный текст или зацикливание
        dump = BASE / "run" / f"cut_{st.name}_{time.strftime('%H%M%S')}_{threading.get_ident() % 1000}.txt"
        try:
            dump.write_text((ch.get("message") or {}).get("content") or json.dumps(ch, ensure_ascii=False),
                            encoding="utf-8")
        except OSError:
            pass
        raise LLMError(f"{st.name}/{st.model}: ответ обрезан на {st.max_tokens} токенах ({dump.name}) — "
                       f"увеличить max_tokens в config.ini")
    if ch.get("finish_reason") == "content_filter":
        # модерация модели (aliceai-llm на Яндексе отказывает на обычных новостях: СВО, болезни…) —
        # дальше по этому диалогу будут одни отказы; лечится только другой моделью этапа
        raise LLMError(f"{st.name}/{st.model}: ответ заблокирован фильтром контента модели — сменить model "
                       f"этапа в config.ini ({((ch.get('message') or {}).get('content') or '')[:100]})")
    if "message" not in ch:
        raise LLMError(f"{st.name}/{st.model}: в ответе нет message: {str(d)[:300]}")
    msg = ch["message"]
    if st.prov.kind == "yandex" and msg.get("tool_calls"):
        # aliceai-llm (06.11.2026): id вызова = имя функции (два fetch_url за шаг или по шагам —
        # одинаковые id, ответы инструментов путаются) — делаем уникальными; при tool_choice
        # в content протекает служебная метка «[TOO_CALL_END]» — убираем
        for i, c in enumerate(msg["tool_calls"]):
            if not str(c.get("id", "")).startswith("call"):
                c["id"] = f"call_{rec['t'][-8:].replace(':', '')}_{threading.get_ident() % 1000}_{i}_{len(messages)}"
        content = re.sub(r"\[TOO\w*_CALL_\w*\]", "", msg.get("content") or "").strip()
        msg["content"] = content or None
    return msg, rec


def parse_json(text):
    """JSON из ответа: снимает ```json-обёртку и текст вокруг."""
    # strict=False — переносы строк внутри строк JSON (yandexgpt-5.1, 06.11.2026: «Invalid control character»)
    text = (text or "").strip()
    try:
        return json.loads(text, strict=False)
    except ValueError:
        pass
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("в ответе нет JSON")
    return json.loads(text[a:b + 1], strict=False)


def check(v, sch, path="$"):
    """Простая проверка по JSON-схеме (type, required, enum, items, min/maxItems) -> список ошибок."""
    errs, t = [], sch.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    py = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float),
          "boolean": bool, "null": type(None)}
    if types and not any(isinstance(v, py[x]) and not (x in ("integer", "number") and isinstance(v, bool))
                         for x in types):
        return [f"{path}: ожидался {'/'.join(types)}"]
    if "enum" in sch and v not in sch["enum"]:
        errs.append(f"{path}: допустимо {sch['enum']}")
    if isinstance(v, str) and len(v.strip()) < sch.get("minLength", 0):
        errs.append(f"{path}: пустая строка")  # aliceai-llm(-flash) шлёт titles ["", "", ""] (06.11.2026)
    if isinstance(v, dict):
        errs += [f"{path}.{k}: нет поля" for k in sch.get("required", []) if k not in v]
        for k, s in (sch.get("properties") or {}).items():
            if k in v:
                errs += check(v[k], s, f"{path}.{k}")
    if isinstance(v, list):
        if len(v) < sch.get("minItems", 0) or len(v) > sch.get("maxItems", 10 ** 9):
            errs.append(f"{path}: элементов {len(v)}, нужно {sch.get('minItems', 0)}–{sch.get('maxItems', '∞')}")
        for i, x in enumerate(v):
            errs += check(x, sch.get("items") or {}, f"{path}[{i}]")
    return errs


def ask(st, system, prompt, schema, name="answer"):
    """Запрос «система + задание -> JSON по схеме». Режим — st.json. Ответ не по схеме —
    одна повторная попытка с перечнем ошибок. Возвращает (dict, запись учёта)."""
    rf = None
    if st.json == "schema":
        rf = {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
    else:
        system += "\n\n## Формат ответа\nОтвет — только JSON-объект по этой схеме, без текста вокруг:\n" + \
            json.dumps(schema, ensure_ascii=False)
        if st.json == "object":
            rf = {"type": "json_object"}
    msgs = [_system(st, system), {"role": "user", "content": prompt}]
    rub = 0.0
    for attempt in (1, 2):
        msg, rec = chat(st, msgs, response_format=rf)
        rub += rec["rub"]
        try:
            data = parse_json(msg.get("content"))
            errs = check(data, schema)
        except ValueError as e:
            data, errs = None, [str(e)]
        if not errs:
            return data, rec | {"rub": round(rub, 4)}
        if attempt == 2:
            raise LLMError(f"{st.name}/{st.model}: ответ не по схеме: {'; '.join(errs[:5])}")
        msgs += [{"role": "assistant", "content": msg.get("content") or ""},
                 {"role": "user", "content": "Ответ не по схеме: " + "; ".join(errs[:10]) +
                  ". Пришли исправленный JSON целиком."}]


def system_message(st, text):
    return _system(st, text)


def cost(stage_name=None):
    return round(sum(r["rub"] for r in USAGE if stage_name in (None, r["stage"])), 3)
