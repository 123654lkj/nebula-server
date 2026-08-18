#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星枢 Nebula v4 进化引擎
- trust 落库 + 回填
- memory_links 轻量关系图
- reflect_ask 反思式检索（多跳 + 关联扩展 + 缺口补搜）
- supersede / related API 支撑
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("nebula.v4")

TRUST_LEVELS = ("canon", "source", "synthesis", "hearsay", "superseded")
TRUST_RANK = {t: i for i, t in enumerate(TRUST_LEVELS)}

_STOP_FOLLOW = {
    "的", "了", "和", "是", "在", "与", "或", "什么", "怎么", "如何", "一下",
    "这个", "那个", "可以", "需要", "我们", "一个", "title", "chunk", "source",
    "path", "host", "read", "notes", "vault", "md", "txt", "json", "html",
    "http", "https", "com", "org", "the", "and", "for", "with", "from",
    "null", "true", "false", "none", "data", "file", "files", "line",
    "chunk", "wiki", "link", "回读", "命令", "相关", "文档", "参见",
}


def infer_trust(
    content: str = "",
    source_file: str = "",
    importance: float = 0.5,
    explicit: Optional[str] = None,
) -> str:
    """推断可信度层级。"""
    if explicit and explicit in TRUST_RANK:
        return explicit
    src = source_file or ""
    head = (content or "")[:500]
    if any(m in head for m in ("【已过时", "SUPERSEDED", "已废弃", "仅历史")):
        return "superseded"
    try:
        from nebula_site import is_vault_src, is_scratch_src, is_demote_src
        if is_scratch_src(src):
            return "hearsay"
        if is_vault_src(src):
            if is_demote_src(src):
                return "source"
            return "canon"
    except Exception:
        pass
    if src.startswith("vault:gateway/") or "GATEWAY_LOCK" in src:
        return "canon"
    if src.startswith("vault:notes/01-") or src.startswith("vault:notes/02-"):
        return "canon"
    if src.startswith("vault:"):
        return "canon"
    if "现行权威" in head or "Agent 必读" in head or "[现行" in head:
        return "canon"
    if "SESSIONS/" in src or ("SESSIONS" in src and src.endswith(".md")):
        return "hearsay"
    if src in ("session-extract", "minimax-auto-sync"):
        return "hearsay"
    if src in ("manual", "grok", "hermes", "api", "mcp", "tuanzi-distill") or not src:
        return "source" if float(importance or 0.5) >= 0.85 else "synthesis"
    if float(importance or 0.5) >= 0.9:
        return "source"
    return "synthesis"


