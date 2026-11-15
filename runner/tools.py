# Инструменты агента отбора (selection.py) — их исполняет наш код, модель только просит:
#   fetch_url(url, find)   — страница как текст (вместо curl в Claude Code)
#   web_search(query)      — Yandex Search API v2 (вместо WebSearch Anthropic)
#   read_proposals(ids)    — полные поля наших предложений (для update)
# Полные тексты прочитанных страниц копятся в FETCHED: рерайт берёт фактуру оттуда,
# если отбор дал url без выдержек (run/fetched.json).
import base64
import ipaddress
import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

BASE = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
MAX_BYTES = 3 * 1024 * 1024
FETCHED = {}  # url -> {"title", "text"}


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "button", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section", "td", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self._in_title = [], 0, "", False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def html_to_text(page):
    p = _Text()
    p.feed(page)
    text = re.sub(r"[ \t\xa0]+", " ", "".join(p.out))
    lines = [l.strip() for l in text.split("\n")]
    return " ".join(p.title.split()), re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _public_host(host):
    """Только публичные адреса: модель выбирает url сама, а на сервере есть свои службы."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"адрес не найден: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("внутренний адрес — запрещено")


def _decode(raw, ctype):
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I) or \
        re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4000], re.I)
    enc = m.group(1) if m else "utf-8"
    enc = enc.decode() if isinstance(enc, bytes) else enc
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def get_page(url, timeout=25):
    """url -> (итоговый url, заголовок, текст). Сайты sudrf/adm часто с битым сертификатом —
    при ошибке SSL повтор без проверки (как curl -k в версии на Claude Code)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("только http/https")
    _public_host(u.hostname)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise
        r = urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context())
    with r:
        _public_host(urllib.parse.urlsplit(r.geturl()).hostname or "")  # и после редиректа
        raw = r.read(MAX_BYTES)
        ctype = r.headers.get("Content-Type", "")
        final = r.geturl()
    page = _decode(raw, ctype)
    if "html" in ctype or "<html" in page[:2000].lower():
        title, text = html_to_text(page)
    elif "xml" in ctype or page.lstrip().startswith("<?xml"):  # RSS: заголовки и описания
        title, text = "", html_to_text(re.sub(r"<!\[CDATA\[|\]\]>", "", page))[1]
    else:
        title, text = "", page
    return final, title, text


def fetch_url(url, find="", chars=6000):
    try:
        final, title, text = get_page(url)
    except Exception as e:  # noqa: BLE001 — ошибку отдаём модели
        return f"ОШИБКА: {type(e).__name__}: {e}"[:400]
    FETCHED[url] = FETCHED[final] = {"title": title, "text": text}
    head = f"URL: {final}\nЗаголовок: {title}\nДлина текста: {len(text)} символов\n\n"
    if find:
        low, pos, parts = text.lower(), 0, []
        for _ in range(4):
            i = low.find(find.lower(), pos)
            if i < 0:
                break
            parts.append(text[max(0, i - 700):i + 1300])
            pos = i + 1300
        if not parts:
            return head + f"«{find}» на странице не найдено. Начало текста:\n" + text[:chars // 2]
        return head + "\n…\n".join(parts)[:chars]
    more = f"\n\n[… обрезано, всего {len(text)} символов; нужный фрагмент — fetch_url с find]" if len(text) > chars else ""
    return head + text[:chars] + more


def web_search(query, cfg):
    key = os.environ.get(cfg.get("key_env", "YANDEX_SEARCH_API_KEY"), "")
    folder = os.environ.get(cfg.get("folder_env", "YANDEX_FOLDER_ID"), "")
    if not key or not folder:
        return "ОШИБКА: поиск не настроен (нет ключа или каталога Yandex Cloud)"
    body = {"query": {"searchType": "SEARCH_TYPE_RU", "queryText": query[:400], "familyMode": "FAMILY_MODE_NONE"},
            "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": str(cfg.get("results", "8")),
                          "docsInGroup": "1"},
            "maxPassages": "2", "l10n": "LOCALIZATION_RU", "folderId": folder, "responseFormat": "FORMAT_XML"}
    if cfg.get("region"):
        body["region"] = str(cfg["region"])
    req = urllib.request.Request("https://searchapi.api.cloud.yandex.net/v2/web/search",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": "Api-Key " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = base64.b64decode(json.loads(r.read())["rawData"])
        root = ET.fromstring(raw)
    except urllib.error.HTTPError as e:
        return f"ОШИБКА поиска: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:  # noqa: BLE001
        return f"ОШИБКА поиска: {type(e).__name__}: {e}"[:300]
    err = root.find(".//error")
    if err is not None:
        return "ОШИБКА поиска: " + " ".join("".join(err.itertext()).split())
    out = []
    for n, doc in enumerate(root.iter("doc"), 1):
        def txt(tag):
            el = doc.find(tag)
            return " ".join("".join(el.itertext()).split()) if el is not None else ""
        passages = " … ".join(" ".join("".join(p.itertext()).split()) for p in doc.iter("passage")) or txt("headline")
        date = txt("modtime")
        date = f"{date[6:8]}.{date[4:6]}.{date[:4]}" if re.match(r"\d{8}T", date) else ""
        out.append(f"{n}. {txt('title')}\n{txt('url')}{' | ' + date if date else ''}\n{passages}")
    return "\n\n".join(out) or "ничего не найдено"


def read_proposals(ids):
    try:
        props = json.loads((BASE / "state" / "proposals.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "предложений нет"
    keep = ("id", "block", "headlines", "lead", "body", "detected_at", "sources_html")
    found = [{k: p.get(k) for k in keep} for p in props if p["id"] in ids]
    return json.dumps(found, ensure_ascii=False) if found else "таких id нет"


def save_fetched():
    (BASE / "run" / "fetched.json").write_text(json.dumps(FETCHED, ensure_ascii=False), encoding="utf-8")
