#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星枢 Nebula v5 — 终极形态引擎（v6 抽取层已接）
================================
在 v4 (trust/links/reflect) 之上：
1. ultimate_ask：reflect + 弱结果 LLM 深改写 + 答案合成
2. compose_answer：claim + evidence + confidence（可执行契约）
3. lifecycle_run：synthesis 衰减/摘要晋升、不伤 vault/canon
4. entity_topic_links：主题共现边
5. bootstrap_context：会话启动 L0 注入包（省 token）
6. session_extract v6：memory_layer + abstract + episode（见 nebula_session_extract）
"""
from __future__ import annotations

import json
import logging
import re
import time
import threading
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nebula.v5")
try:
    from nebula_meta import RELEASE as _NEBULA_VER
except Exception:
    _NEBULA_VER = "v5.1.0"


def _llm_chat(system: str, user: str, max_tokens: int = 400, temperature: float = 0.3,
              extra: Optional[Dict[str, Any]] = None) -> str:
    """统一 LLM 调用（MiniMax / 百炼兼容接口）。"""
    import os
    api_key = (
        os.environ.get("NEBULA_LLM_API_KEY")
        or os.environ.get("BAILIAN_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or ""
    )
    if not api_key:
        return ""
    try:
        import requests
        base = os.environ.get(
            "BAILIAN_CHAT_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        if not (base or "").strip():
            base = "https://api.minimaxi.com/v1/chat/completions"
        model = os.environ.get("NEBULA_LLM_MODEL") or "MiniMax-M3"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if extra:
            payload.update(extra)
        resp = requests.post(
            base,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=float(os.environ.get("NEBULA_LLM_TIMEOUT", "8")),  # extract 可调高
        )
        if resp.status_code != 200:
            logger.warning("llm http %s %s", resp.status_code, resp.text[:200])
            return ""
        msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = "".join(
                (p.get("text") or p.get("content") or "") if isinstance(p, dict) else str(p)
                for p in text
            )
        raw = text or ""
        text = re.sub(r"<think[^>]*>.*?</[^>]*think[^>]*>", "", raw, flags=re.DOTALL)
        text = re.sub(r"<think[^>]*>.*", "", text, flags=re.DOTALL)
        text = (text or "").strip()
        if not text:
            # MiniMax-M3 思考模型：答案可能在 reasoning_content，或整段落在 <think>
            alt = msg.get("reasoning_content") or msg.get("reasoning") or ""
            if isinstance(alt, list):
                alt = "".join(str(x) for x in alt)
            text = (alt or raw or "").strip()
        return text
    except Exception as e:
        logger.warning("llm fail: %s", e)
        return ""


def _is_truth_src(src: str, trust: str = "") -> bool:
    s = str(src or "")
    return (
        s.startswith("vault:")
        or s.startswith("notes/")
        or s.startswith("gateway/")
        or "/opt/gateway/" in s
        or trust == "canon"
    )


def _query_tokens(query: str) -> List[str]:
    """英文标识 + 中文二字切分。停用词不计入重合。"""
    stop = {
        "的", "了", "和", "是", "在", "与", "或", "什么", "怎么", "如何", "一下",
        "这个", "那个", "一个", "能否", "能不能", "the", "and", "for", "how", "what",
    }
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", query or "")
    out: List[str] = []
    for t in raw:
        tl = t.lower()
        if re.fullmatch(r"[\u4e00-\u9fff]+", t) and len(t) > 2:
            for i in range(len(t) - 1):
                bg = t[i : i + 2]
                if bg not in stop and bg not in out:
                    out.append(bg)
        elif tl not in stop and t not in out:
            out.append(t)
    return out


def _overlap_hits(query: str, blob: str) -> Tuple[int, int]:
    toks = _query_tokens(query)
    b = (blob or "").lower()
    return sum(1 for t in toks if t.lower() in b), len(toks)


def compose_answer(query: str, packed: List[Dict]) -> Dict[str, Any]:
    """从 pack 结果合成可执行答案契约（抽取式，默认不调 LLM）。"""
    if not packed:
        return {
            "answer": "未命中可靠记忆。请扩大 query，或回读 L1 权威原文。",
            "confidence": 0.0,
            "executable": False,
            "evidence": [],
            "warnings": ["empty_results"],
        }

    evidence = []
    warnings = []
    has_canon = False
    has_super = False
    for r in packed:
        trust = r.get("trust") or "synthesis"
        if trust == "canon":
            has_canon = True
        if trust == "superseded":
            has_super = True
            continue
        evidence.append(
            {
                "id": r.get("id"),
                "trust": trust,
                "score": r.get("score"),
                "rerank_score": r.get("rerank_score"),
                "src": r.get("src") or r.get("source_file"),
                "readback": r.get("readback"),
                "snippet": (r.get("content") or "")[:220],
            }
        )

    # 顶已是真理库（pin/pack 已决）就别换。只有顶是会话碎片时，才换「重合够」的笔记
    pick = evidence[0] if evidence else {}
    if pick and not _is_truth_src(str(pick.get("src") or ""), pick.get("trust") or ""):
        truth = []
        for ev in evidence:
            if not _is_truth_src(str(ev.get("src") or ""), ev.get("trust") or ""):
                continue
            hits, ntok = _overlap_hits(
                query, (ev.get("snippet") or "") + " " + str(ev.get("src") or "")
            )
            need = 3 if ntok >= 5 else (2 if ntok >= 3 else 1)
            if ntok == 0 or hits >= need:
                ev = dict(ev)
                ev["_hits"] = hits
                truth.append(ev)
        if truth:
            pick = max(
                truth,
                key=lambda ev: (
                    float(ev.get("rerank_score") or 0),
                    float(ev.get("_hits") or 0),
                    float(ev.get("score") or 0),
                ),
            )
    top_snip = (pick.get("snippet") or "").replace("\n", " ").strip()
    answer = top_snip[:600]
    top_trust = (pick.get("trust") if pick else "") or "synthesis"
    if top_trust == "canon":
        conf = 0.88
        executable = True
    elif top_trust == "source":
        conf = 0.65
        executable = True
        warnings.append("top_is_source")
    else:
        # 精排已经把这条选成第一，不再用绝对分阈值（候选只有 4 条时满分才 4）
        if packed and packed[0].get("rerank_score") is not None:
            conf = 0.55
            executable = True
            warnings.append("top_synthesis_reranked")
        else:
            conf = 0.40
            executable = False
            warnings.append("top_low_trust")
    if has_canon and top_trust != "canon":
        warnings.append("canon_present_not_top")
    if has_super:
        warnings.append("superseded_present_filtered")

    # 读回指令
    readbacks = [e["readback"] for e in evidence if e.get("readback")][:3]

    return {
        "answer": answer or "（有命中但正文为空）",
        "confidence": conf,
        "executable": executable,
        "evidence": evidence[:6],
        "readbacks": readbacks,
        "warnings": warnings,
        "query": query,
    }


def needs_llm_deep(packed: List[Dict], reflect_meta: Dict) -> bool:
    """极弱结果才触发 LLM 深改写 — [perf] 避免闲聊/噪声拖到数秒。"""
    if not packed:
        return True
    try:
        top = float(packed[0].get("score") or 0)
    except Exception:
        top = 0.0
    trusts = [r.get("trust") for r in packed]
    # 已有 canon/source 且分数尚可 → 不深搜
    if ("canon" in trusts or "source" in trusts) and top >= 0.01:
        return False
    # 仅当 top 极弱（几乎没命中）才 deep
    if top < 0.008:
        return True
    # gaps 且无 canon 时仍 deep 一次
    if reflect_meta.get("gaps") and "canon" not in trusts and top < 0.015:
        return True
    return False


def llm_deep_queries(query: str, packed: List[Dict], n: int = 3) -> List[str]:
    """LLM 根据缺口生成补搜 query。"""
    snippets = []
    for r in packed[:4]:
        snippets.append(f"- [{r.get('trust')}] {(r.get('content') or '')[:120]}")
    ctx = "\n".join(snippets) if snippets else "(无)"
    system = (
        f"你是记忆检索策略器。根据用户问题与已有命中，生成{n}条不同角度的中文检索 query。"
        "每行一条，不要编号不要解释。要具体（含服务名/路径/错误关键词），禁止空泛。"
    )
    user = f"问题：{query}\n已有命中：\n{ctx}\n请输出{n}条补搜 query："
    text = _llm_chat(system, user, max_tokens=180, temperature=0.4)
    if not text:
        # 降级：规则
        return [f"{query} 权威 现行", f"{query} 故障 修复", f"{query} 配置"]
    qs = []
    for line in text.splitlines():
        line = re.sub(r"^[\d\.\-\*\s]+", "", line.strip())
        if line and line != query and len(line) >= 4:
            qs.append(line[:80])
    return qs[:n] or [query]


def llm_polish_answer(query: str, composed: Dict[str, Any]) -> Optional[str]:
    """可选：用 LLM 把 evidence 压成 3 句可执行结论（有 canon 时更稳）。"""
    if not composed.get("evidence"):
        return None
    if composed.get("confidence", 0) < 0.5:
        return None  # 低置信不让模型编
    ev_lines = []
    for e in composed["evidence"][:5]:
        ev_lines.append(f"#{e['id']}[{e['trust']}] {e['snippet'][:160]}")
    system = (
        "你是记忆裁决助手。只根据证据写 2-4 句中文结论。"
        "禁止编造证据没有的内容。若证据冲突，标明以 canon/GATEWAY 为准。"
        "输出纯结论，不要开场白。"
    )
    user = f"问题：{query}\n证据：\n" + "\n".join(ev_lines)
    text = _llm_chat(system, user, max_tokens=280, temperature=0.2)
    return text or None


def llm_read_answer(query: str, packed: List[Dict]) -> Optional[str]:
    """评测阅读器：先用日期条硬算 first/间隔，算不出再问 LLM。"""
    if not packed:
        return None
    try:
        from vector_memory import extract_dated_events, solve_temporal
    except Exception:
        extract_dated_events = None
        solve_temporal = None
    ev = []
    all_events: List[Dict] = []
    for r in packed[:6]:
        raw = r.get("full") or r.get("content") or ""
        raw_facts = r.get("dated_facts")
        events = []
        if raw_facts and isinstance(raw_facts[0], dict):
            events = list(raw_facts)
        elif extract_dated_events:
            events = extract_dated_events(raw, as_of=r.get("created_at"))
        all_events.extend(events or [])
        ts = r.get("created_at")
        head = f"t={ts} " if ts else ""
        ev.append(f"{head}{raw[:3500]}")
    if solve_temporal:
        computed = solve_temporal(query, all_events)
        if computed:
            return computed
    fact_block = "\n".join(
        f"- {e.get('date')} | {e.get('sent')}" if isinstance(e, dict) else f"- {e}"
        for e in all_events[:16]
    ) or "(none)"
    system = (
        "You answer using ONLY Dated facts and Evidence. "
        "For which-first / which-earlier: pick the event with the earlier date. "
        "For how-many-days: subtract the two dates and reply like '7 days'. "
        "Short phrase only. No preamble. "
        "If a dated fact is related, you must answer — do not say you don't know."
    )
    user = f"Question: {query}\n\nDated facts:\n{fact_block}\n\nEvidence:\n" + "\n---\n".join(ev)
    text = _llm_chat(system, user, max_tokens=400, temperature=0.0) or ""
    text = re.sub(r"<think[^>]*>.*?</[^>]*think[^>]*>", "", text, flags=re.DOTALL)
    if "<think" in text.lower():
        text = text.split("</think>")[-1]
    text = text.strip().strip('"')
    if text.lower().startswith("answer:"):
        text = text.split(":", 1)[1].strip()
    return text or None


def _apply_llm_rerank(query: str, packed: List[Dict], top_k: int) -> Tuple[List[Dict], bool]:
    """Stage 2：qwen3-rerank 精排后再截 top_k。种子已答则跳过。"""
    if not packed or len(packed) <= 1:
        return packed[:top_k], False
    try:
        from reranker import apply_rerank, _rerank_enabled, seed_already_answers
        if not _rerank_enabled():
            return packed[:top_k], False
        if seed_already_answers(query, packed[0]):
            packed[0]["rerank_score"] = packed[0].get("rerank_score")
            packed[0]["rerank_skip"] = "seed_answers"
            return packed[:top_k], False
        wide = apply_rerank(query, list(packed), candidates=len(packed))
        return wide[:top_k], any(r.get("rerank_score") is not None for r in wide[:top_k])
    except Exception as e:
        logger.warning("rerank fail: %s", e)
        return packed[:top_k], False


def ultimate_ask(
    manager,
    query: str,
    top_k: int = 5,
    max_chars: int = 280,
    max_total_chars: int = 1800,
    use_hybrid: bool = True,
    category: Optional[str] = None,
    use_graph: bool = True,
    hops: int = 2,
    llm_deep: str = "auto",  # auto|on|off
    llm_answer: bool = True,
    rerank: bool = True,
    temporal_intent: Optional[str] = None,
    as_of: Optional[float] = None,
    prefer_layers: Optional[list] = None,
    reader: bool = False,
    tenant_id: Optional[str] = None,
    precomputed_vector=None,
) -> Dict[str, Any]:
    """终极检索：v4 reflect → 条件 LLM 深搜 → qwen3-rerank → 答案合成。"""
    from nebula_v4 import reflect_ask
    from vector_memory import pack_results, format_ask_pack, results_token_stats

    t0 = time.time()
    if str(__import__("os").environ.get("NEBULA_RERANK", "1")).lower() in ("0", "false", "off", "no"):
        rerank = False
    # 少扩池：扩太宽会灌进弱相关 canon，精排再把短指针顶上去
    wide_k = max(top_k + 2, 6) if rerank else top_k
    base = reflect_ask(
        manager,
        query=query,
        top_k=wide_k,
        max_chars=max_chars,
        max_total_chars=max_total_chars,
        use_hybrid=use_hybrid,
        category=category,
        use_graph=use_graph,
        hops=hops,
        drop_hearsay_if_canon=True,
        max_synthesis=3 if rerank else 1,
        temporal_intent=temporal_intent,
        as_of=as_of,
        prefer_layers=prefer_layers,
        tenant_id=tenant_id,
        precomputed_vector=precomputed_vector,
    )
    packed = base.get("results") or []
    stages = list(base.get("stages") or [])
    deep_used = False
    deep_queries: List[str] = []

    do_deep = llm_deep == "on" or (llm_deep == "auto" and needs_llm_deep(packed, base))
    if do_deep:
        deep_queries = llm_deep_queries(query, packed, n=2)  # [perf] 2 路足够
        all_raw = {r["id"]: r for r in packed}
        # [perf] 深补搜用 BM25，避免再打远程 embed（LLM 已是主耗时）
        for q in deep_queries:
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
                    if r.get("score") is None:
                        r["score"] = float(r.get("bm25_score") or 0)
                if raw:
                    try:
                        raw = manager._apply_authority_boost(raw, explain=False)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("llm_deep bm25 fail: %s", e)
                raw = []
            for r in raw or []:
                mid = r.get("id")
                if mid is None:
                    continue
                rr = dict(r)
                rr["score"] = float(r.get("score") or 0) * 0.9
                rr["hop"] = "llm_deep"
                if mid not in all_raw or rr["score"] > float(all_raw[mid].get("score") or 0):
                    all_raw[mid] = rr
        merged = sorted(all_raw.values(), key=lambda x: float(x.get("score") or 0), reverse=True)
        packed = pack_results(
            merged,
            query=query,
            top_k=wide_k,
            max_chars=max_chars,
            per_source=1,
            drop_superseded=True,
            drop_hearsay_if_canon=True,
            max_synthesis=3 if rerank else 1,
            compact=True,
            prefer_layers=prefer_layers,
        )
        deep_used = True
        stages.append({"stage": "llm_deep", "queries": deep_queries, "n": len(all_raw)})

    rerank_used = False
    if rerank:
        packed, rerank_used = _apply_llm_rerank(query, packed, top_k)
        if rerank_used:
            stages.append({
                "stage": "rerank",
                "backend": "qwen3-rerank",
                "model": __import__("os").environ.get("NEBULA_RERANK_MODEL") or "qwen3-rerank",
                "n": len(packed),
            })
        elif packed and packed[0].get("rerank_skip"):
            stages.append({"stage": "rerank_skip", "reason": packed[0].get("rerank_skip")})
    else:
        packed = packed[:top_k]

    # rerank 不得掀文件名/锁：GATEWAY_LOCK.md 必须压过事故分析笔记
    try:
        from vector_memory import pin_exact_matches
        before = [r.get("id") for r in packed[:3]]
        packed = pin_exact_matches(query, packed)
        after = [r.get("id") for r in packed[:3]]
        if before != after:
            stages.append({"stage": "pin_exact", "keys": True, "moved": True})
    except Exception:
        pass

    pack_text = format_ask_pack(query, packed, max_total_chars=max_total_chars)
    stats = results_token_stats(packed)
    stats["pack_chars"] = len(pack_text)
    stats["pack_est_tokens"] = max(1, int(len(pack_text) / 2.2))

    composed = compose_answer(query, packed)
    if temporal_intent:
        composed["temporal_intent"] = temporal_intent
    if as_of is not None:
        composed["as_of"] = as_of
    polished = None
    if reader:
        polished = llm_read_answer(query, packed)
        if polished:
            composed["reader"] = True
    elif llm_answer and composed.get("executable") and composed.get("confidence", 0) >= 0.6:
        polished = llm_polish_answer(query, composed)
    if polished:
        composed["answer_llm"] = polished
        composed["answer_final"] = polished
        stages.append({"stage": "llm_answer"})
    else:
        composed["answer_final"] = composed.get("answer")

    # 契约块（Agent 直接可贴）
    contract_lines = [
        f"【星枢v5裁决】q={query[:60]} conf={composed['confidence']:.2f} exec={composed['executable']}",
        f"结论：{composed.get('answer_final') or ''}",
    ]
    if composed.get("readbacks"):
        contract_lines.append("回读：" + " | ".join(composed["readbacks"][:2]))
    if composed.get("warnings"):
        contract_lines.append("警告：" + ",".join(composed["warnings"]))
    contract = "\n".join(contract_lines)

    return {
        "status": "ok",
        "query": query,
        "pack": pack_text,
        "contract": contract,
        "answer": composed.get("answer_final"),
        "composed": composed,
        "results": packed,
        "count": len(packed),
        "followups": base.get("followups") or [],
        "gaps": base.get("gaps") or [],
        "deep_queries": deep_queries,
        "llm_deep_used": deep_used,
        "rerank_used": rerank_used,
        "stages": stages,
        "raw_candidates": base.get("raw_candidates"),
        "token_stats": stats,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "timing": base.get("timing") or {},
        "engine": "ultimate_v5",
        "version": _NEBULA_VER,
        "hint": "优先用 contract 或 pack；executable=false 时不要当事实执行；vault 必须 readback",
    }


def bootstrap_context(
    manager,
    focus_query: str = "",
    budget_chars: int = 2400,
) -> Dict[str, Any]:
    """会话启动 L0 注入：焦点检索 + 固定权威指针。

    快路径：hops=1 / 无图扩展 / 关 LLM，目标冷启动 <1.5s、热缓存 <50ms。
    """
    t0 = time.time()
    parts = []
    used = 0
    meta = {"engine": "bootstrap_v5", "fast": True, "layer_hint": "pack prefer abstract"}

    # 1) 固定权威提示（短）
    header = (
        "【L0星枢】权威：GATEWAY_LOCK>虎虎笔记>星枢chunk；密钥走 Vaultwarden；"
        "网络默认冻结。检索用 /ask engine=ultimate。\n"
    )
    parts.append(header)
    used += len(header)

    # 2) 焦点 ultimate（启动快路径）
    fq = (focus_query or "").strip() or "虎虎现行架构 星枢用法 工作流"
    ult = ultimate_ask(
        manager,
        query=fq,
        top_k=4,
        max_chars=220,
        max_total_chars=min(1600, budget_chars - used - 200),
        use_hybrid=True,
        use_graph=False,
        hops=1,  # 不做 follow-up 多跳 embed
        llm_deep="off",
        llm_answer=False,
        rerank=False,  # 启动路径不打 MiniMax 重排
    )
    pack = ult.get("pack") or ""
    contract = ult.get("contract") or ""
    block = (contract + "\n" + pack)[: max(400, budget_chars - used)]
    parts.append(block)
    used += len(block)
    meta["focus"] = fq
    meta["token_stats"] = ult.get("token_stats")
    meta["confidence"] = (ult.get("composed") or {}).get("confidence")
    meta["ult_elapsed_ms"] = ult.get("elapsed_ms")

    text = "\n".join(parts)
    if len(text) > budget_chars:
        text = text[: budget_chars - 10] + "…"
    return {
        "status": "ok",
        "bootstrap": text,
        "chars": len(text),
        "est_tokens": max(1, int(len(text) / 2.2)),
        "meta": meta,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "version": _NEBULA_VER,
    }


def build_entity_topic_links(conn, limit_memories: int = 800) -> Dict[str, int]:
    """从高价值记忆抽主题词，共现建 topic_cooccur 边。"""
    rows = conn.execute(
        """
        SELECT id, content, trust, importance FROM memories
        WHERE is_compressed=0 AND trust IN ('canon','source')
        ORDER BY importance DESC LIMIT ?
        """,
        (limit_memories,),
    ).fetchall()
    # 词 -> ids
    word_ids: Dict[str, List[int]] = defaultdict(list)
    stop = {
        "这个", "一个", "我们", "可以", "需要", "如果", "已经", "以及", "通过",
        "使用", "进行", "相关", "内容", "系统", "服务", "配置", "问题", "处理",
    }
    for mid, content, trust, imp in rows:
        text = content or ""
        # 英文标识 + 中文 2-6 字
        toks = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,24}|[\u4e00-\u9fff]{2,6}", text)
        seen = set()
        for t in toks[:40]:
            tl = t.lower() if t.isascii() else t
            if tl in stop or t.isdigit():
                continue
            if tl in seen:
                continue
            seen.add(tl)
            word_ids[tl].append(mid)

    now = time.time()
    # 清旧 topic 边
    conn.execute("DELETE FROM memory_links WHERE rel='topic_cooccur'")
    wrote = 0
    for w, ids in word_ids.items():
        if len(ids) < 2 or len(ids) > 40:
            continue
        # 星型：第一个连后 5 个
        hub = ids[0]
        for dst in ids[1:6]:
            if hub == dst:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at)
                   VALUES(?,?,?,?,?)""",
                (hub, dst, "topic_cooccur", 0.6, now),
            )
            conn.execute(
                """INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at)
                   VALUES(?,?,?,?,?)""",
                (dst, hub, "topic_cooccur", 0.6, now),
            )
            wrote += 2
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
    return {"topic_writes": wrote, "total_links": total, "topics": len(word_ids)}


