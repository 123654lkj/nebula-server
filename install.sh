#!/usr/bin/env bash
# 星枢完整安装：venv + 依赖 + 数据目录 + vault 模板
# 用法: ./install.sh [--systemd] [--port 26670]
set -euo pipefail
PORT="${NEBULA_PORT:-26670}"
INSTALL_SYSTEMD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --systemd) INSTALL_SYSTEMD=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --help|-h) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
echo "==> [1/5] Python"
command -v python3 >/dev/null || { echo "需要 python3"; exit 1; }
echo "==> [2/5] venv + deps"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$ROOT/requirements.txt"
echo "==> [3/5] data + vault"
mkdir -p "$ROOT/data" "$ROOT/vault/notes"
[ -f "$ROOT/.env" ] || cp "$ROOT/.env.example" "$ROOT/.env"
echo "==> [4/5] 自检"
"$VENV/bin/python" -c "import flask,numpy,requests; print('deps ok')"
echo "==> [5/5] 完成"
echo "  1) 编辑 $ROOT/.env 填入 BAILIAN_API_KEY"
echo "  2) 启动: ./start.sh   或   npm start   或   npm run docker:up"
echo "  3) 同步黑曜石笔记: npm run sync"
echo "  4) Obsidian 打开: $ROOT/vault"
if [ "$INSTALL_SYSTEMD" = "1" ]; then
  echo "  systemd: sudo bash deploy/install-systemd.sh"
fi
export NEBULA_PORT="$PORT"
