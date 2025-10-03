#!/data/data/com.termux/files/usr/bin/bash
set -e
pkg update -y
pkg install -y git python ffmpeg openssl-tool wget curl make cmake clang tmux
# сборка whisper.cpp (если не собран)
if [ ! -d whisper.cpp ]; then
  git clone https://github.com/ggerganov/whisper.cpp.git
  cd whisper.cpp && make && cd ..
fi
echo "✅ Установка завершена"