def lifecycle_run(
    manager,
    demote_days: int = 21,
    demote_max: int = 300,
    digest: bool = True,
) -> Dict[str, Any]:
    """
    生命周期治理（安全）：
    - 老旧低价值 synthesis：importance 下调，不删 vault/canon/source
    - 可选：把一批 synthesis 压成 1 条 digest 记忆（不 is_compressed 源，只降权）
    """
    conn = manager.conn
    now = time.time()
    cutoff = now - demote_days * 86400
    # 降权
    cur = conn.execute(
        """
        SELECT id, importance, content FROM memories
        WHERE is_compressed=0
          AND (trust='synthesis' OR trust IS NULL OR trust='')
          AND (source_file IS NULL OR (
                source_file NOT LIKE 'vault:%'
                AND source_file NOT IN ('grok','manual')
              ))
          AND importance > 0.35
          AND created_at < ?
        ORDER BY importance DESC
        LIMIT ?
        """,
        (cutoff, demote_max),
    )
    rows = cur.fetchall()
    demoted = 0
    samples = []
    for mid, imp, content in rows:
        # 保护：正文像现行权威
        head = (content or "")[:200]
        if "现行权威" in head or "GATEWAY_LOCK" in head:
            continue
        new_imp = max(0.25, float(imp or 0.5) * 0.55)
        conn.execute(
            "UPDATE memories SET importance=?, updated_at=? WHERE id=?",
            (new_imp, now, mid),
        )
        demoted += 1
        if len(samples) < 5:
            samples.append(mid)
    conn.commit()

    digest_id = None
    if digest and demoted >= 10:
        # 抽一批正文做 digest 记忆
        cur = conn.execute(
            """
            SELECT id, substr(content,1,160) FROM memories
            WHERE id IN ({})
            """.format(",".join("?" * len(samples[:5]))),
            samples[:5],
        )
        bits = [f"#{a}:{b}" for a, b in cur.fetchall()]
        day = time.strftime("%Y-%m-%d")
        body = (
            f"[{day}][lifecycle] synthesis 衰减摘要：本次降权 {demoted} 条"
            f"（>{demote_days}天低价值 synthesis）。样例：\n"
            + "\n".join(bits)
            + "\n说明：源记忆未删除，仅降 importance；vault/canon 不受影响。"
        )
        try:
            r = manager.add(
                content=body,
                source="lifecycle",
                category="fact",
                importance=0.55,
                metadata={"trust": "source", "kind": "lifecycle_digest"},
            )
            digest_id = r.get("id")
        except Exception as e:
            logger.warning("digest add fail: %s", e)

    # 实体边
    try:
        topic = build_entity_topic_links(conn)
    except Exception as e:
        topic = {"error": str(e)}

    return {
        "demoted": demoted,
        "demote_days": demote_days,
        "samples": samples,
        "digest_id": digest_id,
        "topic_links": topic,
        "ts": now,
    }


