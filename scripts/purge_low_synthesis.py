#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理低价值 synthesis，抬高 canon+source 占比（不碰 vault: 源）。

策略（保守）：
- trust=synthesis
- importance <= 0.35
- source_file 不是 vault:%
- 且满足：过短 / 测试垃圾 / 长期未访问且 importance<=0.3
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

DB = Path("/opt/nebula/data/memory_vectors.db")
BASE = "http://127.0.0.1:26670"
DRY = "--apply" not in sys.argv
LIMIT = 400  # 单次上限，防一次砍光


def http_delete(mid: int) -> bool:
    req = urllib.request.Request(f"{BASE}/memory/{mid}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            return True
    except urllib.error.HTTPError as e:
        # fallback sql
        return False
    except Exception:
        return False


def main() -> int:
    con = sqlite3.connect(str(DB), timeout=60)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, importance, access_count, length(content) AS clen,
               substr(content,1,80) AS preview, source_file, category
        FROM memories
        WHERE trust = 'synthesis'
          AND importance <= 0.35
          AND (source_file IS NULL OR source_file NOT LIKE 'vault:%')
        ORDER BY importance ASC, length(content) ASC
        """
    ).fetchall()

    victims = []
    for r in rows:
        preview = (r["preview"] or "").lower()
        junk = any(
            k in preview
            for k in (
                "ping test",
                "nebula-api-survey",
                "ignore",
                "test only",
                "hello world",
            )
        )
        short = (r["clen"] or 0) < 280
        stale = (r["access_count"] or 0) == 0 and (r["importance"] or 0) <= 0.3
        if junk or short or stale:
            victims.append(int(r["id"]))
        if len(victims) >= LIMIT:
            break

    print(json.dumps({"candidates": len(rows), "selected": len(victims), "dry": DRY, "sample_ids": victims[:12]}, ensure_ascii=False))
    if DRY:
        print("dry-run only; pass --apply to delete")
        return 0

    deleted = 0
    sql_deleted = 0
    for mid in victims:
        ok = http_delete(mid)
        if ok:
            deleted += 1
        else:
            try:
                con.execute("DELETE FROM memories WHERE id = ?", (mid,))
                sql_deleted += 1
            except Exception as e:
                print("fail", mid, e)
    con.commit()
    con.close()
    print(json.dumps({"api_deleted": deleted, "sql_deleted": sql_deleted, "total": deleted + sql_deleted}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
