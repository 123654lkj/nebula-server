#!/usr/bin/env bash
# 星枢启动：加载 .env 后前台运行
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; # shellcheck disable=SC1091
  source "$ROOT/.env"; set +a; fi
export NEBULA_PORT="${NEBULA_PORT:-26670}"
export NEBULA_HOST="${NEBULA_HOST:-0.0.0.0}"
cd "$ROOT"
if [ -x "$ROOT/.venv/bin/python" ]; then
  exec "$ROOT/.venv/bin/python" "$ROOT/vector_memory_server.py"
else
  exec python3 "$ROOT/vector_memory_server.py"
fi
