#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

pass() { printf "\033[1;32mPASS\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31mFAIL\033[0m %s\n" "$*"; }
info() { printf "\033[1;34mINFO\033[0m %s\n" "$*"; }

echo "===== Whisper / FFmpeg Diagnostics ====="

# 0) Базовая инфа
info "Python: $(python -V 2>&1)"
info "FFmpeg: $(ffmpeg -version 2>/dev/null | head -n1 || echo 'not found')"
info "CPU: $(getprop ro.product.cpu.abi 2>/dev/null || echo 'n/a')"
info "PATH=$PATH"
echo

# 1) Поиск бинаря whisper
WHISPER_BIN_ENV="${WHISPER_BIN:-}"
CANDIDATES=()

[ -n "$WHISPER_BIN_ENV" ] && CANDIDATES+=("$WHISPER_BIN_ENV")
WHICH1="$(command -v whisper-cli 2>/dev/null || true)"
WHICH2="$(command -v main 2>/dev/null || true)"
[ -n "$WHICH1" ] && CANDIDATES+=("$WHICH1")
[ -n "$WHICH2" ] && CANDIDATES+=("$WHICH2")
[ -f "./whisper-cli" ] && CANDIDATES+=("./whisper-cli")
[ -f "./main" ] && CANDIDATES+=("./main")
[ -f "./whisper.cpp/main" ] && CANDIDATES+=("./whisper.cpp/main")

FOUND_BIN=""
for b in "${CANDIDATES[@]}"; do
  if [ -x "$b" ]; then
    FOUND_BIN="$b"
    break
  fi
done

if [ -n "$WHISPER_BIN_ENV" ]; then
  info "WHISPER_BIN env: $WHISPER_BIN_ENV"
fi
info "Candidates tried: ${CANDIDATES[*]:-(none)}"

if [ -z "$FOUND_BIN" ]; then
  fail "Whisper binary NOT FOUND. Нужно собрать или указать путь (WHISPER_BIN)."
else
  pass "Whisper binary: $FOUND_BIN"
fi
echo

# 2) Проверка запуска бинаря (help)
if [ -n "$FOUND_BIN" ]; then
  if "$FOUND_BIN" -h >/dev/null 2>&1; then
    pass "Whisper binary runs: '$FOUND_BIN -h' OK"
  else
    fail "Whisper binary exists but does not run (rc!=0). Проверь зависимости/архитектуру."
  fi
fi
echo

# 3) Проверка модели
DEFAULT_MODEL="/sdcard/worker/models/ggml-medium-q5_0.bin"
MODEL_PATH_EFF="${MODEL_PATH:-$DEFAULT_MODEL}"
info "MODEL_PATH effective: $MODEL_PATH_EFF"
if [ -f "$MODEL_PATH_EFF" ]; then
  SIZE=$(stat -c%s "$MODEL_PATH_EFF" 2>/dev/null || stat -f%z "$MODEL_PATH_EFF" 2>/dev/null || echo 0)
  KB=$(( SIZE / 1024 ))
  pass "Model file exists. Size ≈ ${KB} KB"
  # эвристика: валидная модель обычно > 100 МБ
  if [ "$KB" -lt 102400 ]; then
    info "Размер кажется подозрительно маленьким (<100MB). Убедись, что это нужная модель."
  fi
else
  fail "Model not found at $MODEL_PATH_EFF"
fi
echo

# 4) FFmpeg
if command -v ffmpeg >/dev/null 2>&1; then
  pass "FFmpeg found"
else
  fail "FFmpeg NOT FOUND. Установи: pkg install ffmpeg"
fi
echo

# 5) Быстрая тест-команда (без аудио): только проверка возможности парсить опции
if [ -n "$FOUND_BIN" ] && [ -f "$MODEL_PATH_EFF" ]; then
  # Фиктивный вызов с заведомо отсутствующим файлом, чтобы увидеть понятную ошибку модель/файл
  set +e
  "$FOUND_BIN" -m "$MODEL_PATH_EFF" -f /nonexistent.wav -otxt -of /tmp/xx 2>&1 | head -n 3
  RC=$?
  set -e
  info "Test run exit code: $RC (127=бинарь не найден, 2=нет модели/файла, 0=успешно)"
fi

echo "===== End diagnostics ====="
