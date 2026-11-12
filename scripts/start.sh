#!/usr/bin/env bash
# Start Flight Radar: scheduler + Telegram bot, and the dashboard on http://localhost:8501.
# Works in WSL/Linux/macOS. Logs go to data/run.log and data/dashboard.log.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .venv/bin/activate ] && source .venv/bin/activate
mkdir -p data
python main.py init >/dev/null
nohup python main.py dashboard >data/dashboard.log 2>&1 &
echo "Dashboard: http://localhost:8501 (log: data/dashboard.log)"
echo "Searching every 12h with a Telegram summary every 12h. Ctrl+C stops the scheduler."
exec python main.py run 2>&1 | tee -a data/run.log
