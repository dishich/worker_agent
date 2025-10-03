#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

: "${WORKER_ID:?Set WORKER_ID env var}"
: "${TOKEN:?Set TOKEN env var}"
MODEL_URL="${MODEL_URL:-}"
MODEL_PATH="${MODEL_PATH:-/sdcard/worker/models/ggml-medium-q5_0.bin}"
THREADS="${THREADS:-8}"
LANG_HINT="${LANG_HINT:-ru}"

echo "▶ Bootstrap worker on Termux"
pkg update -y
pkg install -y git python ffmpeg openssl-tool wget curl make cmake clang tmux

if [ ! -d "$HOME/worker_agent/.git" ]; then
  git clone https://github.com/dishich/worker_agent.git "$HOME/worker_agent"
else
  cd "$HOME/worker_agent"
  git pull --ff-only
fi

cd "$HOME/worker_agent"
./install.sh

cp -f env.sample env.sh
sed -i \
  -e "s|^export WORKER_ID=.*|export WORKER_ID=${WORKER_ID}|" \
  -e "s|^export TOKEN=.*|export TOKEN=${TOKEN}|" \
  -e "s|^export MODEL_PATH=.*|export MODEL_PATH=\"${MODEL_PATH}\"|" \
  -e "s|^export THREADS=.*|export THREADS=${THREADS}|" \
  -e "s|^export LANG_HINT=.*|export LANG_HINT=${LANG_HINT}|" \
  env.sh

if [ -n "$MODEL_URL" ]; then
  mkdir -p /sdcard/worker/models
  echo "▶ Downloading model to ${MODEL_PATH}"
  tmp="/sdcard/worker/models/.tmp.$(date +%s)"
  if wget -O "$tmp" "$MODEL_URL"; then
    mv -f "$tmp" "$MODEL_PATH"
  else
    echo "❌ Failed to download model from MODEL_URL"; rm -f "$tmp"; exit 1
  fi
fi

./stop.sh || true
./start.sh
echo "✅ DONE. Attached tmux logs:"
tmux ls || true
