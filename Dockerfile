# syntax=docker/dockerfile:1.7
# ============================================================
#  Nexus 智枢 - 单 Dockerfile 多阶段构建
#    target=backend : Python 运行时（FastAPI + Agent Core）
#    target=web     : Nginx 静态站点 + /api 反向代理
#  前端由 Node 阶段构建成静态文件，不进入运行镜像。
# ============================================================

ARG PYTHON_VERSION=3.11
ARG NODE_VERSION=22

# ---------- 1) 前端构建 ----------
FROM node:${NODE_VERSION}-alpine AS frontend-build
WORKDIR /build/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ---------- 2) 后端运行时 ----------
FROM python:${PYTHON_VERSION}-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent ./agent
COPY backend ./backend
COPY config.example.yaml ./config.example.yaml
COPY docker/entrypoint.backend.sh ./docker/entrypoint.backend.sh

RUN mkdir -p /app/data /app/workspace \
    && chmod +x /app/docker/entrypoint.backend.sh

EXPOSE 8000

ENTRYPOINT ["/bin/sh", "/app/docker/entrypoint.backend.sh"]
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------- 3) Nginx 静态站点 + API 代理 ----------
FROM nginx:1.27-alpine AS web

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /build/frontend/dist /usr/share/nginx/html

EXPOSE 80