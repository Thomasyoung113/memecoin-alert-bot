#!/bin/bash
# Persistent runner for Gem Alert Bot.
# Keeps the bot alive even if the process crashes.
# Logs are saved to bot.log

cd "$(dirname "$0")"

BOT_LOG="bot.log"
PID_FILE="bot.pid"

stop_bot() {
    if [ -f "$PID_FILE" ]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null
        rm -f "$PID_FILE"
        echo "Bot stopped."
    fi
}

start_bot() {
    echo "Starting Gem Alert Bot..."
    nohup python3 main.py >> "$BOT_LOG" 2>&1 &
    echo $! > "$PID_FILE"
    echo "Bot started (PID: $(cat "$PID_FILE"))"
    echo "Logs: $BOT_LOG"
    echo ""
    echo "To stop:  ./run.sh stop"
    echo "To view:  tail -f $BOT_LOG"
}

case "${1:-start}" in
    start)
        stop_bot
        start_bot
        ;;
    stop)
        stop_bot
        ;;
    restart)
        stop_bot
        sleep 1
        start_bot
        ;;
    logs)
        tail -f "$BOT_LOG"
        ;;
    status)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "Bot is running (PID: $(cat "$PID_FILE"))"
            echo "Active alerts in DB:"
            python3 -c "
from bot.models import get_conn
conn = get_conn()
total = conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]
pending = conn.execute('SELECT COUNT(*) FROM alerts WHERE resolved=0').fetchone()[0]
success = conn.execute('SELECT COUNT(*) FROM alerts WHERE resolved=1 AND hit_2x=1').fetchone()[0]
conn.close()
print(f'  Total alerts: {total}')
print(f'  Pending: {pending}')
print(f'  Hit 2x: {success}')
"
        else
            echo "Bot is not running."
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|logs|status}"
        ;;
esac