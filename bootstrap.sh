#!/data/data/com.termux/files/usr/bin/bash
set -Eeuo pipefail
IFS=$'\n\t'

: "${WORKER_ID:?Set WORKER_ID env var}"
: "${TOKEN:?Set TOKEN env var}"

H="$HOME"
REPO="$H/worker_agent"
PREFIX=/data/data/com.termux/files/usr
PY_SYS="$PREFIX/bin/python"
VENV="$REPO/.venv"
LOG="$H/worker.log"

say(){ printf '[%(%F %T)T] %s\n' -1 "$*"; }

say "▶ Bootstrap worker on Termux"

# 1) База и тулчейн (включая то, на чём падали ninja/patchelf)
yes | pkg update -y || true
yes | pkg install -y \
  git python ffmpeg curl wget make cmake clang tmux termux-api openssh \
  autoconf automake libtool ninja patchelf \
  || true

# 2) Репозиторий
if [ -d "$REPO/.git" ]; then
  say "↻ git pull"
  git -C "$REPO" pull --ff-only || true
else
  say "↓ git clone"
  git clone https://github.com/dishich/worker_agent.git "$REPO"
fi

cd "$REPO"
chmod +x install.sh start.sh stop.sh || true

# 3) VENV и зависимости
# пробуем штатный инсталлер; если он не положил venv/reqs — чиним вручную
say "⚙ install.sh (если есть)"
./install.sh || true

if [ ! -x "$VENV/bin/python" ]; then
  say "⚙ создаю venv"
  "$PY_SYS" -m venv "$VENV"
fi

# активируем и ставим зависимости (из requirements.txt или fallback-набор)
# (у тебя на части девайсов reqs не было — учитываем)
. "$VENV/bin/activate"
pip -q install --upgrade pip wheel setuptools
if [ -f requirements.txt ]; then
  say "📦 pip -r requirements.txt"
  pip -q install -r requirements.txt || true
else
  say "📦 pip fallback deps (aiohttp, websockets, soundfile, numpy, psutil)"
  pip -q install aiohttp websockets soundfile numpy psutil || true
fi

# 4) env для агента
cp -f env.sample env.sh 2>/dev/null || true
sed -i \
  -e "s|^export WORKER_ID=.*|export WORKER_ID=${WORKER_ID}|" \
  -e "s|^export TOKEN=.*|export TOKEN=${TOKEN}|" \
  env.sh

# Глобальные токены/настройки — если ещё не лежат
[ -f "$H/tokens.env" ] || cat > "$H/tokens.env" <<'EOF'
export MODEL_PATH="/sdcard/worker/models/ggml-medium-q5_0.bin"
export MODEL_URL="https://github.com/dishich/worker_agent/releases/download/models-v0.1/ggml-medium-q5_0.bin"
export THREADS="8"
export LANG_HINT="ru"
export YADISK_WEBDAV_URL="https://webdav.yandex.ru"
export YADISK_OAUTH_TOKEN="y0__xDlmdrxAhitqTog0M_RtRTiaNafHyuJTpww0oq0QhouH0FWvA"
export YADISK_BASE_DIR="/calls"
export WHISPER_BIN="$HOME/worker_agent/whisper.cpp/build/bin/whisper-cli"
EOF
chmod 600 "$H/tokens.env"

# 5) Whisper CLI — если не собран штатно, собираем сами (cmake ветка)
if [ ! -x "$REPO/whisper.cpp/build/bin/whisper-cli" ]; then
  say "🔨 собираю whisper.cpp (cmake)"
  if [ ! -d "$REPO/whisper.cpp" ]; then
    git clone https://github.com/ggerganov/whisper.cpp.git "$REPO/whisper.cpp"
  fi
  cmake -S "$REPO/whisper.cpp" -B "$REPO/whisper.cpp/build"
  cmake --build "$REPO/whisper.cpp/build" --config Release -j"$(nproc 2>/dev/null || echo 4)"
fi

# 6) Автозапуск через Termux:Boot
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
say "🧷 boot-скрипт записан: ~/.termux/boot/99-start.sh"

# предупредим, если Termux:Boot не установлен
if ! pm path com.termux.boot >/dev/null 2>&1; then
  say "⚠ Termux:Boot НЕ установлен — автозапуск после ребута не сработает"
fi

# 7) Поднимаем сейчас
say "⏹ stop && ▶ start"
"$REPO/stop.sh" || true
nohup "$REPO/start.sh" >/dev/null 2>&1 &

# 8) Быстрые проверки
sleep 2
( toybox nc -z 127.0.0.1 8022 >/dev/null 2>&1 && say "✅ sshd: LISTEN :8022" ) || say "❌ sshd не слушает :8022"
say "— agent tail —"
tail -n 60 "$LOG" 2>/dev/null || true

say "✅ DONE"
