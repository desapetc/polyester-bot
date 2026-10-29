#!/usr/bin/env bash
# Stop the background bot gracefully (it prints a per-account summary on exit).
cd "$(dirname "$0")"
pid=$(cat bot.pid 2>/dev/null)
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  kill -INT "$pid" && echo "Stopping bot (pid $pid)... summary in logs/bot.log"
else
  echo "Bot not running"
fi