# health 整包短缓存（避免每次 bw status 1.4s）
_HEALTH_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}
_HEALTH_TTL = 60.0
_BW_STATUS_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None, "refreshing": False}
_BW_STATUS_TTL = 300.0


def _refresh_bw_status() -> None:
    try:
        from nebula_secrets import bw_status
        st = bw_status() or {}
    except Exception:
        st = {"ok": False, "status": "error"}
    _BW_STATUS_CACHE["ts"] = time.time()
    _BW_STATUS_CACHE["payload"] = st
    _BW_STATUS_CACHE["refreshing"] = False


def _cached_bw_status() -> Dict[str, Any]:
    """health 不得同步等 bw CLI（实测 ~1.4s）。过期也先回缓存，后台刷新。"""
    now = time.time()
    hit = _BW_STATUS_CACHE.get("payload")
    ts = float(_BW_STATUS_CACHE.get("ts") or 0)
    fresh = hit is not None and (now - ts) < _BW_STATUS_TTL
    if not fresh and not _BW_STATUS_CACHE.get("refreshing"):
        _BW_STATUS_CACHE["refreshing"] = True
        threading.Thread(target=_refresh_bw_status, daemon=True).start()
    if hit is not None:
        return hit
    return {"ok": None, "status": "pending"}


def health_report(manager) -> Dict[str, Any]:
    now = time.time()
    if _HEALTH_CACHE["payload"] is not None and now - float(_HEALTH_CACHE["ts"]) < _HEALTH_TTL:
        out = dict(_HEALTH_CACHE["payload"])
        out["cached"] = True
        return out
    conn = manager.conn
    trust = dict(conn.execute("SELECT trust, COUNT(*) FROM memories GROUP BY trust").fetchall())
    links = dict(conn.execute("SELECT rel, COUNT(*) FROM memory_links GROUP BY rel").fetchall())
    total = conn.execute("SELECT COUNT(*) FROM memories WHERE is_compressed=0").fetchone()[0]
    vault_n = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE source_file LIKE 'vault:%' AND is_compressed=0"
    ).fetchone()[0]
    # v6 memory_layer 分布（JSON 字段，缺省算 unknown）
    layers = {"semantic": 0, "episodic": 0, "procedural": 0, "unknown": 0}
    try:
        for ml, n in conn.execute(
            """
            SELECT COALESCE(json_extract(metadata,'$.memory_layer'),'unknown'), COUNT(*)
            FROM memories WHERE is_compressed=0
            GROUP BY 1
            """
        ).fetchall():
            key = (ml or "unknown").lower()
            if key not in layers:
                layers[key] = 0
            layers[key] = int(n or 0)
    except Exception:
        pass
    extract_n = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE is_compressed=0 AND source_file='session-extract'"
    ).fetchone()[0]
    abstract_n = conn.execute(
        """
        SELECT COUNT(*) FROM memories
        WHERE is_compressed=0 AND json_extract(metadata,'$.abstract') IS NOT NULL
          AND length(json_extract(metadata,'$.abstract')) > 0
        """
    ).fetchone()[0]
    payload = {
        "version": _NEBULA_VER,
        "total_active": total,
        "vault_chunks": vault_n,
        "trust": trust,
        "links": links,
        "memory_layers": layers,
        "session_extract_count": extract_n,
        "abstract_count": abstract_n,
        "maturity": _maturity_score(trust, links, total),
        "cached": False,
    }
    _HEALTH_CACHE["ts"] = now
    _HEALTH_CACHE["payload"] = payload
    return dict(payload)