def migrate_schema(conn) -> Dict[str, Any]:
    """幂等迁移：trust 列 + memory_links 表 + 索引。"""
    done = []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "trust" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN trust TEXT DEFAULT 'synthesis'")
        done.append("add_trust_column")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src_id INTEGER NOT NULL,
            dst_id INTEGER NOT NULL,
            rel TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created_at REAL,
            UNIQUE(src_id, dst_id, rel)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_links_src ON memory_links(src_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_links_dst ON memory_links(dst_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_trust ON memories(trust)"
    )
    conn.commit()
    done.append("memory_links_ok")
    return {"migrated": done}


def backfill_trust(conn, limit: int = 0, batch: int = 500) -> int:
    """回填 trust 字段。limit=0 表示全量。"""
    sql = "SELECT id, content, source_file, importance, trust FROM memories WHERE is_compressed=0"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    n = 0
    for mid, content, src, imp, old in rows:
        t = infer_trust(content or "", src or "", float(imp or 0.5))
        if old != t:
            conn.execute("UPDATE memories SET trust=? WHERE id=?", (t, mid))
            n += 1
        if n and n % batch == 0:
            conn.commit()
    conn.commit()
    return n


def rebuild_links(conn, max_per_source: int = 40) -> Dict[str, int]:
    """按 source_file 建 same_source 边；同前缀 vault 文件弱 related。"""
    conn.execute("DELETE FROM memory_links WHERE rel IN ('same_source','co_source_prefix')")
    rows = conn.execute(
        """
        SELECT id, source_file FROM memories
        WHERE is_compressed=0 AND source_file IS NOT NULL AND source_file != ''
        ORDER BY source_file, id
        """
    ).fetchall()
    by_src: Dict[str, List[int]] = {}
    for mid, src in rows:
        by_src.setdefault(src, []).append(mid)

    same = 0
    now = time.time()
    for src, ids in by_src.items():
        ids = ids[:max_per_source]
        if len(ids) < 2:
            continue
        # 链式 + 星型（第一个连其余）避免 O(n^2) 爆炸
        for i in range(len(ids) - 1):
            a, b = ids[i], ids[i + 1]
            conn.execute(
                """INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at)
                   VALUES(?,?,?,?,?)""",
                (a, b, "same_source", 1.0, now),
            )
            conn.execute(
                """INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at)
                   VALUES(?,?,?,?,?)""",
                (b, a, "same_source", 1.0, now),
            )
            same += 2
        hub = ids[0]
        for b in ids[2: min(len(ids), 12)]:
            conn.execute(
                """INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at)
                   VALUES(?,?,?,?,?)""",
                (hub, b, "same_source", 0.8, now),
            )
            same += 1

    # vault 同目录弱关联：取每个目录前 2 个 id 互连
    prefix_groups: Dict[str, List[int]] = {}
    for src, ids in by_src.items():
        if not src.startswith("vault:"):
            continue
        parts = src.split("/")
        if len(parts) >= 2:
            pref = "/".join(parts[:-1])
            prefix_groups.setdefault(pref, []).append(ids[0])
    related = 0
    for pref, ids in prefix_groups.items():
        ids = ids[:8]
        for i in range(len(ids)):
            for j in range(i + 1, min(i + 3, len(ids))):
                conn.execute(
                    """INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at)
                       VALUES(?,?,?,?,?)""",
                    (ids[i], ids[j], "co_source_prefix", 0.5, now),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at)
                       VALUES(?,?,?,?,?)""",
                    (ids[j], ids[i], "co_source_prefix", 0.5, now),
                )
                related += 2
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
    return {"same_source_writes": same, "related_writes": related, "total_links": total}


def related_ids(conn, memory_id: int, limit: int = 8) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT l.dst_id, l.rel, l.weight, m.content, m.trust, m.importance, m.source_file, m.category
        FROM memory_links l
        JOIN memories m ON m.id = l.dst_id
        WHERE l.src_id = ? AND m.is_compressed = 0
        ORDER BY l.weight DESC, m.importance DESC
        LIMIT ?
        """,
        (memory_id, limit),
    ).fetchall()
    out = []
    for dst, rel, w, content, trust, imp, src, cat in rows:
        out.append(
            {
                "id": dst,
                "rel": rel,
                "weight": w,
                "trust": trust,
                "importance": imp,
                "source_file": src,
                "category": cat,
                "content": (content or "")[:280],
            }
        )
    return out


def supersede_memory(
    conn,
    old_id: int,
    new_id: Optional[int] = None,
    note: str = "",
) -> Dict[str, Any]:
    """标记旧记忆 superseded，可选链接到新记忆。"""
    row = conn.execute(
        "SELECT id, content, importance FROM memories WHERE id=?", (old_id,)
    ).fetchone()
    if not row:
        return {"ok": False, "error": "not_found"}
    content = row[1] or ""
    if not content.startswith("【已过时") and "SUPERSEDED" not in content[:80]:
        prefix = f"【已过时 SUPERSEDED {time.strftime('%Y-%m-%d')}】"
        if note:
            prefix += note + "\n"
        content = prefix + content
    new_imp = min(float(row[2] or 0.5), 0.35)
    conn.execute(
        "UPDATE memories SET trust=?, importance=?, content=?, updated_at=? WHERE id=?",
        ("superseded", new_imp, content, time.time(), old_id),
    )
    if new_id:
        conn.execute(
            """INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at)
               VALUES(?,?,?,?,?)""",
            (old_id, new_id, "superseded_by", 1.0, time.time()),
        )
        conn.execute(
            """INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at)
               VALUES(?,?,?,?,?)""",
            (new_id, old_id, "supersedes", 1.0, time.time()),
        )
    conn.commit()
    return {"ok": True, "old_id": old_id, "new_id": new_id}


