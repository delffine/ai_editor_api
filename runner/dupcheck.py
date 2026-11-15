# Подсказка «похоже на news-XXXXX»: входящий материал сравнивается с архивом сайта
# (заголовок + лид, ~60 дней) по общим «основам» слов с весом IDF. Решает отбор по фактам —
# здесь только кандидаты, чтобы поздние перепечатки (СМИ дают сюжет через 3–17 дней) не проскакивали.
import math
import re

STOP = set("""сызра сызран самар самарс облас регио район город городс жител котор также котор
этого этому также более после перед через время сейча может будет было были сообщ расск
отмет говор ранее сегод вчера новос неско котор однак тольк очень можно нужно стало своих
свое своей своим всего всех между когда такие такой таких этих этот этой""".split())


ENDINGS = sorted("""иями ями ами ого его ому ему ыми ими ией ией ость ости ая яя ое ее ой ей ый ий ую юю ом ем
ах ях ов ев ам ям ых их ые ие ию ья ье ьи ы и а я о е у ю ь й""".split(), key=len, reverse=True)


def stem(w):
    if w.isdigit() or not re.match("[а-я]", w):
        return w
    for e in ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            w = w[:-len(e)]
            break
    return w[:7]


def stems(text):
    out = set()
    for w in re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е")):
        if len(w) < 4 and not (w.isdigit() and len(w) >= 2):
            continue
        s = stem(w)
        if s[:5] not in STOP:
            out.add(s)
    return out


class Index:
    def __init__(self, site, idf_from=None):  # site: [{"id", "date", "title", "lead"}]
        self.site = [(s, stems(s["title"] + " " + s["title"] + " " + s["lead"])) for s in site]
        if idf_from is not None:  # маленький список (убранное редакцией) — редкость слов берём из архива сайта
            self.idf, self.n = idf_from.idf, idf_from.n
            return
        df = {}
        for _, st in self.site:
            for t in st:
                df[t] = df.get(t, 0) + 1
        n = len(self.site) or 1
        self.idf = {t: math.log(n / c) for t, c in df.items()}
        self.n = n

    def match(self, title, text, top=2, min_score=0.22, min_shared=4):
        q = stems(title + " " + text[:700])
        qw = {t: self.idf.get(t, math.log(self.n)) for t in q}
        total = sum(qw.values()) or 1
        res = []
        for s, st in self.site:
            shared = q & st
            if len(shared) < min_shared:
                continue
            w = sum(qw[t] for t in shared)
            # доля «веса» входящего, совпавшая с новостью сайта; рядом — сравнение с размером самой новости сайта
            sw = sum(self.idf.get(t, math.log(self.n)) for t in st) or 1
            score = min(w / total, 1) * 0.5 + min(w / sw, 1) * 0.5
            if score >= min_score:
                res.append((round(score, 2), s))
        res.sort(key=lambda r: -r[0])
        return res[:top]
