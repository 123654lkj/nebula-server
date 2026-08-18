#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星枢 LongMemEval harness（隔离实例，禁止灌现网库）

用法:
  python3 nebula_lme.py download
  # 另开终端:
  NEBULA_DB_PATH=$HOME/.local/state/nebula-lme/memory.db \\
  NEBULA_PORT=26671 NEBULA_BIND=127.0.0.1 \\
    python3 /opt/nebula/vector_memory_server.py
  python3 nebula_lme.py smoke --limit 20
  python3 nebula_lme.py run --split s --limit 50
  python3 nebula_lme.py report --run-dir ~/.local/state/nebula-lme/runs/<ts>

协议（对外主报）:
  Protocol A = session 整段写入 + created_at=haystack_date，不跑 session-extract。
  judge: 默认 lexical（含 answer 子串 / token overlap）；官方 gpt-4o 另接 evaluate_qa.py。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(os.environ.get("NEBULA_LME_ROOT", os.path.expanduser("~/.local/state/nebula-lme")))
DATA = ROOT / "data"
RUNS = ROOT / "runs"
BASE_DEFAULT = os.environ.get("NEBULA_LME_URL", "http://127.0.0.1:26770")

HF = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main"
FILES = {
    "oracle": "longmemeval_oracle.json",
    "s": "longmemeval_s_cleaned.json",
    "m": "longmemeval_m_cleaned.json",
}