def _maturity_score(trust: Dict, links: Dict, total: int) -> Dict[str, Any]:
    canon = int(trust.get("canon", 0) or 0)
    synth = int(trust.get("synthesis", 0) or 0)
    link_n = sum(int(v or 0) for v in (links or {}).values())
    score = 35  # base
    if canon >= 100:
        score += 12
    if link_n >= 2000:
        score += 12
    if total and synth < total * 0.55:
        score += 10
    elif total and synth < total * 0.7:
        score += 5
    if int(trust.get("superseded", 0) or 0) >= 5:
        score += 4
    score += 10  # v5 stack present

    # 真牛逼：回归通过率硬挂钩
    reg = {}
    try:
        from pathlib import Path as _P
        import json as _json
        p = _P("/home/huhu/.local/state/nebula-regression/latest.json")
        if p.exists():
            reg = _json.loads(p.read_text(encoding="utf-8"))
            pr = float(reg.get("pass_rate") or 0)
            score += int(pr * 25)  # 全过 +25
    except Exception:
        reg = {}

    # secrets 解锁（缓存，避免 health 每次 1s+ CLI）
    try:
        st = _cached_bw_status()
        if st.get("ok") or st.get("status") == "unlocked":
            score += 8
    except Exception:
        pass

    score = min(100, score)
    if score >= 92 and float(reg.get("pass_rate") or 0) >= 0.95:
        level = "true_nb"  # 真牛逼
    elif score >= 85:
        level = "ultimate"
    elif score >= 70:
        level = "advanced"
    else:
        level = "intermediate"
    return {
        "score": score,
        "level": level,
        "regression_pass_rate": reg.get("pass_rate"),
        "regression_passed": reg.get("passed"),
        "regression_total": reg.get("total"),
    }



