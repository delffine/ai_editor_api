#!/bin/bash
# Установка и обновление кода на сервере: ./update.sh  (первый раз — см. README.md, «Установка»).
# Ждёт конца текущего прогона (тот же flock, что у run.sh). config.ini не трогает:
# в архиве он лежит как config.ini.dist и копируется, только если config.ini ещё нет.
set -eu
{  # блок читается bash целиком до выполнения — tar может перезаписать этот же файл
cd "$(dirname "$0")"
URL="${AI_EDITOR_API_DIST:-http://VPS_HOST:8808/ai-editor/dl/ai_editor_api.tar.gz}"
mkdir -p run logs state
curl -fsS -o run/dist.tar.gz "$URL"
exec 9>run/.lock
flock 9
tar xzf run/dist.tar.gz
rm -f run/dist.tar.gz
[ -f config.ini ] || cp config.ini.dist config.ini
chmod +x run.sh update.sh check.py cost.py
echo "обновлено: $(cat VERSION)"
if ! diff -q <(grep -E '^\[|^[a-z_]+ *=' config.ini.dist | cut -d= -f1 | tr -d ' ') \
             <(grep -E '^\[|^[a-z_]+ *=' config.ini | cut -d= -f1 | tr -d ' ') >/dev/null; then
  echo "в config.ini.dist есть изменения параметров — сравните: diff config.ini config.ini.dist"
fi
exit 0
}
