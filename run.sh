#!/usr/bin/env bash
# 启动脚本：自动加载同目录下的 .env（若存在）再起服务
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
  echo "已加载 .env"
else
  echo "未找到 .env（将以 MOCK 模式运行）。可执行： cp .env.example .env 并填入 key"
fi
python3 server.py
