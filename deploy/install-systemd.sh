#!/usr/bin/env bash
set -euo pipefail
# 把当前包安装到 /opt/nebula 并注册 systemd（需 root）
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${NEBULA_INSTALL_DIR:-/opt/nebula}"
if [ "$(id -u)" -ne 0 ]; then echo "need root (sudo)"; exit 1; fi
mkdir -p "$DEST"
rsync -a --delete --exclude .git --exclude .venv --exclude data/*.db --exclude node_modules "$ROOT/" "$DEST/"
cd "$DEST"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; echo "edit $DEST/.env"; fi
# fix WorkingDirectory in units if DEST != /opt/nebula
for u in nebula-memory.service vault-nebula-sync.service; do
  sed "s#/opt/nebula#$DEST#g" "deploy/$u" > "/etc/systemd/system/$u"
done
cp deploy/vault-nebula-sync.timer deploy/nebula-lifecycle.service deploy/nebula-lifecycle.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nebula-memory.service
systemctl enable --now vault-nebula-sync.timer
systemctl enable --now nebula-lifecycle.timer
echo "installed: $DEST"
systemctl --no-pager --full status nebula-memory.service | head -15