def promote_draft(manager, evidence_query: str = "", session_notes: str = "", max_chars: int = 1200) -> dict:
    """3.7 中间层：根据星枢证据起草笔记晋升草稿（不自动写盘）。"""
    from nebula_v4 import reflect_ask
    ask = reflect_ask(manager, query=evidence_query or session_notes[:80] or "任务结论", top_k=5, hops=2, use_graph=True)
    pack = ask.get("pack") or ""
    system = (
        "你是记忆晋升助手。根据证据判断是否应写入虎虎笔记。"
        "只输出 JSON，不要 markdown 围栏。"
        '格式: {"should_write_vault":true/false,"path":"04-项目笔记/xxx.md","title":"...",'
        '"markdown":"中文正文","category":"lesson|project|fact","importance":0.0-1.0,'
        '"memory_summary":"给星枢的一句话","reason":"理由"}。'
        "密钥/密码不要写进 markdown。权威配置冲突时以 GATEWAY_LOCK 为准。"
        "未验证的推断 should_write_vault=false。"
    )
    user = f"线索/会话:\n{(session_notes or '')[:1500]}\n\n星枢证据:\n{pack[:2000]}"
    text = _llm_chat(system, user, max_tokens=800, temperature=0.2)
    import json as _json
    data = None
    if text:
        try:
            # strip fence
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", text)
            data = _json.loads(m.group(0) if m else text)
        except Exception:
            data = {"raw": text, "should_write_vault": False, "reason": "JSON解析失败"}
    if not data:
        data = {"should_write_vault": False, "reason": "LLM不可用", "pack": pack[:500]}
    data["evidence_pack"] = pack
    data["engine"] = "promote_draft_v5"
    # 安全：草稿再扫密
    try:
        from nebula_secrets import scan_secrets
        md = data.get("markdown") or ""
        if scan_secrets(md):
            data["should_write_vault"] = False
            data["secret_blocked"] = True
            data["reason"] = (data.get("reason") or "") + "; 草稿含疑似密钥已拦截"
            data["markdown"] = "[REDACTED]"
    except Exception:
        pass
    return data