def improve_followups(query: str, results: List[Dict], max_followups: int = 3) -> List[str]:
    """更高质量的 follow-up 查询（过滤扩展名/噪声）。"""
    if not results:
        return []
    q_tokens = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", (query or "").lower()))
    scores: Dict[str, float] = {}
    for r in results[:8]:
        text = (r.get("content") or "") + " " + str(r.get("src") or r.get("source_file") or "")
        src = str(r.get("src") or r.get("source_file") or "")
        base = src.split("/")[-1]
        base = re.sub(r"\.(md|txt|json|py|yml|yaml)$", "", base, flags=re.I)
        if base and len(base) >= 3 and base.lower() not in _STOP_FOLLOW:
            scores[base] = scores.get(base, 0) + 5.0
        for t in re.findall(r"[\w\u4e00-\u9fff]{2,16}", text):
            tl = t.lower()
            if tl in _STOP_FOLLOW or tl in q_tokens:
                continue
            if t.isdigit() or len(t) <= 1:
                continue
            if re.fullmatch(r"[a-z]{1,3}", tl):
                continue
            w = 1.0
            boost_kw = ("禁止", "冻结", "权威", "故障", "端口", "架构", "skill", "agent", "phantun", "xray")
            if any(k in t for k in boost_kw):
                w = 3.0
            if len(t) >= 4:
                w *= 1.4
            if r.get("trust") == "canon":
                w *= 1.3
            scores[t] = scores.get(t, 0) + w
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    followups = []
    for term, sc in ranked:
        if sc < 2.0:
            continue
        if term.lower() in (query or "").lower():
            continue
        # 避免「query + md」
        if term.lower() in _STOP_FOLLOW:
            continue
        fq = f"{query} {term}".strip()
        if fq not in followups:
            followups.append(fq)
        if len(followups) >= max_followups:
            break
    return followups


def detect_gaps(query: str, packed: List[Dict]) -> List[str]:
    """根据首轮结果探测缺口，生成补搜 query。"""
    gaps = []
    trusts = [r.get("trust") for r in packed]
    if not packed:
        gaps.append(query)
        return gaps
    if "canon" not in trusts and "source" not in trusts:
        gaps.append(f"{query} 权威 现行")
    # 查询含网络词但结果无 gateway
    q = query.lower()
    blob = " ".join((r.get("content") or "") + str(r.get("src") or "") for r in packed).lower()
    if any(k in q for k in ("网关", "冻结", "代理", "phantun", "gateway")):
        if "gateway" not in blob and "冻结" not in blob:
            gaps.append(f"{query} GATEWAY_LOCK")
    if any(k in q for k in ("星枢", "记忆", "nebula")):
        if "26670" not in blob and "sop" not in blob and "ask" not in blob:
            gaps.append(f"{query} 星枢 SOP /ask")
    # 分数极低
    try:
        top = float(packed[0].get("score") or 0)
        if top < 0.02:
            gaps.append(f"{query} 结论 根因")
    except Exception:
        pass
    return gaps[:2]


