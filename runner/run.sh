#!/bin/bash
# Прогон ИИ-редактора (API-версия) по cron:
#   сбор (fetch.py) -> сортировка, дешёвая модель (triage.py) -> отбор, агентский цикл (selection.py)
#   -> рерайт + фактчек каждой новости (stages.py) -> страница (finish.py).
# Модели этапов — config.ini, ключи — ~/.config/ai-editor-api.env.
# Лог: logs/run_ГГГГ-ММ-ДД.log
set -u
# время — самарское везде: имя лога, «старт»/«конец», этапы (сервер в MSK)
export TZ=Europe/Samara
cd "$(dirname "$0")"
mkdir -p logs run state
LOG="logs/run_$(date +%F).log"
exec >>"$LOG" 2>&1
exec 9>run/.lock
flock -n 9 || { echo "$(date '+%F %T') прошлый прогон ещё идёт — пропуск"; exit 0; }
echo "===== $(date '+%F %T') старт"

set -a; . "$HOME/.config/ai-editor-api.env"; set +a
export PYTHONIOENCODING=utf-8
rm -f run/changes.json run/selected.json run/stages.json run/triage.json run/select_input.md \
      run/fetched.json run/error.txt run/usage.jsonl

# причина сбоя для страницы; подробнее (ошибку шлюза) этапы пишут в run/error.txt сами
fail() { echo "$1"; [ -s run/error.txt ] || echo "$1" > run/error.txt; }

python3 fetch.py; rc=$?
if [ $rc -eq 3 ]; then
  echo "нового нет — модели не вызываются"
elif [ $rc -ne 0 ]; then
  fail "сбор входа (fetch.py) упал, код $rc"; exit 1
else
  timeout -s KILL 300 python3 triage.py; rc=$?
  if [ $rc -eq 4 ]; then
    echo "кандидатов нет — отбор и рерайт не вызываются"
  else
    if [ $rc -ne 0 ]; then  # сортировка упала — отбор получает весь вход
      echo "сортировка (triage.py) упала, код $rc — отбор по всему входу"; cp run/input.md run/select_input.md
    fi
    timeout -s KILL 900 python3 selection.py || fail "отбор (selection.py) не сдал итог — см. лог"
    # нет selected.json — нет и changes.json: finish.py покажет сбой и не сдвинет отметки
    if [ -s run/selected.json ]; then
      timeout -s KILL 900 python3 stages.py || fail "рерайт/фактчек (stages.py) упал — см. лог"
    fi
  fi
fi

python3 finish.py
python3 cost.py --run 2>/dev/null
echo "===== $(date '+%F %T') конец"
