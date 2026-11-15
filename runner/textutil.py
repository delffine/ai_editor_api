# Общие текстовые правки (из ai_editor server/rewrite/rewrite_worker.py).

QUOTES = "\"'`\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u2039\u203a"
OPEN_AFTER = " \t\r\n([{«-\u2013\u2014/"


def fix_quotes(s):
    """Все кавычки — «ёлочки» (правило редакции). Открывающая — в начале строки
    или после пробела/скобки/тире, иначе закрывающая; апостроф между буквами
    (О'Нил) не трогаем."""
    out = []
    for i, ch in enumerate(s):
        if ch not in QUOTES:
            out.append(ch)
            continue
        prev = s[i - 1] if i else ""
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if ch in "'\u2019`" and prev.isalpha() and nxt.isalpha():
            out.append(ch)
        elif not prev or prev in OPEN_AFTER or ch == "\u201e":
            out.append("«")
        else:
            out.append("»")
    return "".join(out)
