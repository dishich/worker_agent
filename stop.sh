#!/data/data/com.termux/files/usr/bin/bash
tmux kill-session -t worker 2>/dev/null || true
echo "⏹ Агент остановлен"
