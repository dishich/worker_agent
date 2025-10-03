#!/data/data/com.termux/files/usr/bin/bash -l
set -Eeuo pipefail

# --- базовые пути ---
H="${HOME:-/data/data/com.termux/files/home}"
REPO="$H/worker_agent"
LOG="$H/worker.log"
PATH="/data/data/com.termux/files/usr/bin:$PATH"
export PYTHONUNBUFFERED=1

# --- окружение: tokens.env и (для совместимости) env.sh ---
[ -f "$H/tokens.env" ]    && . "$H/tokens.env"
[ -f "$REPO/env.sh" ]     && . "$REPO/env.sh"

# --- дефолты, если не заданы во внешнем окружении ---
export MODEL_PATH="${MODEL_PATH:-/sdcard/worker/models/ggml-medium-q5_0.bin}"
export THREADS="${THREADS:-8}"
export LANG_HINT="${LANG_HINT:-ru}"

# --- проверить/докачать модель при отсутствии ---
if [ ! -s "$MODEL_PATH" ]; then
  mkdir -p "$(dirname "$MODEL_PATH")"
  MODEL_URL="${MODEL_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium-q5_0.bin}"
  {
    echo "[$(date '+%F %T')] [info] model not found, downloading: $MODEL_URL -> $MODEL_PATH"
    if command -v curl >/dev/null 2>&1; then
      curl -L --fail --retry 3 --retry-delay 2 --connect-timeout 10 --max-time 600 \
           -o "${MODEL_PATH}.tmp" "$MODEL_URL" \
      && mv -f "${MODEL_PATH}.tmp" "$MODEL_PATH" \
      && echo "[$(date '+%F %T')] [ok] model downloaded"
    else
      echo "[$(date '+%F %T')] [warn] curl not found; skip auto-download"
    fi
  } >> "$LOG" 2>&1
fi

# --- лёгкая ротация лога (5 МБ) ---
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ]; then
  mv -f "$LOG" "$LOG.1" 2>/dev/null || true
fi
touch "$LOG"

# --- venv если есть ---
if [ -d "$REPO/.venv" ] && [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
else
  PY="$(command -v python3 || command -v python || echo /data/data/com.termux/files/usr/bin/python)"
fi

# --- в репо и старт баннер ---
cd "$REPO"
echo "[$(date '+%F %T')] START agent: model_path=\"$MODEL_PATH\" threads=$THREADS lang_hint=$LANG_HINT py=$("$PY" -V 2>&1)" >> "$LOG"

# --- запуск агента (stdout/stderr в worker.log) ---
exec "$PY" "$REPO/agent.py" >> "$LOG" 2>&1
