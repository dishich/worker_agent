#!/data/data/com.termux/files/usr/bin/bash -l
set -Eeuo pipefail

# --- базовые пути ---
H="${HOME:-/data/data/com.termux/files/home}"
REPO="$H/worker_agent"
LOG="$H/worker.log"
PREFIX=/data/data/com.termux/files/usr
PATH="$PREFIX/bin:$PATH"
export PYTHONUNBUFFERED=1
export TMPDIR="$H/.cache/tmp"
mkdir -p "$TMPDIR" "$H/.cache/pip" >/dev/null 2>&1 || true

# --- окружение: сперва env.sh (дефолты репо), потом tokens.env (локальные/боевые) ---
[ -f "$REPO/env.sh" ]  && . "$REPO/env.sh"
[ -f "$H/tokens.env" ] && . "$H/tokens.env"

# --- дефолты на всякий случай ---
export MODEL_PATH="${MODEL_PATH:-/sdcard/worker/models/ggml-medium-q5_0.bin}"
export THREADS="${THREADS:-8}"
export LANG_HINT="${LANG_HINT:-ru}"

# --- докачать модель при отсутствии (не критично, но полезно) ---
if [ ! -s "$MODEL_PATH" ]; then
  mkdir -p "$(dirname "$MODEL_PATH")"
  MODEL_URL="${MODEL_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium-q5_0.bin}"
  {
    echo "[$(date '+%F %T')] [info] model not found, downloading: $MODEL_URL"
    if command -v curl >/dev/null 2>&1; then
      curl -L --fail --retry 3 --retry-delay 2 --connect-timeout 10 --max-time 900 \
           -o "${MODEL_PATH}.tmp" "$MODEL_URL" \
      && mv -f "${MODEL_PATH}.tmp" "$MODEL_PATH" \
      && echo "[$(date '+%F %T')] [ok] model downloaded" \
      || echo "[$(date '+%F %T')] [err] model download failed"
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

# --- venv + зависимости (если надо) ---
cd "$REPO"
if [ ! -x ".venv/bin/python" ]; then
  echo "[$(date '+%F %T')] [info] creating venv" >> "$LOG"
  (python -m venv .venv && . .venv/bin/activate && pip -q install --upgrade pip wheel setuptools) >>"$LOG" 2>&1 || true
fi
PY="$REPO/.venv/bin/python"
if [ ! -x "$PY" ]; then
  # fallback на системный python (не идеал, но пусть стартует)
  PY="$(command -v python3 || command -v python || echo "$PREFIX/bin/python")"
fi

# если есть requirements.txt — ставим оттуда, иначе минимальный набор
if [ -x "$REPO/.venv/bin/pip" ]; then
  if [ -f requirements.txt ]; then
    "$REPO/.venv/bin/pip" install -r requirements.txt >>"$LOG" 2>&1 || true
  else
    "$REPO/.venv/bin/pip" install aiohttp websockets soundfile numpy psutil >>"$LOG" 2>&1 || true
  fi
fi

# --- баннер в лог для проверок ---
{
  echo "[$(date '+%F %T')] START agent: model_path=\"$MODEL_PATH\" threads=$THREADS lang_hint=$LANG_HINT py=$("$PY" -V 2>&1)"
  command -v ffmpeg >/dev/null 2>&1 && echo "[$(date '+%F %T')] FFmpeg: $(ffmpeg -version 2>/dev/null | head -n1 | awk '{print $3}')"
  [ -n "${SERVER_WS:-}" ] && echo "[$(date '+%F %T')] WS: $SERVER_WS"
} >> "$LOG"

# --- watchdog: перезапуск при крэше с бэкоффом ---
backoff=2
while :; do
  "$PY" "$REPO/agent.py" >> "$LOG" 2>&1 || true
  echo "[$(date '+%F %T')] [warn] agent exited, restart in ${backoff}s" >> "$LOG"
  sleep "$backoff"
  backoff=$(( backoff < 60 ? backoff*2 : 60 ))
done
