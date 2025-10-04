#!/data/data/com.termux/files/usr/bin/bash -l
set -Eeuo pipefail

H="$HOME"
REPO="$H/worker_agent"
cd "$REPO"

# 0) venv
if [ ! -x ".venv/bin/python" ]; then
  python -m venv .venv
fi
. .venv/bin/activate
pip install -U pip wheel setuptools

# 1) Termux-пакеты, чтобы ничего не собирать через pip
if command -v pkg >/dev/null 2>&1; then
  yes | pkg update -y || true
  yes | pkg install -y ninja patchelf autoconf automake libtool || true
fi

# 2) Python-зависимости (requirements.txt добавь в репо)
# если файла нет — поставим базовый набор
REQ_IN="requirements.txt"
REQ_TMP="$(mktemp)"
if [ -f "$REQ_IN" ]; then
  # ninja/patchelf держим системными — из pip их не ставим
  grep -v -E '^(ninja|patchelf)([=<>].*)?$' "$REQ_IN" > "$REQ_TMP" || true
else
  cat > "$REQ_TMP" <<'EOF'
aiohttp
websockets
soundfile
numpy
psutil
EOF
fi
pip install -r "$REQ_TMP"

# 3) whisper.cpp — собираем через CMake (устойчивее, чем просто make)
if [ ! -x "$REPO/whisper.cpp/build/bin/whisper-cli" ]; then
  [ -d whisper.cpp/.git ] || git clone https://github.com/ggerganov/whisper.cpp.git
  cd whisper.cpp
  cmake -B build
  cmake --build build --config Release
  cd "$REPO"
fi

echo "✅ install.sh done"
