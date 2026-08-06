#!/usr/bin/env bash
# ============================================================
# 星枢 Nebula 一键部署脚本
# 用法:
#   ./install.sh                 # 安装到当前目录（venv + 依赖）
#   ./install.sh --systemd       # 额外注册 systemd 服务并开机自启
#   ./install.sh --port 26670    # 指定端口
#   ./install.sh --help
# ============================================================
set -euo pipefail

PORT="${NEBULA_PORT:-26670}"
INSTALL_SYSTEMD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --systemd) INSTALL_SYSTEMD=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --help|-h)
      sed -n '2,9p' "$0"
      exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
SERVICE_NAME="nebula-memory"

echo "==> [1/4] 检查 Python"
command -v python3 >/dev/null || { echo "错误: 需要 python3"; exit 1; }

echo "==> [2/4] 创建虚拟环境并安装依赖"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$ROOT/requirements.txt" -q
echo "    依赖安装完成（可选依赖见 requirements.txt 注释）"

echo "==> [3/4] 初始化数据目录"
mkdir -p "$ROOT/data"
if [ ! -f "$ROOT/data/.gitkeep" ]; then touch "$ROOT/data/.gitkeep"; fi

if [ -z "${BAILIAN_API_KEY:-}" ]; then
  if [ -f "$ROOT/.env" ]; then
    set -a; source "$ROOT/.env"; set +a
  else
    echo "    ⚠ 未检测到 BAILIAN_API_KEY。"
    echo "      复制 .env.example 为 .env 并填入你的 key："
    echo "        cp .env.example .env && vi .env"
    echo "      或直接 export BAILIAN_API_KEY=sk-xxx"
  fi
fi

echo "==> [4/4] 启动"
if [ "$INSTALL_SYSTEMD" = "1" ]; then
  cat > "/tmp/$SERVICE_NAME.service" <<EOF
[Unit]
Description=星枢 (Nebula) 向量记忆系统
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
EnvironmentFile=-$ROOT/.env
ExecStart=$VENV/bin/python $ROOT/vector_memory_server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  echo "    systemd 单元已生成，如需安装到系统执行："
  echo "      sudo cp /tmp/$SERVICE_NAME.service /etc/systemd/system/"
  echo "      sudo systemctl daemon-reload && sudo systemctl enable --now $SERVICE_NAME"
else
  echo "    (加 --systemd 可注册为 systemd 服务)"
  echo "    启动中: $VENV/bin/python $ROOT/vector_memory_server.py"
  exec "$VENV/bin/python" "$ROOT/vector_memory_server.py"
fi
