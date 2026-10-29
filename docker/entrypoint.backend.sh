#!/bin/sh
# 后端容器入口：
# 1) 首次启动时从 config.example.yaml 生成 config.yaml（git clone 里没有 config.yaml）
# 2) 确保挂载目录存在
# 3) 交给 uvicorn 主进程（exec，保证信号能直达 Python）
set -eu

if [ ! -f /app/config.yaml ]; then
    cp /app/config.example.yaml /app/config.yaml
    echo "[MyAi] 未找到 config.yaml，已从 config.example.yaml 生成。"
    echo "[MyAi] 如需自定义模型/审批策略，请在宿主机准备 config.yaml 并挂载到 /app/config.yaml。"
fi

mkdir -p /app/data /app/workspace

exec "$@"