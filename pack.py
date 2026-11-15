#!/usr/bin/env python3
# Сборка архива для сервера: python pack.py [--sync] -> dist/ai_editor_api.tar.gz
#   --sync — сначала подтянуть общие правила из ../ai_editor (rules.md, rewrite_rules.md и т.д.)
# Все текстовые файлы — с LF; config.ini кладётся как config.ini.dist (update.sh не затирает рабочий).
# Выкладка (у пользователя потом ./update.sh):
#   scp dist/ai_editor_api.tar.gz user@VPS_HOST:/tmp/  (затем tar xzf в папке кода)
import io
import shutil
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "runner"
MAIN = HERE.parent / "ai_editor" / "server"
# общие с ai_editor файлы: правила редакции и справочники сайта (там — источник истины).
# social.py — свой с 08.11.2026 (без Telegram), не синхронизируется.
SHARED = {"runner/rules.md": "rules.md", "runner/cats.md": "cats.md", "runner/tags.md": "tags.md",
          "runner/fotos.md": "fotos.md", "runner/dupcheck.py": "dupcheck.py",
          "rewrite/rewrite_rules.md": "rewrite_rules.md", "rewrite/factcheck_rules.md": "factcheck_rules.md",
          "rewrite/fix_rules.md": "fix_rules.md"}
EXEC = {"run.sh", "update.sh", "check.py", "cost.py"}


def sync():
    for src, dst in SHARED.items():
        a, b = MAIN / src, RUNNER / dst
        if a.read_bytes().replace(b"\r\n", b"\n") != b.read_bytes().replace(b"\r\n", b"\n"):
            shutil.copyfile(a, b)
            print("обновлён из ai_editor:", dst)


def main():
    if "--sync" in sys.argv:
        sync()
    version = time.strftime("%Y-%m-%d %H:%M")
    (HERE / "dist").mkdir(exist_ok=True)
    out = HERE / "dist" / "ai_editor_api.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        files = sorted(p for p in RUNNER.iterdir() if p.is_file() and p.suffix != ".pyc")
        for p in files + [None]:
            name = "VERSION" if p is None else ("config.ini.dist" if p.name == "config.ini" else p.name)
            data = version.encode() + b"\n" if p is None else p.read_bytes()
            if p is not None and p.suffix in (".py", ".sh", ".md", ".html", ".ini", ".example"):
                data = data.replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), int(time.time())
            info.mode = 0o755 if name in EXEC else 0o644
            tar.addfile(info, io.BytesIO(data))
    print(f"{out} ({out.stat().st_size // 1024} КБ, {len(files) + 1} файлов), версия {version}")


if __name__ == "__main__":
    main()