def http(base: str, method: str, path: str, body: Optional[dict] = None, timeout: int = 180) -> Dict[str, Any]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_date(s: str) -> Optional[float]:
    if not s:
        return None
    s = str(s).strip()
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        try:
            return datetime(y, mo, d, hh, mm, ss).timestamp()
        except ValueError:
            pass
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10), ("%Y/%m/%d", 10)):
        try:
            return datetime.strptime(s[:n], fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def session_text(session: Any) -> str:
    if isinstance(session, str):
        return session.strip()
    turns = session if isinstance(session, list) else []
    lines = []
    for t in turns:
        if not isinstance(t, dict):
            continue
        role = t.get("role") or t.get("speaker") or "user"
        content = (t.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def download(split: str = "oracle") -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    names = [FILES["oracle"]] if split == "oracle" else [FILES.get(split, FILES["oracle"]), FILES["oracle"]]
    last = None
    for name in names:
        dest = DATA / name
        last = dest
        if dest.exists() and dest.stat().st_size > 1000:
            print(f"exists {dest} ({dest.stat().st_size} bytes)")
            continue
        url = f"{HF}/{name}"
        print(f"GET {url}")
        urllib.request.urlretrieve(url, dest)
        print(f"saved {dest} ({dest.stat().st_size} bytes)")
    return last


def load_split(split: str) -> List[Dict]:
    name = FILES.get(split) or FILES["oracle"]
    path = DATA / name
    if not path.exists():
        download(split)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("data") or data.get("instances") or []
    return list(data)


def ingest_question(base: str, item: Dict, qid: str) -> int:
    sessions = item.get("haystack_sessions") or []
    dates = item.get("haystack_dates") or []
    sids = item.get("haystack_session_ids") or []
    n = 0
    cat = f"lme:{qid}"
    for i, sess in enumerate(sessions):
        text = session_text(sess)
        if not text:
            continue
        date_s = dates[i] if i < len(dates) else ""
        sid = sids[i] if i < len(sids) else str(i)
        ts = parse_date(date_s)
        body = {
            "content": f"[session {sid} @ {date_s}]\n{text}"[:12000],
            "source": f"lme:{qid}",
            "category": cat,
            "importance": 0.55,
            "semantic_dedup": False,
            "metadata": {"memory_layer": "episodic", "abstract": f"LME {qid} session {sid}"},
        }
        if ts:
            body["created_at"] = ts
        http(base, "POST", "/memory/add", body, timeout=120)
        n += 1
    return n


def _read_namespace(qid: str) -> List[Dict[str, Any]]:
    """Oracle：该题全部 session 原文（不靠 snippet）。"""
    import sqlite3
    db = os.environ.get("NEBULA_DB_PATH") or str(ROOT / "memory.db")
    if not Path(db).exists():
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT content, created_at FROM memories WHERE category = ? AND ifnull(is_compressed,0)=0",
            (f"lme:{qid}",),
        ).fetchall()
    finally:
        con.close()
    return [{"content": c or "", "created_at": t} for c, t in rows]


def ask_question(base: str, item: Dict, qid: str, mode: str = "retrieve") -> Dict[str, Any]:
    q = item.get("question") or ""
    qdate = item.get("question_date") or ""
    as_of = parse_date(qdate)
    if mode == "oracle_all":
        sys_path_ok = True
        try:
            import sys
            if "/opt/nebula" not in sys.path:
                sys.path.insert(0, "/opt/nebula")
            from nebula_v5 import llm_read_answer
            packed = _read_namespace(qid)
            hyp = (llm_read_answer(q, packed) or "").strip()
        except Exception as e:
            hyp = f"[reader_fail] {e}"
            sys_path_ok = False
        return {
            "question_id": qid,
            "question": q,
            "question_type": item.get("question_type"),
            "gold": item.get("answer"),
            "hypothesis": hyp,
            "elapsed_ms": None,
            "mode": "oracle_all",
            "n_docs": len(packed) if sys_path_ok else 0,
        }
    body = {
        "query": q,
        "top_k": 8,
        "no_cache": True,
        "llm_deep": "off",
        "llm_answer": False,
        "reader": True,
        "rerank": False,
        "use_graph": False,
        "max_chars": 900,
        "max_total_chars": 6000,
        "category": f"lme:{qid}",
    }
    if as_of:
        body["as_of"] = as_of
    r = http(base, "POST", "/ask", body, timeout=180)
    hyp = (r.get("answer") or (r.get("composed") or {}).get("answer") or "").strip()
    return {
        "question_id": qid,
        "question": q,
        "question_type": item.get("question_type"),
        "gold": item.get("answer"),
        "hypothesis": hyp,
        "temporal_intent": (r.get("composed") or {}).get("temporal_intent"),
        "elapsed_ms": r.get("elapsed_ms"),
    }


def lexical_ok(gold: str, hyp: str) -> bool:
    g = (gold or "").strip().lower()
    h = (hyp or "").strip().lower()
    if not g or not h:
        return False
    if g in h:
        return True
    gtoks = [t for t in re.findall(r"[a-z0-9\u4e00-\u9fff]+", g) if len(t) > 1]
    if not gtoks:
        return False
    hit = sum(1 for t in gtoks if t in h)
    return hit / len(gtoks) >= 0.5


def run(base: str, split: str, limit: int, offset: int = 0, mode: str = "retrieve") -> Path:
    items = load_split(split)
    items = items[offset: offset + limit if limit else None]
    ts = time.strftime("%Y%m%d-%H%M%S")
    outdir = RUNS / f"{split}-{ts}"
    outdir.mkdir(parents=True, exist_ok=True)
    hyps = []
    t0 = time.time()
    for i, item in enumerate(items, 1):
        qid = str(item.get("question_id") or f"{split}-{i}")
        print(f"[{i}/{len(items)}] ingest {qid}", flush=True)
        n = ingest_question(base, item, qid)
        print(f"  sessions={n} ask...", flush=True)
        row = ask_question(base, item, qid, mode=mode)
        row["ingested"] = n
        row["lexical"] = lexical_ok(row.get("gold") or "", row.get("hypothesis") or "")
        hyps.append(row)
        print(f"  lexical={row['lexical']} type={row.get('question_type')} hyp={row['hypothesis'][:80]!r}", flush=True)
        (outdir / "hypothesis.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in hyps) + "\n", encoding="utf-8"
        )
    metrics = summarize(hyps)
    metrics.update({
        "split": split,
        "n": len(hyps),
        "elapsed_s": round(time.time() - t0, 1),
        "base": base,
        "protocol": "A-session-doc",
        "ask_mode": mode,
        "judge": "lexical_overlap>=0.5",
    })
    (outdir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    proto = (
        f"# 星枢 LME run {ts}\n\n"
        f"- dataset: longmemeval-cleaned `{split}`\n"
        f"- protocol: A (session as document, no extract LLM)\n"
        f"- judge: lexical (official gpt-4o evaluate_qa.py 另跑)\n"
        f"- ask: /ask llm_deep=off category=lme:{{qid}} as_of=question_date\n"
        f"- n={metrics['n']} overall={metrics.get('overall')}\n"
        f"- by_type: {json.dumps(metrics.get('by_type'), ensure_ascii=False)}\n"
    )
    (outdir / "PROTOCOL.md").write_text(proto, encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"wrote {outdir}")
    return outdir


def summarize(rows: List[Dict]) -> Dict[str, Any]:
    by: Dict[str, List[bool]] = {}
    for r in rows:
        t = r.get("question_type") or "unknown"
        by.setdefault(t, []).append(bool(r.get("lexical")))
    by_type = {k: round(sum(v) / len(v), 4) if v else 0 for k, v in sorted(by.items())}
    vals = [bool(r.get("lexical")) for r in rows]
    return {
        "overall": round(sum(vals) / len(vals), 4) if vals else 0,
        "by_type": by_type,
        "correct": sum(vals),
    }


def report(run_dir: str) -> None:
    p = Path(run_dir)
    print((p / "metrics.json").read_text(encoding="utf-8"))
    print((p / "PROTOCOL.md").read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["download", "smoke", "run", "report", "health"])
    ap.add_argument("--base", default=BASE_DEFAULT)
    ap.add_argument("--split", default="oracle", choices=list(FILES))
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--mode", default="retrieve", choices=["retrieve", "oracle_all"])
    ap.add_argument("--run-dir", default="")
    args = ap.parse_args()
    if args.cmd == "download":
        download(args.split)
        return
    if args.cmd == "health":
        print(json.dumps(http(args.base, "GET", "/health", timeout=5), ensure_ascii=False))
        return
    if args.cmd == "report":
        report(args.run_dir)
        return
    if args.cmd == "smoke":
        args.split = "oracle"
        if args.limit <= 0:
            args.limit = 20
        if args.mode == "retrieve":
            args.mode = "oracle_all"
    run(args.base, args.split, args.limit, args.offset, mode=args.mode)


if __name__ == "__main__":
    main()
