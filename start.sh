#!/data/data/com.termux/files/usr/bin/bash
set -e
source ./env.sh
tmux new-session -d -s worker "python agent.py"
echo "✅ Агент запущен в tmux (сессия: worker)"
