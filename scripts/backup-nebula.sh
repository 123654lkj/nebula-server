#!/usr/bin/env bash
# 备份星枢 SQLite 库
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB="${NEBULA_DB_PATH:-$ROOT/data/memory_vectors.db}"
OUT_DIR="${NEBULA_BACKUP_DIR:-$ROOT/data/backups}"
mkdir -p "$OUT_DIR"
ts=$(date +%Y%m%d_%H%M%S)
if [ -f "$DB" ]; then
  gzip -c "$DB" > "$OUT_DIR/nebula_${ts}.db.gz"
  echo "backup -> $OUT_DIR/nebula_${ts}.db.gz"
  # keep last 14
  ls -1t "$OUT_DIR"/nebula_*.db.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
else
  echo "no db at $DB" >&2
  exit 1
fi
