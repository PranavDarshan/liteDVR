#!/bin/sh
set -eu
echo '== platform =='
uname -a
getconf LONG_BIT
python3 --version
echo '== Python packages =='
python3 -c 'import aiohttp, aiosqlite; print("aiohttp", aiohttp.__version__); print("aiosqlite", aiosqlite.__version__)'
echo '== FFmpeg =='
ffmpeg -hide_banner -version | head -n 3
ffmpeg -hide_banner -protocols | grep -E 'rtsp|tcp' || true
echo '== storage =='
df -h /var/lib/litedvr 2>/dev/null || df -h /
