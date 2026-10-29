#!/usr/bin/env bash
# Run the bot in the background (Linux/VPS). Log: logs/bot.log, stop: ./stop.sh
cd "$(dirname "$0")"
mkdir -p logs
if [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; then
  echo "Bot already running (pid $(cat bot.pid))"; exit 1
fi
nohup .venv/bin/python -u bot.py >> logs/bot.log 2>&1 < /dev/null &
echo $! > bot.pid
echo "Bot started (pid $!). Log: tail -f logs/bot.log"
