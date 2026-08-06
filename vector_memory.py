#!/usr/bin/env python3
"""
星枢 (Nebula) — 向量记忆核心引擎 v3 (进化版)
================================================
优化清单:
  [e1] 清理死代码: 删除 QueryCache 类
  [e2] 嵌入矩阵增量更新: vstack 追加 + 懒删除 + 定期 compact
  [e4] 数据库连接池化: thread-local 连接复用
  [e6] MemoryCompressor 重写: 从 SQLite 按日期分组压缩
  [e7] 搜索并行化: 向量搜索和 BM25 用 ThreadPoolExecutor 并行
  [e9] 统一错误处理和日志: 全局 logging 替代 print
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

# ─── 统一日志 [e9] ──────────────────────────────────────────────────────
logger = logging.getLogger("nebula")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# ─── 常量 ────────────────────────────────────────────────────────────────
DEFAULT_CATEGORY = "general"
EMBEDDING_DIM = 2048  # qwen2.5-vl-embedding

CATEGORY_KEYWORDS = {
    "code":       ["def ", "class ", "import ", "function ", "return ", "async ", "git ", "commit", "变量", "函数", "代码"],
    "network":    ["ip ", "tcp", "udp", "proxy", "代理", "透明代理", "mihomo", "iptables", "nftables", "dns", "vpn", "hy2", "hysteria", "xray", "sing-box"],
    "hardware":   ["树莓派", "gpu", "cpu", "内存", "usb", "硬件", "挂灯", "台灯", "旋钮", "ulanzi"],
    "homelab":    ["docker", "容器", "compose", "home assistant", "ha ", "nas", "portainer"],
    "todo":       ["todo", "计划", "要做", "下一步", "规划"],
    "note":       ["学了", "学习", "笔记", "心得", "记录", "总结"],
    "config":     ["配置", "设置", "config", ".json", ".yaml", ".toml", "环境变量"],
    "debug":      ["bug", "错误", "报错", "fix", "修复", "排查", "问题"],
    "ai":         ["模型", "embedding", "向量", "llm", "gpt", "minimax", "豆包", "千问", "智谱", "rerank"],
}


def get_category_keywords() -> Dict[str, List[str]]:
    return CATEGORY_KEYWORDS


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embedding_to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def blob_to_embedding(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def _rule_based_classify(text: str) -> str:
    text_lower = text.lower()
    keywords = get_category_keywords()
    best_score = 0
    best_cat = DEFAULT_CATEGORY
    for cat, kw_list in keywords.items():
        score = sum(1 for kw in kw_list if kw.lower() in text_lower)
        if score > best_score:
            best_score = score
            best_cat = cat
    return best_cat


# ─── 数据库初始化 ────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    """初始化数据库表结构"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_file TEXT,
        section_title TEXT,
        content_hash TEXT UNIQUE,
        content TEXT NOT NULL,
        content_preview TEXT,
        embedding BLOB,
        embedding_dim INTEGER,
        embedding_model TEXT,
        category TEXT DEFAULT 'general',
        importance REAL DEFAULT 0.5,
        created_at REAL,
        updated_at REAL,
        is_compressed INTEGER DEFAULT 0,
        level INTEGER,
        parent_id INTEGER,
        line_start INTEGER,
        line_end INTEGER,
        node_type TEXT,
        node_name TEXT,
        project_name TEXT,
        location TEXT,
        metadata TEXT DEFAULT '{}'
    );

    CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT,
        usage_count INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS memory_tags (
        memory_id INTEGER,
        tag_id INTEGER,
        PRIMARY KEY (memory_id, tag_id),
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id) REFERENCES tags(id)
    );

    CREATE TABLE IF NOT EXISTS compression_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_str TEXT,
        summary TEXT,
        source_count INTEGER,
        compressed_at REAL
    );

    CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash);
    CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
    CREATE INDEX IF NOT EXISTS idx_memories_compressed ON memories(is_compressed);
    CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
    CREATE INDEX IF NOT EXISTS idx_memories_node_type ON memories(node_type);
    CREATE INDEX IF NOT EXISTS idx_memories_node_name ON memories(node_name);
    CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_name);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_name_nocase ON tags(name COLLATE NOCASE);

    -- 标签计数由数据库维护，避免不同写入路径各自漏更新。
    CREATE TRIGGER IF NOT EXISTS memory_tags_count_after_insert
    AFTER INSERT ON memory_tags
    BEGIN
        UPDATE tags SET usage_count = usage_count + 1 WHERE id = NEW.tag_id;
    END;

    CREATE TRIGGER IF NOT EXISTS memory_tags_count_after_delete
    AFTER DELETE ON memory_tags
    BEGIN
        UPDATE tags SET usage_count = MAX(usage_count - 1, 0) WHERE id = OLD.tag_id;
    END;

    CREATE TRIGGER IF NOT EXISTS memory_tags_prune_after_delete
    AFTER DELETE ON memory_tags
    WHEN NOT EXISTS (SELECT 1 FROM memory_tags WHERE tag_id = OLD.tag_id)
    BEGIN
        DELETE FROM tags WHERE id = OLD.tag_id;
    END;

    -- 部分旧连接未启用 foreign_keys，显式触发器仍能清理关系。
    CREATE TRIGGER IF NOT EXISTS memories_tags_cleanup_after_delete
    AFTER DELETE ON memories
    BEGIN
        DELETE FROM memory_tags WHERE memory_id = OLD.id;
    END;

    CREATE TRIGGER IF NOT EXISTS tags_links_cleanup_after_delete
    AFTER DELETE ON tags
    BEGIN
        DELETE FROM memory_tags WHERE tag_id = OLD.id;
    END;
    """)

    # FTS5 全文搜索（用于 BM25）
    try:
        conn.execute("SELECT 1 FROM memories_fts LIMIT 1")
    except sqlite3.OperationalError:
        conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content, content_preview, category,
            content='memories', content_rowid='id'
        );
        -- 触发器：同步 INSERT
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, content_preview, category)
            VALUES (new.id, new.content, new.content_preview, new.category);
        END;
        -- 触发器：同步 DELETE
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, content_preview, category)
            VALUES ('delete', old.id, old.content, old.content_preview, old.category);
        END;
        -- 触发器：同步 UPDATE
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, content_preview, category)
            VALUES ('delete', old.id, old.content, old.content_preview, old.category);
            INSERT INTO memories_fts(rowid, content, content_preview, category)
            VALUES (new.id, new.content, new.content_preview, new.category);
        END;
        -- 初始填充
        INSERT OR IGNORE INTO memories_fts(rowid, content, content_preview, category)
            SELECT id, content, content_preview, category FROM memories;
        """)

    conn.commit()


# ─── Embedder ────────────────────────────────────────────────────────────


# ─── Token 友好结果打包（Agent 默认路径）──────────────────────────────

_VAULT_HEADER_RE = re.compile(
    r"^\[笔记\][^\n]*\n?",
    re.MULTILINE,
)
_NOISE_KEYS = (
    "explain", "bm25_score", "vector_score", "time_decay",
    "authority_mult", "created_at", "level", "node_type", "node_name",
    "project_name", "location", "metadata",
)


