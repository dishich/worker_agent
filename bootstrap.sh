#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail

### ==== ОБЯЗАТЕЛЬНЫЕ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ====
: "${WORKER_ID:?Set WORKER_ID env var}"
: "${TOKEN:?Set TOKEN env var}"

### ==== НАСТРОЙКИ ПО УМОЛЧАНИЮ (МОЖНО ПЕРЕОПРЕДЕЛИТЬ СВЕРХУ) ====
MODEL_PATH="${MODEL_PATH:-/sdcard/worker/models/ggml-medium-q5_0.bin}"
MODEL_URL="${MODEL_URL:-https://github.com/dishich/worker_agent/releases/download/models-v0.1/ggml-medium-q5_0.bin}"
THREADS="${THREADS:-8}"
LANG_HINT="${LANG_HINT:-ru}"

### ==== БАЗОВЫЕ ПУТИ ====
H="$HOME"
REPO="$H/worker_agent"
PREFIX="/data/data/com.termux/files/usr"
LOG="$H/worker.log"

echo "▶ Bootstrap worker on Termux ($(date '+%F %T'))"

### 1) Обновление и пакеты (тихо и идемпотентно)
yes | pkg update -y >/dev/null 2>&1 || true
yes | pkg install -y \
  git python ffmpeg openssl-tool wget curl make cmake clang tmux \
  termux-api openssh autoconf automake libtool ninja patchelf >/dev/null 2>&1 || true

# pip починка, если что-то пошло не так
if ! "$PREFIX/bin/python" -m ensurepip >/dev/null 2>&1; then
  yes | pkg install -y python-pip >/dev/null 2>&1 || true
fi

### 2) Репозиторий
if [ -d "$REPO/.git" ]; then
  git -C "$REPO" pull --ff-only || true
else
  git clone https://github.com/dishich/worker_agent.git "$REPO"
fi

### 3) Установка окружения (venv + deps) и сборка whisper.cpp
cd "$REPO"
chmod +x install.sh start.sh stop.sh || true

# если install.sh упадёт из-за отсутствующих build-утилит — мы их уже поставили выше
./install.sh || true

# страховка: если venv не появился — создаём и ставим минимальные зависимости
if [ ! -x "$REPO/.venv/bin/python" ]; then
  "$PREFIX/bin/python" -m venv "$REPO/.venv"
fi
. "$REPO/.venv/bin/activate"
pip -q install --upgrade pip wheel setuptools

# fallback requirements (на случай отсутствия requirements.txt внутри install.sh)
if [ ! -f requirements.txt ]; then
  cat > requirements.txt <<'REQ'
aiohttp
websockets
soundfile
numpy
psutil
REQ
fi
pip -q install -r requirements.txt || pip -q install aiohttp websockets soundfile numpy psutil

# whisper.cpp — обновить/собрать, если бинаря нет
if [ -d "$REPO/whisper.cpp/.git" ]; then
  git -C "$REPO/whisper.cpp" pull --ff-only || true
fi
WHISPER_BIN="$REPO/whisper.cpp/build/bin/whisper-cli"
if [ ! -x "$WHISPER_BIN" ]; then
  CORES="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
  cmake -B "$REPO/whisper.cpp/build" >/dev/null
  cmake --build "$REPO/whisper.cpp/build" --config Release -j"$CORES" >/dev/null
fi

### 4) env.sh + tokens.env
# env.sh — управляемые проектом дефолты
cp -f env.sample env.sh 2>/dev/null || touch env.sh
sed -i \
  -e "s|^export WORKER_ID=.*|export WORKER_ID=${WORKER_ID}|" \
  -e "s|^export TOKEN=.*|export TOKEN=${TOKEN}|" \
  -e "s|^export THREADS=.*|export THREADS=${THREADS}|" \
  -e "s|^export LANG_HINT=.*|export LANG_HINT=${LANG_HINT}|" \
  env.sh

# tokens.env — локальные переопределения для устройства (не коммитим)
cat > "$H/tokens.env" <<EOF
export SERVER_WS="wss://call-analysis-s6cb.onrender.com/ws/worker/\${WORKER_ID}?token=\${TOKEN}"
export SERVER_API="https://call-analysis-s6cb.onrender.com/api/v1/job_result"
export MODEL_PATH="${MODEL_PATH}"
export MODEL_URL="${MODEL_URL}"
export THREADS="${THREADS}"
export LANG_HINT="${LANG_HINT}"
export WHISPER_BIN="${WHISPER_BIN}"
# опционально: Я.Диск
export YADISK_WEBDAV_URL="${YADISK_WEBDAV_URL:-https://webdav.yandex.ru}"
export YADISK_OAUTH_TOKEN="${YADISK_OAUTH_TOKEN:-}"
export YADISK_BASE_DIR="${YADISK_BASE_DIR:-/calls}"
EOF
chmod 600 "$H/tokens.env"

### 5) Модель — докачать при отсутствии
if [ ! -s "$MODEL_PATH" ]; then
  echo "▶ Downloading model to ${MODEL_PATH}"
  mkdir -p "$(dirname "$MODEL_PATH")"
  TMP="${MODEL_PATH}.tmp.$$"
  curl -L --fail --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 1200 \
       -o "$TMP" "$MODEL_URL"
  mv -f "$TMP" "$MODEL_PATH"
fi

### 6) Автозапуск через Termux:Boot (sshd + агент)
mkdir -p "$H/.termux/boot"
cat > "$H/.termux/boot/99-start.sh" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
set -e
PREFIX=/data/data/com.termux/files/usr
H="$HOME"

termux-wake-lock >/dev/null 2>&1 || true

# SSHD
"$PREFIX/bin/ssh-keygen" -A >/dev/null 2>&1 || true
pkill -f "$PREFIX/bin/sshd" >/dev/null 2>&1 || true
nohup "$PREFIX/bin/sshd" -p 8022 >/dev/null 2>&1 &

# Агент
[ -x "$H/worker_agent/start.sh" ] && nohup "$H/worker_agent/start.sh" >>"$H/worker.log" 2>&1 &
BOOT
chmod 700 "$H/.termux/boot/99-start.sh"
command -v termux-reload-settings >/dev/null 2>&1 && termux-reload-settings || true

### 7) Стартуем сейчас
# лёгкая ротация лога
[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ] && mv -f "$LOG" "$LOG.1" || true
touch "$LOG"

"$REPO/stop.sh" >/dev/null 2>&1 || true
nohup "$REPO/start.sh" >/dev/null 2>&1 &

echo "[$(date '+%F %T')] BOOTSTRAP completed on $(getprop ro.product.model 2>/dev/null || echo unknown)" >> "$LOG"
echo "✅ DONE. tail -n 60 ~/worker.log:"
tail -n 60 "$LOG" || true
