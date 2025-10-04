#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail

: "${WORKER_ID:?Set WORKER_ID env var}"
: "${TOKEN:?Set TOKEN env var}"

H="$HOME"
REPO="$H/worker_agent"
PREFIX=/data/data/com.termux/files/usr

echo "▶ Bootstrap worker on Termux"

# 1️⃣ Обновление и базовые пакеты
yes | pkg update -y || true
yes | pkg install -y git python ffmpeg openssl-tool wget curl make cmake clang tmux termux-api openssh autoconf automake libtool ninja patchelf || true

# 2️⃣ Репозиторий
if [ -d "$REPO/.git" ]; then
  cd "$REPO" && git pull --ff-only
else
  git clone https://github.com/dishich/worker_agent.git "$REPO"
fi

# 3️⃣ Установка окружения и зависимостей
cd "$REPO"
chmod +x install.sh start.sh stop.sh || true
./install.sh

# 4️⃣ Настройка окружения
cp -f env.sample env.sh || true
sed -i \
  -e "s|^export WORKER_ID=.*|export WORKER_ID=${WORKER_ID}|" \
  -e "s|^export TOKEN=.*|export TOKEN=${TOKEN}|" \
  env.sh

# 5️⃣ Создаём tokens.env (если его ещё нет)
[ -f "$H/tokens.env" ] || cat > "$H/tokens.env" <<'EOF'
export MODEL_PATH="/sdcard/worker/models/ggml-medium-q5_0.bin"
export MODEL_URL="https://github.com/dishich/worker_agent/releases/download/models-v0.1/ggml-medium-q5_0.bin"
export THREADS="8"
export LANG_HINT="ru"
export YADISK_WEBDAV_URL="https://webdav.yandex.ru"
export YADISK_OAUTH_TOKEN="y0__xDlmdrxAhitqTog0M_RtRTiaNafHyuJTpww0oq0QhouH0FWvA"
export YADISK_BASE_DIR="/calls"
EOF
chmod 600 "$H/tokens.env"

# 6️⃣ Автозапуск через Termux:Boot
mkdir -p "$H/.termux/boot"
cat > "$H/.termux/boot/99-start.sh" <<'SH'
#!/data/data/com.termux/files/usr/bin/bash
PREFIX=/data/data/com.termux/files/usr
H="$HOME"
termux-wake-lock >/dev/null 2>&1 || true
"$PREFIX/bin/ssh-keygen" -A >/dev/null 2>&1 || true
pkill -f "$PREFIX/bin/sshd" >/dev/null 2>&1 || true
nohup "$PREFIX/bin/sshd" -p 8022 >/dev/null 2>&1 &
[ -x "$H/worker_agent/start.sh" ] && nohup "$H/worker_agent/start.sh" >>"$H/worker.log" 2>&1 &
SH
chmod 700 "$H/.termux/boot/99-start.sh"

# 7️⃣ Стартуем сразу
"$REPO/stop.sh" || true
nohup "$REPO/start.sh" >/dev/null 2>&1 &

echo "✅ DONE. tail -f ~/worker.log"