def extract_snippet(content: str, query: str, max_chars: int = 320) -> str:
    """从正文抽与 query 最相关的窗口，去掉 vault 模板头/frontmatter，省 token。"""
    if not content:
        return ""
    text = _VAULT_HEADER_RE.sub("", content).strip()
    # 去 YAML frontmatter
    if text.startswith("---"):
        end_fm = text.find("\n---", 3)
        if end_fm > 0:
            text = text[end_fm + 4:].strip()
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s == "---":
            continue
        if s.startswith("source=vault:") or s.startswith("回读命令:"):
            continue
        if s.startswith("chunk=") and len(s) < 40:
            continue
        if s.startswith("title=") and len(s) < 80:
            continue
        # 弱相关「相关文档/参见」段降权：后面窗口打分处理
        lines.append(ln)
    text = "\n".join(lines).strip()
    if not text:
        text = content.strip()

    if len(text) <= max_chars:
        return text

    q = (query or "").lower()
    tokens = re.findall(r"[\w\u4e00-\u9fff]{2,}", q)
    stop = {
        "的", "了", "和", "是", "在", "与", "或", "什么", "怎么", "如何",
        "一下", "这个", "那个", "一个", "我们", "可以", "需要",
    }
    tokens = [t for t in tokens if t not in stop]
    # 整词短语加权
    phrases = []
    if len(q) >= 4:
        phrases.append(q[:40])

    best_i = 0
    best_score = -10**9
    window = max_chars
    step = max(24, max_chars // 6)
    lower = text.lower()
    limit = max(1, len(text) - window + 1)
    for i in range(0, limit, step):
        chunk = lower[i:i + window]
        sc = 0.0
        for t in tokens:
            c = chunk.count(t)
            if c:
                sc += (3.0 + min(c, 5)) * (1.2 if len(t) >= 4 else 1.0)
        for ph in phrases:
            if ph and ph in chunk:
                sc += 6
        # 权威/行动信号
        for kw, w in (
            ("禁止", 4), ("冻结", 4), ("现行", 3), ("权威", 3),
            ("必读", 3), ("结论", 2), ("默认", 1.5), ("必须", 2),
            ("不要", 2), ("只能", 2), ("端口", 1),
        ):
            if kw in chunk:
                sc += w
        # 相关文档/目录类段落降权
        if "相关文档" in chunk or "参见" in chunk or "目录" in chunk:
            sc -= 5
        if chunk.strip().startswith("|") and chunk.count("|") > 8:
            sc -= 1  # 大表格略降
        # 轻微偏好靠前
        sc -= i / max(len(text), 1) * 1.5
        if sc > best_score:
            best_score = sc
            best_i = i
    snippet = text[best_i:best_i + window].strip()
    if best_i > 0:
        snippet = "…" + snippet
    if best_i + window < len(text):
        snippet = snippet + "…"
    return snippet


def _query_hit_score(content: str, query: str) -> float:
    """同源多 chunk 时选 query 命中更密的。"""
    if not content:
        return 0.0
    lower = content.lower()
    tokens = re.findall(r"[\w\u4e00-\u9fff]{2,}", (query or "").lower())
    sc = 0.0
    for t in tokens:
        sc += lower.count(t) * (1.5 if len(t) >= 4 else 1.0)
    for kw in ("现行", "权威", "禁止", "冻结", "必读"):
        if kw in content:
            sc += 2
    if "相关文档" in content[:200]:
        sc -= 2
    return sc



def pack_results(
    results: List[Dict],
    query: str = "",
    top_k: int = 5,
    max_chars: int = 320,
    per_source: int = 1,
    drop_superseded: bool = True,
    drop_hearsay_if_canon: bool = True,
    compact: bool = True,
    min_score: float = 0.0,
) -> List[Dict]:
    """把原始检索结果压成 Agent 友好、省 token 的列表。"""
    if not results:
        return []

    ordered = sorted(results, key=lambda x: float(x.get("score") or 0), reverse=True)

    if drop_superseded:
        ordered = [r for r in ordered if r.get("trust") != "superseded"]

    if min_score and min_score > 0:
        ordered = [r for r in ordered if float(r.get("score") or 0) >= min_score]

    has_canon = any(r.get("trust") == "canon" for r in ordered[: max(top_k * 2, 6)])
    if drop_hearsay_if_canon and has_canon:
        filtered = []
        synth_kept = 0
        for r in ordered:
            t = r.get("trust")
            if t == "hearsay":
                continue
            if t == "synthesis":
                if synth_kept >= 1:
                    continue
                synth_kept += 1
            filtered.append(r)
        if filtered:
            ordered = filtered

    # 同源：先在每个 source 内挑 query 命中最密的一条（分数接近时）
    best_by_src: Dict[str, Dict] = {}
    best_hit: Dict[str, float] = {}
    for r in ordered:
        src = r.get("source_file") or f"id:{r.get('id')}"
        key = src
        if src in ("manual", "grok", "api", "hermes", None, ""):
            key = f"id:{r.get('id')}"
        hit = _query_hit_score(r.get("content") or "", query)
        score = float(r.get("score") or 0)
        if key not in best_by_src:
            best_by_src[key] = r
            best_hit[key] = hit
            continue
        prev = best_by_src[key]
        prev_score = float(prev.get("score") or 0)
        # 分数接近（5% 内）时用 hit 决胜；否则保留高分
        if score > prev_score * 1.05:
            best_by_src[key] = r
            best_hit[key] = hit
        elif score >= prev_score * 0.95 and hit > best_hit.get(key, -1):
            best_by_src[key] = r
            best_hit[key] = hit

    # 再按原序输出（保持全局分数序），同源只留 best
    seen_src = set()
    diversified = []
    for r in ordered:
        src = r.get("source_file") or f"id:{r.get('id')}"
        key = src
        if src in ("manual", "grok", "api", "hermes", None, ""):
            key = f"id:{r.get('id')}"
        chosen = best_by_src.get(key)
        if not chosen or chosen.get("id") != r.get("id"):
            continue
        if key in seen_src:
            continue
        # per_source>1 时允许追加同源次优——此处简化：只 1 条最优
        if per_source <= 1:
            seen_src.add(key)
            diversified.append(chosen)
        else:
            # 宽松：按序收，计数
            n = sum(1 for x in diversified if (x.get("source_file") or f"id:{x.get('id')}") == src)
            if n >= per_source:
                continue
            diversified.append(r)
        if len(diversified) >= top_k * 2:
            break

    packed = []
    for r in diversified[:top_k]:
        item = dict(r)
        full = item.get("content") or ""
        snippet = extract_snippet(full, query, max_chars=max_chars)
        if compact:
            item["content"] = snippet
            item["content_chars"] = len(snippet)
            item["full_chars"] = len(full)
            for k in _NOISE_KEYS:
                item.pop(k, None)
            sf = item.get("source_file") or ""
            if sf.startswith("vault:"):
                item["src"] = sf[len("vault:"):]
            elif sf:
                item["src"] = sf if len(sf) <= 60 else ("…" + sf[-57:])
            else:
                item["src"] = None
        else:
            item["content"] = snippet if len(full) > max_chars * 2 else full
            item["content_chars"] = len(item["content"])
            item["full_chars"] = len(full)
        packed.append(item)
    return packed


def results_token_stats(results: List[Dict]) -> Dict[str, int]:
    """粗估 token（中英混合 ~chars/2.2）。"""
    total_chars = 0
    for r in results:
        total_chars += len(r.get("content") or "")
        total_chars += len(str(r.get("source_file") or r.get("src") or ""))
        total_chars += len(str(r.get("readback") or ""))
        total_chars += 24
    est_tokens = max(1, int(total_chars / 2.2)) if results else 0
    return {"chars": total_chars, "est_tokens": est_tokens, "count": len(results)}


def format_ask_pack(query: str, results: List[Dict], max_total_chars: int = 1800) -> str:
    """压成一段可直接塞进上下文的短文。"""
    lines = [f"【星枢】q={query.strip()[:80]} | hits={len(results)}"]
    used = len(lines[0])
    for i, r in enumerate(results, 1):
        trust = r.get("trust") or "?"
        try:
            score_s = f"{float(r.get('score') or 0):.3f}"
        except Exception:
            score_s = str(r.get("score"))
        src = r.get("src") or r.get("source_file") or ""
        head = f"{i}. [{trust}|{score_s}] #{r.get('id')} {src}"
        body = (r.get("content") or "").replace("\n", " ").strip()
        rb = r.get("readback")
        block = head + "\n   " + body
        if rb:
            block += f"\n   → {rb}"
        if used + len(block) + 1 > max_total_chars:
            lines.append(f"…截断，剩余 {len(results) - i + 1} 条未展开")
            break
        lines.append(block)
        used += len(block) + 1
    return "\n".join(lines)




def expand_followup_queries(query: str, results: List[Dict], max_followups: int = 2) -> List[str]:
    try:
        from nebula_v4 import improve_followups
        return improve_followups(query, results, max_followups=max_followups)
    except Exception:
        pass
    return _expand_followup_queries_legacy(query, results, max_followups)


def _expand_followup_queries_legacy(query: str, results: List[Dict], max_followups: int = 2) -> List[str]:
    """从首轮结果抽 follow-up 查询词（无 LLM，省 token/延迟）。"""
    if not results:
        return []
    q_tokens = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", (query or "").lower()))
    stop = {
        "的", "了", "和", "是", "在", "与", "或", "什么", "怎么", "如何", "一下",
        "这个", "那个", "可以", "需要", "我们", "一个", "title", "chunk", "source",
        "path", "host", "read", "notes", "vault",
    }
    scores: Dict[str, float] = {}
    for r in results[:6]:
        text = (r.get("content") or "") + " " + str(r.get("src") or r.get("source_file") or "")
        src = str(r.get("src") or r.get("source_file") or "")
        base = src.split("/")[-1].replace(".md", "") if src else ""
        if base and len(base) >= 3:
            scores[base] = scores.get(base, 0) + 4.0
        for t in re.findall(r"[\w\u4e00-\u9fff]{2,12}", text):
            tl = t.lower()
            if tl in stop or tl in q_tokens or len(tl) < 2:
                continue
            if t.isdigit():
                continue
            w = 1.0
            if any(k in t for k in ("禁止", "冻结", "权威", "故障", "端口", "架构", "skill", "agent")):
                w = 2.5
            if len(t) >= 4:
                w *= 1.3
            scores[t] = scores.get(t, 0) + w
        if r.get("trust") == "canon":
            for k in list(scores.keys())[-8:]:
                scores[k] = scores.get(k, 0) + 0.5

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    followups = []
    for term, _ in ranked:
        if term.lower() in (query or "").lower():
            continue
        fq = f"{query} {term}".strip()
        if fq not in followups and fq != query:
            followups.append(fq)
        if len(followups) >= max_followups:
            break
    return followups


def multi_hop_search(
    manager,
    query: str,
    top_k: int = 4,
    hops: int = 2,
    use_hybrid: bool = True,
    category: str = None,
    max_chars: int = 260,
) -> Dict:
    """轻量多跳：首轮检索 → 抽词 → 再检索 → 合并 pack。"""
    hops = max(1, min(int(hops or 1), 3))
    all_raw: Dict[int, Dict] = {}
    followups_used: List[str] = []
    for hop in range(hops):
        if hop == 0:
            qlist = [query]
        else:
            qlist = followups_used
            if not qlist:
                break
        for q in qlist:
            raw = manager.search(
                query=q,
                top_k=max(top_k * 3, 10),
                category=category,
                use_hybrid=use_hybrid,
                rerank=False,
                enable_time_decay=True,
            )
            for r in raw or []:
                mid = r.get("id")
                if mid is None:
                    continue
                sc = float(r.get("score") or 0) * (1.0 if hop == 0 else 0.85)
                rr = dict(r)
                rr["score"] = sc
                rr["hop"] = hop
                if mid not in all_raw or sc > float(all_raw[mid].get("score") or 0):
                    all_raw[mid] = rr
        if hop == 0:
            seed = sorted(all_raw.values(), key=lambda x: float(x.get("score") or 0), reverse=True)[: max(top_k, 4)]
            followups_used = expand_followup_queries(query, seed, max_followups=2)

    merged = sorted(all_raw.values(), key=lambda x: float(x.get("score") or 0), reverse=True)
    packed = pack_results(
        merged,
        query=query,
        top_k=top_k,
        max_chars=max_chars,
        per_source=1,
        drop_superseded=True,
        drop_hearsay_if_canon=True,
        compact=True,
    )
    return {
        "results": packed,
        "followups": followups_used,
        "hops": hops,
        "raw_candidates": len(all_raw),
    }




class Embedder:
    """qwen2.5-vl-embedding via 百炼 dashscope HTTP 多模态接口

    支持 文本/图片/视频 多模态向量, 2048 维 (dimension参数)。
    使用 urllib 直接调 HTTP, 无需额外 SDK 依赖。
    """

    MULTIMODAL_EMBED_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"

    def __init__(self, provider: str = "qwen3vl"):
        self.provider = provider
        self.model = "qwen2.5-vl-embedding"
        self.dim = EMBEDDING_DIM
        self._api_key = None
        self._cache = None  # 可选 EmbeddingCache（server 注入，ask/多跳共用）
        self._http = None   # [perf2] requests.Session 复用 TLS

    def set_cache(self, cache) -> None:
        """注入查询向量 LRU 缓存，所有 embed 路径自动命中。"""
        self._cache = cache

    def _get_http(self):
        if self._http is None:
            try:
                import requests
                s = requests.Session()
                s.headers.update({"Content-Type": "application/json"})
                adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                self._http = s
            except Exception:
                self._http = False
        return self._http

    def _get_api_key(self) -> str:
        if self._api_key is None:
            self._api_key = os.environ.get("BAILIAN_API_KEY", "")
            if not self._api_key:
                raise RuntimeError("BAILIAN_API_KEY 未设置")
        return self._api_key

    def _call_api(self, contents: list) -> list:
        """调用 dashscope 多模态 embedding API, 返回向量列表"""
        import json as _json

        api_key = self._get_api_key()
        payload_obj = {
            "model": self.model,
            "input": {"contents": contents},
            "parameters": {"dimension": self.dim},
        }
        body = None
        http = self._get_http()
        if http and http is not False:
            try:
                resp = http.post(
                    self.MULTIMODAL_EMBED_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload_obj,
                    timeout=12,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"Embedding API HTTP {resp.status_code}: {resp.text[:300]}")
                body = resp.json()
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning("requests embed fail, fallback urllib: %s", e)
                body = None
        if body is None:
            import urllib.request
            import urllib.error
            payload = _json.dumps(payload_obj).encode("utf-8")
            req = urllib.request.Request(
                self.MULTIMODAL_EMBED_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=12) as resp:
                    body = _json.loads(resp.read())
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Embedding API HTTP {e.code}: {err[:300]}")
            except Exception as e:
                raise RuntimeError(f"Embedding API 请求失败: {e}")

        if body.get("status_code") and body["status_code"] != 200:
            raise RuntimeError(f"Embedding API 错误: {body}")

        embeddings = body.get("output", {}).get("embeddings", [])
        results = []
        for item in embeddings:
            vec = item.get("vector") or item.get("embedding")
            if vec:
                results.append(vec)
        if not results:
            raise RuntimeError(f"Embedding API 返回空向量: {str(body)[:200]}")
        return results

    def embed(self, text: str) -> np.ndarray:
        # [perf] 全局查询向量缓存 — /ask multi-hop / hybrid 全路径生效
        if self._cache is not None:
            hit = self._cache.get(text)
            if hit is not None:
                return np.asarray(hit, dtype=np.float32)
        vecs = self._call_api([{"text": text}])
        vec = np.array(vecs[0], dtype=np.float32)
        if self._cache is not None:
            try:
                self._cache.put(text, vec)
            except Exception:
                pass
        return vec

    def embed_batch(self, texts: List[str], batch_size: int = 1) -> List[np.ndarray]:
        """批量 embedding — 逐条调用 (qwen2.5-vl-embedding 每次只接受1个text)"""
        results = []
        for t in texts:
            results.append(self.embed(t))
        return results

    def warmup(self) -> bool:
        try:
            self.embed("warmup")
            logger.info(f"Embedder 预热成功: {self.model} dim={self.dim}")
            return True
        except Exception as e:
            logger.warning(f"Embedder 预热失败: {e}")
            return False


# ─── 连接池 [e4] ─────────────────────────────────────────────────────────

class ConnectionPool:
    """Thread-local SQLite 连接池，每个线程复用自己的连接"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._all_conns: List[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def get(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # [perf2] WAL + 大 cache/mmap
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-65536")
            conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = conn
            with self._lock:
                self._all_conns.append(conn)
        return conn

    def close_all(self):
        with self._lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()


# ─── MemoryManager (核心) ────────────────────────────────────────────────

class MemoryManager:
    """向量记忆管理器 — 进化版 v3"""

    def __init__(self, db_path: str = None, embedder: Embedder = None):
        self.db_path = db_path or os.path.join(os.path.dirname(__file__), 'data', 'memory_vectors.db')
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # [e4] 连接池
        self._pool = ConnectionPool(self.db_path)
        self.conn = self._pool.get()  # 主线程连接
        init_db(self.conn)
        try:
            from nebula_v4 import migrate_schema
            migrate_schema(self.conn)
        except Exception as _e:
            logger.warning(f"v4 migrate skip: {_e}")

        self.embedder = embedder or Embedder()
        self._db_lock = threading.Lock()

        # [e2] 嵌入矩阵增量缓存
        self._emb_matrix: Optional[np.ndarray] = None  # shape (N, dim) 已行归一化
        self._emb_ids: Optional[np.ndarray] = None      # shape (N,) — memory IDs
        self._emb_lock = threading.Lock()
        self._emb_dirty_deletes: set = set()  # 懒删除标记
        self._emb_compact_threshold = 50  # 累计删除 50 条后触发 compact
        self._emb_row_normalized = False  # [perf] 加载时预归一，search 免重复 norm

        # 搜索线程池 [e7]
        self._search_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nebula-search")

        # Reranker（延迟加载）
        self._reranker = None
        self._reranker_loaded = False

        # 旧版 _cache 兼容（给 import_jsonl 等用）
        self._cache = None

        logger.info(f"星枢 v4 初始化完成 | DB: {self.db_path}")

    # ─── 嵌入矩阵管理 [e2] ─────────────────────────────────────────────

    def _load_emb_matrix(self) -> Tuple[np.ndarray, np.ndarray]:
        """加载嵌入矩阵（首次全量加载，之后增量更新）"""
        with self._emb_lock:
            if self._emb_matrix is not None:
                return self._emb_matrix, self._emb_ids

            cur = self.conn.execute(
                "SELECT id, embedding, embedding_dim FROM memories WHERE is_compressed = 0 AND embedding IS NOT NULL"
            )
            rows = cur.fetchall()
            if not rows:
                self._emb_matrix = np.empty((0, EMBEDDING_DIM), dtype=np.float32)
                self._emb_ids = np.empty(0, dtype=np.int64)
                return self._emb_matrix, self._emb_ids

            vecs = []
            ids = []
            for mid, blob, dim in rows:
                if blob and dim:
                    try:
                        vec = blob_to_embedding(blob, dim)
                        vecs.append(vec)
                        ids.append(mid)
                    except Exception:
                        continue

            if vecs:
                mat = np.vstack(vecs).astype(np.float32, copy=False)
                # [perf] 行 L2 归一化一次，search 直接 mat @ q
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                mat = mat / np.maximum(norms, 1e-10)
                self._emb_matrix = mat
                self._emb_ids = np.array(ids, dtype=np.int64)
                self._emb_row_normalized = True
            else:
                self._emb_matrix = np.empty((0, EMBEDDING_DIM), dtype=np.float32)
                self._emb_ids = np.empty(0, dtype=np.int64)
                self._emb_row_normalized = True

            self._emb_dirty_deletes.clear()
            logger.info(f"嵌入矩阵加载: {len(ids)} 条记忆 (row-normalized)")
            return self._emb_matrix, self._emb_ids

    def _append_to_emb_matrix(self, memory_id: int, vec: np.ndarray):
        """[e2] 增量追加一条嵌入到矩阵（不全量重建）"""
        with self._emb_lock:
            if self._emb_matrix is None:
                # 矩阵还没加载过，不需要追加，等下次 search 时会全量加载
                return
            v = np.asarray(vec, dtype=np.float32).reshape(1, -1)
            if self._emb_row_normalized:
                n = float(np.linalg.norm(v))
                if n > 1e-10:
                    v = v / n
            self._emb_matrix = np.vstack([self._emb_matrix, v])
            self._emb_ids = np.append(self._emb_ids, memory_id)

    def _mark_emb_deleted(self, memory_id: int):
        """[e2] 懒删除：标记某 ID 需要从矩阵中排除"""
        with self._emb_lock:
            self._emb_dirty_deletes.add(memory_id)
            # 累计删除达到阈值时触发 compact
            if len(self._emb_dirty_deletes) >= self._emb_compact_threshold:
                self._compact_emb_matrix_unlocked()

    def _compact_emb_matrix_unlocked(self):
        """[e2] 压缩嵌入矩阵：移除懒删除的行（需持有 _emb_lock）"""
        if self._emb_matrix is None or not self._emb_dirty_deletes:
            return
        mask = np.isin(self._emb_ids, list(self._emb_dirty_deletes), invert=True)
        self._emb_matrix = self._emb_matrix[mask]
        self._emb_ids = self._emb_ids[mask]
        removed = len(self._emb_dirty_deletes)
        self._emb_dirty_deletes.clear()
        logger.info(f"嵌入矩阵 compact: 移除 {removed} 条，剩余 {len(self._emb_ids)} 条")

    def _invalidate_emb_cache(self):
        """完全重置嵌入矩阵（用于 recategorize 等批量操作）"""
        with self._emb_lock:
            self._emb_matrix = None
            self._emb_ids = None
            self._emb_dirty_deletes.clear()
            self._emb_row_normalized = False

    # ─── Reranker ──────────────────────────────────────────────────────

    def _get_reranker(self):
        if self._reranker_loaded:
            return self._reranker
        try:
            from reranker import get_reranker
            self._reranker = get_reranker()
        except Exception as e:
            logger.warning(f"Reranker 不可用: {e}")
            self._reranker = None
        self._reranker_loaded = True
        return self._reranker

    # ─── 向量搜索 ─────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 10, category: str = None,
               rerank: bool = False, rerank_candidates: int = 5,
               use_hybrid: bool = False, explain: bool = False,
               enable_time_decay: bool = True, time_decay_lambda: float = 0.05,
               use_cache: bool = True, precomputed_vector: np.ndarray = None,
               date_from: str = None, date_to: str = None,
               project_name: str = None, location: str = None,
               similarity_threshold: float = 0.0,
               # 层级搜索参数
               level: int = None, node_type: str = None, node_name: str = None,
               source_file: str = None, line_range: Tuple[int, int] = None,
               ) -> List[Dict]:
        """
        向量搜索 — 核心方法
        支持: 分类过滤、时间衰减、层级搜索、hybrid 混合搜索
        """
        if not query and not source_file:
            return []

        # hybrid 模式 → 走 _hybrid_search（透传 precomputed 避免重复 embed）
        if use_hybrid:
            return self._hybrid_search(
                query, top_k=top_k,
                enable_time_decay=enable_time_decay,
                time_decay_lambda=time_decay_lambda,
                explain=explain,
                precomputed_vector=precomputed_vector,
            )

        # 1. 获取查询向量
        if precomputed_vector is not None:
            q_vec = precomputed_vector
        else:
            q_vec = self.embedder.embed(query)
        q_vec = q_vec / (np.linalg.norm(q_vec) + 1e-10)

        # 2. 加载嵌入矩阵 [e2]
        emb_matrix, emb_ids = self._load_emb_matrix()
        if emb_matrix.shape[0] == 0:
            return []

        # 3. 排除懒删除的 ID
        with self._emb_lock:
            if self._emb_dirty_deletes:
                mask = np.isin(emb_ids, list(self._emb_dirty_deletes), invert=True)
                work_matrix = emb_matrix[mask]
                work_ids = emb_ids[mask]
            else:
                work_matrix = emb_matrix
                work_ids = emb_ids

        if work_matrix.shape[0] == 0:
            return []

        # 4. 余弦相似度（矩阵运算）— 矩阵已预归一时跳过行 norm
        if getattr(self, '_emb_row_normalized', False):
            scores = work_matrix @ q_vec  # (N,)
        else:
            norms = np.linalg.norm(work_matrix, axis=1, keepdims=True)
            norms = np.maximum(norms, 1e-10)
            scores = (work_matrix / norms) @ q_vec  # (N,)

        # 5. 取 top candidates
        fetch_k = min(top_k * 3, len(scores))
        top_indices = np.argpartition(scores, -fetch_k)[-fetch_k:]
        top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

        # 6. 获取候选 ID 和分数
        candidate_ids = work_ids[top_indices].tolist()
        candidate_scores = scores[top_indices].tolist()

        # 7. 从数据库获取完整记录
        if not candidate_ids:
            return []

        placeholders = ",".join("?" * len(candidate_ids))
        sql = f"""
            SELECT id, content, category, importance, created_at, source_file,
                   level, node_type, node_name, project_name, location, metadata
            FROM memories
            WHERE id IN ({placeholders}) AND is_compressed = 0
        """
        params = list(candidate_ids)

        # 附加过滤条件
        filters = []
        if category:
            filters.append("category = ?")
            params.append(category)
        if level is not None:
            filters.append("level = ?")
            params.append(level)
        if node_type:
            filters.append("node_type = ?")
            params.append(node_type)
        if node_name:
            filters.append("node_name = ?")
            params.append(node_name)
        if source_file:
            filters.append("source_file = ?")
            params.append(source_file)
        if project_name:
            filters.append("project_name = ?")
            params.append(project_name)
        if location:
            filters.append("location = ?")
            params.append(location)
        if date_from:
            try:
                ts_from = time.mktime(time.strptime(date_from, "%Y-%m-%d"))
                filters.append("created_at >= ?")
                params.append(ts_from)
            except ValueError:
                pass
        if date_to:
            try:
                ts_to = time.mktime(time.strptime(date_to, "%Y-%m-%d")) + 86400
                filters.append("created_at < ?")
                params.append(ts_to)
            except ValueError:
                pass

        if filters:
            sql += " AND " + " AND ".join(filters)

        cur = self.conn.execute(sql, params)
        db_rows = {row[0]: row for row in cur.fetchall()}

        # 8. 组装结果
        now = time.time()
        results = []
        for mid, raw_score in zip(candidate_ids, candidate_scores):
            if mid not in db_rows:
                continue
            row = db_rows[mid]
            _, content, cat, importance, created_at, src_file, lvl, n_type, n_name, proj, loc, meta_json = row

            score = float(raw_score)
            if score < similarity_threshold:
                continue

            # 时间衰减
            time_decay = 1.0
            if enable_time_decay and created_at:
                days = (now - created_at) / 86400.0
                time_decay = math.exp(-time_decay_lambda * days)
                score *= time_decay

            result = {
                "id": mid,
                "content": content,
                "score": round(score, 6),
                "category": cat,
                "importance": importance,
                "created_at": created_at,
                "source_file": src_file,
            }
            if lvl is not None:
                result["level"] = lvl
            if n_type:
                result["node_type"] = n_type
            if n_name:
                result["node_name"] = n_name
            if proj:
                result["project_name"] = proj
            if loc:
                result["location"] = loc

            if explain:
                result["explain"] = {
                    "vector_score": float(raw_score),
                    "time_decay": time_decay if enable_time_decay else None,
                    "final_score": round(score, 6),
                }

            results.append(result)

        results.sort(key=lambda x: x["score"], reverse=True)

        # 8.5 权威加权（vault/importance/SUPERSEDED）— 纯向量路径也生效
        results = self._apply_authority_boost(results, explain=explain)

        # 9. Rerank (可选)
        if rerank and len(results) > 1:
            reranker = self._get_reranker()
            if reranker:
                actual_k = min(rerank_candidates, len(results))
                candidates_text = [r["content"] for r in results[:actual_k]]
                try:
                    rerank_scores = reranker.rerank(query, candidates_text)
                    for r, rs in zip(results[:actual_k], rerank_scores):
                        r["rerank_score"] = rs
                    results[:actual_k] = sorted(
                        results[:actual_k],
                        key=lambda x: x.get("rerank_score", 0),
                        reverse=True,
                    )
                except Exception as e:
                    logger.warning(f"Rerank 失败: {e}")

        return results[:top_k]

    # ─── BM25 搜索 ────────────────────────────────────────────────────


    def _apply_authority_boost(self, results: List[Dict], explain: bool = False) -> List[Dict]:
        """权威加权：importance + vault/gateway 提升 + SUPERSEDED/过时降权。
        来自知识库落地：The_Forest 可信度分层 + vault 为 L1 权威。
        """
        if not results:
            return results

        ids = [r["id"] for r in results if r.get("id") is not None]
        if not ids:
            return results

        # [perf2] 结果已带齐字段时跳过二次 SELECT
        need_ids = []
        meta = {}
        for r in results:
            mid = r.get("id")
            if mid is None:
                continue
            if (
                r.get("content") is not None
                and r.get("importance") is not None
                and (r.get("source_file") is not None or r.get("src") is not None)
                and r.get("trust") is not None
            ):
                meta[mid] = (
                    mid,
                    r.get("importance"),
                    r.get("source_file") or r.get("src") or "",
                    r.get("category"),
                    r.get("content") or "",
                    None,
                    r.get("trust"),
                )
            else:
                need_ids.append(mid)
        if need_ids:
            placeholders = ",".join("?" * len(need_ids))
            try:
                try:
                    cur = self.conn.execute(
                        f"""SELECT id, importance, source_file, category, content, metadata, trust
                            FROM memories WHERE id IN ({placeholders})""",
                        need_ids,
                    )
                except Exception:
                    cur = self.conn.execute(
                        f"""SELECT id, importance, source_file, category, content, metadata
                            FROM memories WHERE id IN ({placeholders})""",
                        need_ids,
                    )
                for row in cur.fetchall():
                    meta[row[0]] = row
            except Exception as e:
                logger.warning(f"authority boost meta 读取失败: {e}")
                if not meta:
                    return results

        demote_markers = (
            "【已过时", "SUPERSEDED", "已废弃", "不再使用", "仅历史",
            "假信息", "误导", "deprecated",
        )
        for r in results:
            mid = r.get("id")
            row = meta.get(mid)
            if not row:
                continue
            if len(row) >= 7:
                _, importance, source_file, category, content, metadata_json, db_trust = row
            else:
                _, importance, source_file, category, content, metadata_json = row
                db_trust = None
            imp = importance if importance is not None else 0.5
            src = source_file or ""
            content = content or ""
            mult = 1.0
            trust = db_trust if db_trust else "synthesis"

            # importance
            if imp >= 0.95:
                mult *= 1.45
            elif imp >= 0.9:
                mult *= 1.35
            elif imp >= 0.7:
                mult *= 1.15
            elif imp < 0.35:
                mult *= 0.45
            elif imp < 0.5:
                mult *= 0.85

            # vault / gateway 权威
            if src.startswith("vault:gateway/") or "GATEWAY_LOCK" in src:
                mult *= 1.55
                trust = "canon"
            elif src.startswith("vault:notes/01-") or src.startswith("vault:notes/02-"):
                mult *= 1.4
                trust = "canon"
            elif src.startswith("vault:"):
                mult *= 1.3
                trust = "canon"
            elif "SESSIONS/" in src or src.startswith("sessions/"):
                mult *= 0.75
                trust = "hearsay"
            elif src in ("manual", "grok", "api", "hermes") or not src:
                trust = "source" if imp >= 0.85 else "synthesis"

            # 正文标记过时
            head = content[:400]
            if any(m in head for m in demote_markers):
                mult *= 0.22
                trust = "superseded"
            # 明确现行权威
            if "现行权威" in head or "Agent 必读" in head or "[2026-07-11 现行" in head:
                mult *= 1.25
                if trust != "superseded":
                    trust = "canon"

            # 填充缺失字段
            if "importance" not in r or r.get("importance") is None:
                r["importance"] = imp
            if "source_file" not in r or not r.get("source_file"):
                r["source_file"] = src or None
            if "category" not in r or not r.get("category"):
                r["category"] = category
            # DB trust=superseded 强制降权
            if db_trust == "superseded" and trust != "superseded":
                trust = "superseded"
                mult *= 0.22
            r["trust"] = trust
            r["authority_mult"] = round(mult, 4)

            old = float(r.get("score", 0) or 0)
            r["score"] = round(old * mult, 6)
            if explain or r.get("explain"):
                exp = r.get("explain") or {}
                exp["authority_mult"] = mult
                exp["trust"] = trust
                exp["importance"] = imp
                exp["final_score"] = r["score"]
                r["explain"] = exp

            # vault 回读提示
            if src.startswith("vault:"):
                path = src[len("vault:"):]
                _vault_root = os.environ.get("NEBULA_VAULT_ROOT", "")
                if path.startswith("gateway/") and _vault_root:
                    r["readback"] = f"read {os.path.join(_vault_root, 'gateway', path[len('gateway/'):])}"
                elif path.startswith("notes/") and _vault_root:
                    r["readback"] = f'read "{os.path.join(_vault_root, path)}"'
                elif path.startswith("infra/") and _vault_root:
                    r["readback"] = f'read "{os.path.join(_vault_root, "infra", path[len("infra/"):])}"'
                else:
                    r["readback"] = f"vault source: {src}"

        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results


    def _bm25_search(self, query: str, top_k: int = 10,
                     enable_time_decay: bool = True, time_decay_lambda: float = 0.05,
                     explain: bool = False) -> List[Dict]:
        """FTS5 BM25 全文搜索"""
        if not query:
            return []

        # 分词 + FTS5 查询
        tokens = re.findall(r'[\w\u4e00-\u9fff]+', query.lower())
        if not tokens:
            return []
        fts_query = " OR ".join(tokens)

        try:
            cur = self.conn.execute(
                """SELECT m.id, m.content, bm25(memories_fts) as score, m.created_at
                   FROM memories_fts
                   JOIN memories m ON m.id = memories_fts.rowid
                   WHERE memories_fts MATCH ? AND m.is_compressed = 0
                   ORDER BY bm25(memories_fts)
                   LIMIT ?""",
                (fts_query, top_k * 2),
            )
            rows = cur.fetchall()
            if not rows:
                return []

            max_abs = max(abs(r[2]) for r in rows) or 1.0
            now = time.time()
            results = []
            for mid, content, raw_score, created_at in rows:
                score = abs(raw_score) / max_abs

                time_decay = 1.0
                if enable_time_decay and created_at:
                    days = (now - created_at) / 86400.0
                    time_decay = math.exp(-time_decay_lambda * days)
                    score *= time_decay

                results.append({
                    "id": mid,
                    "content": content,
                    "bm25_score": score,
                    "created_at": created_at,
                    "time_decay": time_decay if enable_time_decay else None,
                    "explain": {
                        "vector_score": None,
                        "bm25_score": score,
                        "rrf_score": None,
                        "time_decay": time_decay if enable_time_decay else None,
                        "final_score": score,
                    } if explain else None,
                })
            return results
        except Exception as e:
            logger.warning(f"BM25 搜索失败: {e}")
            return []

    # ─── Hybrid 搜索 (RRF 融合) [e7] ─────────────────────────────────

    def _hybrid_search(self, query: str, top_k: int = 10,
                       vector_weight: float = 0.7, bm25_weight: float = 0.3,
                       enable_time_decay: bool = True, time_decay_lambda: float = 0.05,
                       explain: bool = False,
                       precomputed_vector: np.ndarray = None) -> List[Dict]:
        """向量 + BM25 融合搜索（RRF）+ 时间衰减 — [e7] 并行化"""
        if not query:
            return []

        # [e7] 并行执行向量搜索和 BM25 搜索
        # use_cache 留给 embedder 层缓存；precomputed 可跳过 API
        future_vec = self._search_pool.submit(
            self.search,
            query=query, top_k=top_k * 2,
            rerank=False, enable_time_decay=False,
            use_cache=True, explain=explain,
            precomputed_vector=precomputed_vector,
        )
        future_bm25 = self._search_pool.submit(
            self._bm25_search,
            query=query, top_k=top_k * 2,
            enable_time_decay=False, explain=explain,
        )

        vector_results = future_vec.result(timeout=30)
        bm25_results = future_bm25.result(timeout=30)

        vector_dict = {r["id"]: r for r in vector_results}
        bm25_dict = {r["id"]: r for r in bm25_results}

        # RRF 融合 (k=60)
        k = 60
        vector_rank = {}
        for rank, mid in enumerate(
            sorted(vector_dict.keys(), key=lambda x: vector_dict[x].get("score", 0), reverse=True), 1
        ):
            vector_rank[mid] = rank

        bm25_rank = {}
        for rank, mid in enumerate(
            sorted(bm25_dict.keys(), key=lambda x: bm25_dict[x].get("bm25_score", 0), reverse=True), 1
        ):
            bm25_rank[mid] = rank

        all_ids = set(vector_rank.keys()) | set(bm25_rank.keys())
        now = time.time()
        fused = []
        for mid in all_ids:
            v_rrf = 1.0 / (k + vector_rank.get(mid, float('inf')))
            b_rrf = 1.0 / (k + bm25_rank.get(mid, float('inf')))
            rrf_score = v_rrf + b_rrf

            created_at = vector_dict.get(mid, {}).get("created_at") or bm25_dict.get(mid, {}).get("created_at")

            time_decay = 1.0
            if enable_time_decay and created_at:
                days = (now - created_at) / 86400.0
                time_decay = math.exp(-time_decay_lambda * days)

            original_rrf = rrf_score
            fused_score = rrf_score * time_decay

            content = vector_dict.get(mid, {}).get("content", bm25_dict.get(mid, {}).get("content", ""))
            fused.append({
                "id": mid,
                "content": content,
                "score": fused_score,
                "vector_score": vector_dict.get(mid, {}).get("score", 0.0),
                "bm25_score": bm25_dict.get(mid, {}).get("bm25_score", 0.0),
                "created_at": created_at,
                "time_decay": time_decay if enable_time_decay else None,
                "explain": {
                    "vector_score": vector_dict.get(mid, {}).get("score", 0.0),
                    "bm25_score": bm25_dict.get(mid, {}).get("bm25_score", 0.0),
                    "rrf_score": original_rrf,
                    "time_decay": time_decay if enable_time_decay else None,
                    "final_score": fused_score,
                } if explain else None,
            })

        # 权威加权（importance + vault + SUPERSEDED + 字段补全）
        fused = self._apply_authority_boost(fused, explain=explain)
        return fused[:top_k]

    # ─── 搜索 + LLM 改写 ─────────────────────────────────────────────

    def search_with_rewrite(self, query: str, top_k: int = 10,
                            n_rewrites: int = 2, **search_kwargs) -> Dict:
        """LLM 改写查询 → 多路搜索 → 融合去重"""
        rewriter = QueryRewriter()
        rewrites = rewriter.rewrite(query, n=n_rewrites)

        all_results = {}
        rewrite_counts = {}

        for rw in rewrites:
            results = self.search(query=rw, top_k=top_k, **search_kwargs)
            rewrite_counts[rw] = len(results)
            for r in results:
                mid = r["id"]
                if mid not in all_results or r["score"] > all_results[mid]["score"]:
                    all_results[mid] = r

        merged = sorted(all_results.values(), key=lambda x: x["score"], reverse=True)[:top_k]

        return {
            "results": merged,
            "rewrites": rewrites,
            "rewrite_counts": rewrite_counts,
            "original_count": rewrite_counts.get(query, 0),
        }

    # ─── 层级专用搜索 ────────────────────────────────────────────────

    def search_function(self, func_name: str, top_k: int = 5) -> List[Dict]:
        """精确搜索函数名"""
        return self.search(query=func_name, level=1, node_type='function', node_name=func_name, top_k=top_k)

    def search_by_location(self, filepath: str, line: int, top_k: int = 3) -> List[Dict]:
        """按文件+行号定位代码块"""
        return self.search(query="", level=None, source_file=filepath, line_range=(line, line), top_k=top_k)

    def search_class(self, class_name: str, top_k: int = 5) -> List[Dict]:
        """精确搜索类名"""
        return self.search(query=class_name, level=1, node_type='class', node_name=class_name, top_k=top_k)

    # ─── 记忆 CRUD ──────────────────────────────────────────────────

    def add(self, content: str, source: str = "manual", category: str = None,
            tags: List[str] = None, importance: float = 0.5,
            level: int = None, parent_id: int = None,
            line_start: int = None, line_end: int = None,
            node_type: str = None, node_name: str = None,
            project_name: str = None, location: str = None,
            metadata: Dict[str, Any] = None,
            semantic_dedup: bool = True,
            similarity_threshold: float = 0.95) -> Dict[str, Any]:
        """添加记忆（支持精确去重 + 语义去重）"""
        # 1. 精确去重（content_hash）
        h = content_hash(content)
        cur = self.conn.execute(
            "SELECT id FROM memories WHERE content_hash = ? AND is_compressed = 0", (h,)
        )
        exact_match = cur.fetchone()
        if exact_match:
            return {"id": exact_match[0], "is_duplicate": True, "duplicate_of": exact_match[0], "similarity_score": 1.0}

        # 1.5 密钥明文拦截 → 禁止入向量
        try:
            from nebula_secrets import scan_secrets, redact_secrets
            hits = scan_secrets(content or "")
            if hits:
                # 自动脱敏再拒写？策略：直接拒绝，要求改走密码本
                raise ValueError(
                    "SECRET_REJECTED: 检测到疑似密钥明文(" + ",".join(hits) +
                    ")。请 POST /secrets/store 写入 Vaultwarden，星枢只存 vaultwarden 指针。"
                )
        except ValueError:
            raise
        except Exception as _se:
            logger.warning(f"secret scan skip: {_se}")

        # 2. 语义去重
        similarity_score = 0.0
        if semantic_dedup and content and content.strip():
            similar = self.search(query=content, top_k=1, category=category, use_cache=False)
            if similar and similar[0].get("score", 0) >= similarity_threshold:
                dup_id = similar[0]["id"]
                return {"id": dup_id, "is_duplicate": True, "duplicate_of": dup_id, "similarity_score": similar[0]["score"]}
            elif similar:
                similarity_score = similar[0].get("score", 0)

        # 3. 写入新记忆
        preview = content[:200]
        now = time.time()
        if category is None:
            category = _rule_based_classify(content)
        vec = self.embedder.embed(content)
        blob = embedding_to_blob(vec)
        dim = int(len(vec))

        try:
            from nebula_v4 import infer_trust
            _trust = infer_trust(content, source or "", float(importance or 0.5),
                                 (metadata or {}).get("trust") if isinstance(metadata, dict) else None)
        except Exception:
            _trust = "synthesis"
        if not isinstance(metadata, dict):
            metadata = {}
        else:
            metadata = dict(metadata)
        metadata["trust"] = _trust
        metadata_json = json.dumps(metadata)

        with self._db_lock:
            try:
                cur = self.conn.execute(
                    """INSERT INTO memories (
                        source_file, section_title, content_hash, content,
                        content_preview, embedding, embedding_dim, embedding_model,
                        category, importance, created_at, updated_at,
                        level, parent_id, line_start, line_end, node_type, node_name,
                        project_name, location, metadata, trust
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source, h, content, preview, blob, dim, self.embedder.model,
                     category, importance, now, now,
                     level, parent_id, line_start, line_end, node_type, node_name,
                     project_name, location, metadata_json, _trust),
                )
            except Exception:
                cur = self.conn.execute(
                    """INSERT INTO memories (
                        source_file, section_title, content_hash, content,
                        content_preview, embedding, embedding_dim, embedding_model,
                        category, importance, created_at, updated_at,
                        level, parent_id, line_start, line_end, node_type, node_name,
                        project_name, location, metadata
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (source, h, content, preview, blob, dim, self.embedder.model,
                     category, importance, now, now,
                     level, parent_id, line_start, line_end, node_type, node_name,
                     project_name, location, metadata_json),
                )
            mid = cur.lastrowid
            if tags:
                for tag_name in tags:
                    tag_id = ensure_tag(self.conn, tag_name, category)
                    self.conn.execute(
                        "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id) VALUES (?, ?)",
                        (mid, tag_id),
                    )
            self.conn.commit()

        # [e2] 增量追加到嵌入矩阵（不全量重建！）
        self._append_to_emb_matrix(mid, vec)

        return {"id": mid, "is_duplicate": False, "duplicate_of": None, "similarity_score": similarity_score, "trust": _trust}

    def delete(self, memory_id: int) -> bool:
        """删除记忆"""
        with self._db_lock:
            cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            if cur.rowcount == 0:
                return False
            self.conn.commit()
        # [e2] 懒删除（不全量重建！）
        self._mark_emb_deleted(memory_id)
        return True

    # ─── 分类管理 ────────────────────────────────────────────────────

    def list_categories(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT category, COUNT(*) as count FROM memories GROUP BY category ORDER BY count DESC"
        )
        return [{"category": row[0], "count": row[1]} for row in cur.fetchall()]

    def cache_stats(self) -> Dict:
        return {"emb_matrix_size": self._emb_matrix.shape[0] if self._emb_matrix is not None else 0,
                "dirty_deletes": len(self._emb_dirty_deletes)}

    def recategorize(self, memory_id: int, method: str = "rule") -> bool:
        cur = self.conn.execute(
            "SELECT id, content FROM memories WHERE id = ? AND is_compressed = 0", (memory_id,)
        )
        row = cur.fetchone()
        if not row:
            return False
        mid, content = row
        new_cat = _rule_based_classify(content) if method == "rule" else DEFAULT_CATEGORY
        with self._db_lock:
            self.conn.execute(
                "UPDATE memories SET category = ?, updated_at = ? WHERE id = ?",
                (new_cat, time.time(), memory_id),
            )
            self.conn.commit()
        return True

    def batch_recategorize(self, category_filter: str = None, method: str = "rule",
                           limit: int = 100, dry_run: bool = False) -> Dict[str, Any]:
        if category_filter:
            cur = self.conn.execute(
                "SELECT id, content, category FROM memories WHERE category = ? AND is_compressed = 0 ORDER BY id LIMIT ?",
                (category_filter, limit),
            )
        else:
            cur = self.conn.execute(
                "SELECT id, content, category FROM memories WHERE is_compressed = 0 ORDER BY id LIMIT ?",
                (limit,),
            )

        rows = cur.fetchall()
        stats = {"total": len(rows), "changed": 0, "unchanged": 0, "errors": 0}

        for row in rows:
            mid, content, old_cat = row
            try:
                new_cat = _rule_based_classify(content) if method == "rule" else DEFAULT_CATEGORY
                if new_cat != old_cat:
                    if not dry_run:
                        self.conn.execute(
                            "UPDATE memories SET category = ?, updated_at = ? WHERE id = ?",
                            (new_cat, time.time(), mid),
                        )
                    stats["changed"] += 1
                else:
                    stats["unchanged"] += 1
            except Exception as e:
                logger.warning(f"重分类 ID={mid} 失败: {e}")
                stats["errors"] += 1

        if not dry_run:
            with self._db_lock:
                self.conn.commit()

        return stats

    # ─── Import/Export JSONL ──────────────────────────────────────────

    def import_jsonl(self, filepath: str, replace: bool = False) -> Dict[str, int]:
        added, skipped, errors = 0, 0, 0
        filepath = Path(filepath)
        if not filepath.exists():
            raise FileNotFoundError(f"JSONL 文件不存在: {filepath}")

        with open(filepath, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    content = data.get("content", "")
                    if not content:
                        errors += 1
                        continue

                    h = content_hash(content)
                    cur = self.conn.execute(
                        "SELECT id FROM memories WHERE content_hash = ? AND is_compressed = 0", (h,)
                    )
                    if cur.fetchone():
                        if replace:
                            self.conn.execute("DELETE FROM memories WHERE content_hash = ?", (h,))
                        else:
                            skipped += 1
                            continue

                    category = data.get("category", _rule_based_classify(content))
                    metadata_json = json.dumps(data.get("metadata", {}))

                    vec = None
                    if "embedding" in data and "embedding_dim" in data:
                        try:
                            if isinstance(data["embedding"], list):
                                vec = np.array(data["embedding"], dtype=np.float32)
                            elif isinstance(data["embedding"], str):
                                import base64
                                vec = blob_to_embedding(base64.b64decode(data["embedding"]), data["embedding_dim"])
                        except Exception:
                            pass
                    if vec is None:
                        vec = self.embedder.embed(content)

                    blob = embedding_to_blob(vec)
                    dim = int(len(vec))
                    now = time.time()
                    preview = content[:200]

                    self.conn.execute(
                        """INSERT INTO memories (
                            source_file, section_title, content_hash, content,
                            content_preview, embedding, embedding_dim, embedding_model,
                            category, importance, created_at, updated_at,
                            level, parent_id, line_start, line_end, node_type, node_name,
                            project_name, location, metadata
                        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (data.get("source_file", "jsonl_import"), h, content, preview, blob, dim,
                         self.embedder.model, category, data.get("importance", 0.5),
                         data.get("created_at", now), now,
                         data.get("level"), data.get("parent_id"),
                         data.get("line_start"), data.get("line_end"),
                         data.get("node_type"), data.get("node_name"),
                         data.get("project_name"), data.get("location"), metadata_json),
                    )
                    added += 1
                except json.JSONDecodeError as e:
                    logger.warning(f"JSONL 第 {line_num} 行解析失败: {e}")
                    errors += 1
                except Exception as e:
                    logger.warning(f"JSONL 第 {line_num} 行导入失败: {e}")
                    errors += 1

        with self._db_lock:
            self.conn.commit()
        # 导入后重建嵌入矩阵
        self._invalidate_emb_cache()
        return {"added": added, "skipped": skipped, "errors": errors}

    def export_jsonl(self, filepath: str) -> int:
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        cur = self.conn.execute(
            """SELECT id, source_file, content_hash, content, content_preview,
                      category, importance, level, parent_id, line_start, line_end,
                      node_type, node_name, project_name, location, metadata,
                      embedding, embedding_dim, embedding_model, created_at, updated_at
               FROM memories WHERE is_compressed = 0 ORDER BY id"""
        )
        rows = cur.fetchall()

        with open(filepath, "w", encoding="utf-8") as f:
            for row in rows:
                (mid, source_file, chash, content, preview,
                 category, importance, level, parent_id, line_start, line_end,
                 node_type, node_name, project_name, location, metadata_json,
                 embedding_blob, embedding_dim, embedding_model, created_at, updated_at) = row

                obj = {"id": mid, "source_file": source_file, "content_hash": chash, "content": content}
                if category:
                    obj["category"] = category
                if level is not None:
                    obj["level"] = level
                if parent_id is not None:
                    obj["parent_id"] = parent_id
                if line_start is not None:
                    obj["line_start"] = line_start
                if line_end is not None:
                    obj["line_end"] = line_end
                if node_type:
                    obj["node_type"] = node_type
                if node_name:
                    obj["node_name"] = node_name
                if project_name:
                    obj["project_name"] = project_name
                if location:
                    obj["location"] = location
                if metadata_json:
                    try:
                        obj["metadata"] = json.loads(metadata_json)
                    except json.JSONDecodeError:
                        obj["metadata"] = {}
                if embedding_blob and embedding_dim:
                    import base64
                    obj["embedding"] = base64.b64encode(embedding_blob).decode("utf-8")
                    obj["embedding_dim"] = embedding_dim
                if embedding_model:
                    obj["embedding_model"] = embedding_model
                if importance != 0.5:
                    obj["importance"] = importance
                if created_at:
                    obj["created_at"] = created_at
                if updated_at:
                    obj["updated_at"] = updated_at
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

        return len(rows)


# ─── 辅助函数 ────────────────────────────────────────────────────────────

def ensure_tag(conn: sqlite3.Connection, name: str, category: str = None) -> int:
    cur = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO tags (name, category, usage_count) VALUES (?, ?, 0)", (name, category))
    return cur.lastrowid


# ─── MemoryIngestor ──────────────────────────────────────────────────────

class MemoryIngestor:
    """文件扫描、分段、去重、批量入库"""

    def __init__(self, manager: MemoryManager):
        self.manager = manager
        self.conn = manager.conn
        self.code_parser = None
        try:
            try:
                from .code_parser import CodeParser
            except ImportError:
                from code_parser import CodeParser
            self.code_parser = CodeParser()
        except Exception as e:
            logger.debug(f"CodeParser 加载失败 (非致命): {e}")

    def ingest_file(self, filepath: str, section_title: str = None,
                    category: str = None, chunk_size: int = 500,
                    overlap: int = 80, importance: float = 0.5) -> int:
        filepath = Path(filepath)
        if not filepath.exists():
            return 0

        added = 0
        seen_hashes = set()

        if filepath.suffix == '.py' and self.code_parser:
            try:
                result = self.code_parser.parse_file(str(filepath))
                if 'error' not in result:
                    fb = result.get('file_block', {})
                    if fb:
                        added += self._ingest_block(fb, filepath, category, importance, seen_hashes)
                    for fb in result.get('function_blocks', []):
                        added += self._ingest_block(fb, filepath, category, importance, seen_hashes)
                    for cb in result.get('class_blocks', []):
                        added += self._ingest_block(cb, filepath, category, importance, seen_hashes)
                    for lb in result.get('line_blocks', []):
                        lb['parent_id'] = None
                        added += self._ingest_block(lb, filepath, category, importance, seen_hashes)
                else:
                    added += self._ingest_text_file(filepath, category, chunk_size, overlap, importance)
            except Exception:
                added += self._ingest_text_file(filepath, category, chunk_size, overlap, importance)
        else:
            added += self._ingest_text_file(filepath, category, chunk_size, overlap, importance)

        return added

    def _ingest_block(self, block: Dict, filepath: Path, category: str, importance: float, seen_hashes: set) -> int:
        content = block.get('content', '')
        if not content or not content.strip():
            return 0
        h = content_hash(content)
        if h in seen_hashes:
            return 0
        seen_hashes.add(h)
        cur = self.conn.execute(
            "SELECT id FROM memories WHERE content_hash = ? AND is_compressed = 0", (h,)
        )
        if cur.fetchone():
            return 0
        try:
            result = self.manager.add(
                content=content, source=str(filepath), category=category,
                importance=importance, level=block.get('level'),
                parent_id=block.get('parent_id'), line_start=block.get('line_start'),
                line_end=block.get('line_end'), node_type=block.get('node_type'),
                node_name=block.get('node_name'),
            )
            return 0 if result.get("is_duplicate") else 1
        except Exception as e:
            logger.warning(f"入库失败: {e}")
            return 0

    def _ingest_text_file(self, filepath: Path, category: str, chunk_size: int, overlap: int, importance: float) -> int:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
        chunks = self._split_text(content, chunk_size, overlap)
        added = 0
        seen_hashes = set()
        for chunk in chunks:
            h = content_hash(chunk)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            cur = self.conn.execute(
                "SELECT id FROM memories WHERE content_hash = ? AND is_compressed = 0", (h,)
            )
            if cur.fetchone():
                continue
            try:
                result = self.manager.add(content=chunk, source=str(filepath), category=category, importance=importance)
                if result.get("id") and not result.get("is_duplicate"):
                    added += 1
            except Exception:
                pass
        return added

    def _split_text(self, text: str, chunk_size: int = 500, overlap: int = 80) -> List[str]:
        sentences = re.split(r'(?<=[。！？.!\n；;])', text)
        chunks = []
        current = []
        current_len = 0
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if current_len + len(sentence) > chunk_size and current:
                chunks.append(''.join(current))
                current = current[-overlap // 10:] if overlap > 0 else []
                current_len = sum(len(s) for s in current)
            current.append(sentence)
            current_len += len(sentence)
        if current:
            chunks.append(''.join(current))
        return chunks


# ─── QueryRewriter ───────────────────────────────────────────────────────

class QueryRewriter:
    """LLM 查询改写器 — 全链路固定百炼 qwen3.7-plus（NEBULA_LLM_MODEL 可覆盖）"""

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or os.environ.get('BAILIAN_API_KEY', '')
        self.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        # 全链路 LLM 统一 qwen3.7-plus
        self.model = model or os.environ.get("NEBULA_LLM_MODEL", "qwen3.7-plus")

    def rewrite(self, query: str, n: int = 3) -> List[str]:
        if not self.api_key:
            return [query]
        try:
            import requests as req
            resp = req.post(
                self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": f"你是一个查询改写助手。用户会给一个搜索查询，你需要生成{n}个不同角度的同义查询，用于向量搜索。每个查询一行，不要编号，不要解释。保持简洁，中文为主。"},
                        {"role": "user", "content": query},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 200,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                text = data.get('choices', [{}])[0].get('message', {}).get('content', '')
                text = re.sub(r'<think[^>]*>.*?</[^>]*think[^>]*>', '', text, flags=re.DOTALL)
                text = re.sub(r'<think[^>]*>.*', '', text, flags=re.DOTALL)
                rewrites = [line.strip() for line in text.strip().split('\n') if line.strip()]
                junk = ['抱歉', '我不', '建议', '没有', '无法', '不清楚', '不知道']
                rewrites = [r for r in rewrites if not any(p in r for p in junk) and r != query]
                rewrites.insert(0, query)
                if len(rewrites) < 2:
                    rewrites = [query]
                return rewrites[:n]
        except Exception as e:
            logger.debug(f"QueryRewrite 失败: {e}")
        return [query]


# ─── MemoryCompressor [e6] 重写 ──────────────────────────────────────────

class MemoryCompressor:
    """
    记忆压缩引擎 v2 — 从 SQLite 按日期分组压缩
    不再依赖文件系统 memory/ 目录，直接从数据库读取记忆并通过 LLM 压缩。
    """

    def __init__(self, manager: MemoryManager):
        self.manager = manager
        self.conn = manager.conn

    def compress_date(self, date_str: str) -> str:
        """压缩某天的记忆为摘要，写入 compression_log"""
        # 检查是否已压缩
        cur = self.conn.execute(
            "SELECT id FROM compression_log WHERE date_str = ?", (date_str,)
        )
        if cur.fetchone():
            logger.info(f"日期 {date_str} 已压缩，跳过")
            return ""

        # 解析日期范围
        try:
            ts_start = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
            ts_end = ts_start + 86400
        except ValueError:
            logger.warning(f"日期格式错误: {date_str}")
            return ""

        # 查询当天记忆
        cur = self.conn.execute(
            """SELECT id, content, category FROM memories
               WHERE created_at >= ? AND created_at < ? AND is_compressed = 0
               ORDER BY created_at""",
            (ts_start, ts_end),
        )
        rows = cur.fetchall()
        if not rows:
            return ""

        # 拼接记忆内容
        combined = ""
        for mid, content, cat in rows:
            combined += f"[{cat}] {content[:500]}\n\n"

        # 调用 LLM 压缩
        summary = self._call_llm(
            f"以下是 {date_str} 的 {len(rows)} 条记忆，请提炼为 3-5 条要点：\n\n{combined[:12000]}"
        )

        if not summary:
            summary = f"📅 {date_str} 有 {len(rows)} 条记忆（LLM 不可用，待压缩）"

        # 记录压缩日志
        self.conn.execute(
            "INSERT INTO compression_log (date_str, summary, source_count, compressed_at) VALUES (?, ?, ?, ?)",
            (date_str, summary, len(rows), time.time()),
        )

        # 标记源记忆为已压缩
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE memories SET is_compressed = 1 WHERE id IN ({placeholders})", ids
        )
        self.conn.commit()

        # 将摘要作为新记忆写入
        self.manager.add(
            content=f"【{date_str} 摘要】{summary}",
            source="compressor",
            category="summary",
            importance=0.7,
        )

        logger.info(f"压缩完成: {date_str}, {len(rows)} 条 → 摘要")
        return summary

    def compress_range(self, days_ago: int = 30) -> Dict[str, str]:
        """压缩 N 天前的所有未压缩记忆"""
        cutoff = time.time() - days_ago * 86400
        cur = self.conn.execute(
            """SELECT DISTINCT date(created_at, 'unixepoch', 'localtime') as d
               FROM memories
               WHERE created_at < ? AND is_compressed = 0
               ORDER BY d""",
            (cutoff,),
        )
        dates = [row[0] for row in cur.fetchall() if row[0]]
        results = {}
        for d in dates:
            summary = self.compress_date(d)
            if summary:
                results[d] = summary
        return results

    def _call_llm(self, prompt: str) -> str:
        # 全链路 LLM 固定 qwen3.7-plus（可用 NEBULA_LLM_MODEL 覆盖，默认 3.7）
        api_key = os.environ.get("BAILIAN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            logger.debug("BAILIAN/DASHSCOPE API_KEY 未设置，跳过 LLM 压缩")
            return ""
        try:
            import httpx
            model = os.environ.get("NEBULA_LLM_MODEL", "qwen3.7-plus")
            resp = httpx.post(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "你是一个记忆压缩助手。将输入的笔记提炼为3-5条简洁要点，中文输出。保留关键信息（项目名、技术决策、教训），去掉流水账。"},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 2000,
                    "temperature": 0.3,
                },
                timeout=12,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"LLM 压缩调用失败: {e}")
            return ""


# ─── 测试入口 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🐰 星枢 (Nebula) v3 — 内置测试")
    print("=" * 60)

    test_db = "/tmp/test_vector_memory.db"
    if os.path.exists(test_db):
        os.remove(test_db)

    manager = MemoryManager(db_path=test_db)
    ingestor = MemoryIngestor(manager)

    print("🔥 预热 Embedder...")
    if manager.embedder.warmup():
        print("✅ Embedder 预热成功")

    test_docs = [
        ("def hello(): return 'world'", "code"),
        ("今天学了向量数据库", "note"),
        ("计划下一步做 stuff", "todo"),
    ]

    for content, cat in test_docs:
        result = manager.add(content=content, category=cat)
        print(f"✅ 添加: id={result['id']}, duplicate={result['is_duplicate']}")

    results = manager.search(query="向量", top_k=5)
    print(f"✅ 搜索: 找到 {len(results)} 条")

    print("=" * 60)
    print("✅ 内置测试完成")