def reflect_ask(
    manager,
    query: str,
    top_k: int = 5,
    max_chars: int = 280,
    max_total_chars: int = 1800,
    use_hybrid: bool = True,
    category: Optional[str] = None,
    use_graph: bool = True,
    hops: int = 2,
    drop_hearsay_if_canon: bool = True,
    max_synthesis: int = 1,
    temporal_intent: Optional[str] = None,
    as_of: Optional[float] = None,
    prefer_layers: Optional[list] = None,
    tenant_id: Optional[str] = None,
    precomputed_vector=None,
) -> Dict[str, Any]:
    """
    v4 反思检索：
    1) 首轮 hybrid
    2) follow-up + gap 补搜
    3) 图扩展 related
    4) pack 输出
    """
    from vector_memory import pack_results, results_token_stats, format_ask_pack

    t0 = time.time()
    all_raw: Dict[int, Dict] = {}
    stages = []
    timing: Dict[str, float] = {}

    def _ingest(raw_list, hop_tag, mult=1.0):
        for r in raw_list or []:
            mid = r.get("id")
            if mid is None:
                continue
            rr = dict(r)
            rr["score"] = float(r.get("score") or 0) * mult
            rr["hop"] = hop_tag
            if mid not in all_raw or rr["score"] > float(all_raw[mid].get("score") or 0):
                all_raw[mid] = rr

    # Stage 1
    t1 = time.time()
    raw1 = manager.search(
        query=query,
        top_k=max(top_k * 3, 10),
        category=category,
        use_hybrid=use_hybrid,
        enable_time_decay=True,
        temporal_intent=temporal_intent,
        as_of=as_of,
        tenant_id=tenant_id,
        precomputed_vector=precomputed_vector,
    )
    _ingest(raw1, 0, 1.0)
    timing["seed_ms"] = round((time.time() - t1) * 1000, 1)
    stages.append({"stage": "seed", "n": len(raw1 or []), "ms": timing["seed_ms"]})
    _vis = ("图片", "照片", "截图", "这张图", "图里", "看图", "image", "photo", "screenshot")
    q2 = query
    for _k in _vis:
        q2 = q2.replace(_k, " ")
    q2 = " ".join(q2.split())
    if q2 and q2 != query:
        try:
            raw_img = manager.search(
                query=q2,
                top_k=max(top_k, 4),
                category="image",
                use_hybrid=True,
                enable_time_decay=True,
                temporal_intent=temporal_intent,
                as_of=as_of,
                tenant_id=tenant_id,
                precomputed_vector=precomputed_vector,
            )
            _ingest(raw_img, 0, 1.25)
            stages.append({"stage": "image_recall", "q": q2, "n": len(raw_img or [])})
        except Exception as _ie:
            logger.warning("image_recall fail: %s", _ie)

    seed_packed = pack_results(
        list(all_raw.values()),
        query=query,
        top_k=max(top_k, 4),
        max_chars=max_chars,
        per_source=1,
        drop_superseded=True,
        drop_hearsay_if_canon=drop_hearsay_if_canon,
        max_synthesis=max_synthesis,
        compact=True,
        prefer_layers=prefer_layers,
    )

    followups = improve_followups(query, seed_packed, max_followups=2)
    gaps = detect_gaps(query, seed_packed)
    extra_q = []
    for q in followups + gaps:
        if q not in extra_q and q != query:
            extra_q.append(q)

    # [perf2] 强 seed 跳过 expand/graph
    try:
        top_sc = float(seed_packed[0].get("score") or 0) if seed_packed else 0.0
    except Exception:
        top_sc = 0.0
    trusts = {r.get("trust") for r in (seed_packed or [])}
    # canon hit => early exit; source needs score floor (RRF ~0.01-0.03)
    strong_seed = (
        len(seed_packed or []) >= 1
        and (
            "canon" in trusts
            or ("source" in trusts and top_sc >= 0.008)
            or top_sc >= 0.025
        )
    )

    if hops >= 2 and not strong_seed:
        t2 = time.time()
        expand_qs = extra_q[:2]
        for q in expand_qs:
            try:
                raw = manager._bm25_search(
                    query=q,
                    top_k=max(top_k * 2, 8),
                    enable_time_decay=True,
                    temporal_intent=temporal_intent,
                    as_of=as_of,
                    category=category,
                    tenant_id=tenant_id,
                )
                for r in raw or []:
                    if "score" not in r or r.get("score") is None:
                        r["score"] = float(r.get("bm25_score") or 0)
                if raw:
                    try:
                        raw = manager._apply_authority_boost(raw, explain=False)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("expand bm25 fail %s: %s", q[:40], e)
                raw = []
            _ingest(raw, 1, 0.88)
        timing["expand_ms"] = round((time.time() - t2) * 1000, 1)
        stages.append({"stage": "expand_bm25", "queries": expand_qs, "n": len(all_raw), "ms": timing["expand_ms"]})
    elif hops >= 2 and strong_seed:
        stages.append({"stage": "early_exit_strong_seed", "top": top_sc, "trusts": sorted(str(x) for x in trusts if x)})

    graph_added = 0
    if use_graph and hops >= 2 and not strong_seed:
        top_ids = [
            r["id"]
            for r in sorted(all_raw.values(), key=lambda x: float(x.get("score") or 0), reverse=True)[:5]
        ]
        for mid in top_ids:
            try:
                rels = related_ids(manager.conn, mid, limit=4)
            except Exception as e:
                logger.warning("related_ids fail: %s", e)
                continue
            for rel in rels:
                rid = rel["id"]
                if rid in all_raw:
                    continue
                # 构造最小结果结构并再权威加权
                item = {
                    "id": rid,
                    "content": rel.get("content") or "",
                    "score": 0.015 * float(rel.get("weight") or 1.0),
                    "trust": rel.get("trust"),
                    "importance": rel.get("importance"),
                    "source_file": rel.get("source_file"),
                    "category": rel.get("category"),
                    "hop": 2,
                }
                all_raw[rid] = item
                graph_added += 1
        if graph_added:
            # 补权威字段
            try:
                all_raw_list = manager._apply_authority_boost(list(all_raw.values()), explain=False)
                all_raw = {r["id"]: r for r in all_raw_list}
            except Exception:
                pass
        stages.append({"stage": "graph", "added": graph_added})

    merged = sorted(all_raw.values(), key=lambda x: float(x.get("score") or 0), reverse=True)
    packed = pack_results(
        merged,
        query=query,
        top_k=top_k,
        max_chars=max_chars,
        per_source=1,
        drop_superseded=True,
        drop_hearsay_if_canon=drop_hearsay_if_canon,
        max_synthesis=max_synthesis,
        compact=True,
        prefer_layers=prefer_layers,
    )
    # 若仍无 canon，放宽 hearsay 限制再 pack 一次
    if packed and not any(r.get("trust") == "canon" for r in packed):
        packed2 = pack_results(
            merged,
            query=query,
            top_k=top_k,
            max_chars=max_chars,
            per_source=1,
            drop_superseded=True,
            drop_hearsay_if_canon=False,
            max_synthesis=max_synthesis,
            compact=True,
            prefer_layers=prefer_layers,
        )
        if packed2:
            packed = packed2

    pack_text = format_ask_pack(query, packed, max_total_chars=max_total_chars)
    stats = results_token_stats(packed)
    stats["pack_chars"] = len(pack_text)
    stats["pack_est_tokens"] = max(1, int(len(pack_text) / 2.2))

    # 简短 answer_hint：取 top canon/source 首句
    answer_hint = ""
    for r in packed:
        if r.get("trust") in ("canon", "source"):
            answer_hint = (r.get("content") or "")[:180]
            break
    if not answer_hint and packed:
        answer_hint = (packed[0].get("content") or "")[:180]

    return {
        "status": "ok",
        "query": query,
        "pack": pack_text,
        "results": packed,
        "count": len(packed),
        "answer_hint": answer_hint,
        "followups": followups,
        "gaps": gaps,
        "stages": stages,
        "raw_candidates": len(all_raw),
        "token_stats": stats,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "timing": timing,
        "engine": "reflect_v4",
        "hint": "优先读 pack；vault 结果用 readback 回读原文；trust=superseded 勿执行",
    }


def apply_trust_on_add_fields(
    content: str,
    source: str,
    importance: float,
    metadata: Optional[Dict] = None,
) -> Tuple[str, Dict]:
    meta = dict(metadata or {})
    trust = infer_trust(content, source or "", importance, meta.get("trust"))
    meta["trust"] = trust
    return trust, meta
