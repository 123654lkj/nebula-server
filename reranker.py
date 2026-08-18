#!/usr/bin/env python3
"""星枢精排：百炼 qwen3-rerank（cross-encoder）。

MiniMax-M3 不再占排序位。本地 bge 权重不在盘上，且 MemoryMax=512M 塞不下。
失败时保持粗排原序。pin / canon / superseded 仍在精排之后一票否决。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("nebula.reranker")

# 兼容旧名：不再加载本地 cross-encoder
CACHE_DIR = os.path.expanduser("~/.cache/modelscope/hub/models/BAAI/bge-reranker-v2-m3")

DEFAULT_RERANK_MODEL = "qwen3-rerank"
DEFAULT_RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
DEFAULT_INSTRUCT = (
    "Given a web search query, retrieve relevant passages that answer the query."
)


def _rerank_enabled() -> bool:
    return os.environ.get("NEBULA_RERANK", "1").lower() not in ("0", "false", "off", "no")


def _rerank_model() -> str:
    return os.environ.get("NEBULA_RERANK_MODEL") or DEFAULT_RERANK_MODEL


def _rerank_url() -> str:
    return os.environ.get("NEBULA_RERANK_URL") or DEFAULT_RERANK_URL


def _api_key() -> str:
    return (
        os.environ.get("BAILIAN_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or ""
    )


class BailianReranker:
    """百炼文本排序。接口兼容旧 CrossEncoderReranker.rerank()，返回 0~1 相关分。"""

    _instance = None

    def __init__(self, device: str = "bailian", dtype: str = "n/a"):
        self._device = device
        self._dtype = dtype
        self._cache: Dict[Tuple[str, str], Tuple[List[float], float]] = {}
        self._cache_ttl = 600.0
        self._http = None

    @classmethod
    def get_instance(cls, device: str = "auto", dtype: str = "auto") -> "BailianReranker":
        if cls._instance is None:
            cls._instance = cls(device="bailian", dtype="n/a")
        return cls._instance

    @property
    def is_loaded(self) -> bool:
        return bool(_api_key())

    @property
    def backend(self) -> str:
        return "qwen3-rerank"

    @property
    def model(self) -> str:
        return _rerank_model()

    def _cache_get(self, qh: str, ch: str) -> Optional[List[float]]:
        hit = self._cache.get((qh, ch))
        if not hit:
            return None
        scores, ts = hit
        if time.time() - ts > self._cache_ttl:
            self._cache.pop((qh, ch), None)
            return None
        return list(scores)

    def _cache_put(self, qh: str, ch: str, scores: List[float]) -> None:
        if len(self._cache) > 128:
            self._cache.clear()
        self._cache[(qh, ch)] = (list(scores), time.time())

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            _rerank_url(),
            data=body,
            headers={
                "Authorization": "Bearer " + _api_key(),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def rerank(self, query: str, candidates: List[str], top_k: Optional[int] = None, batch_size: int = 32) -> List[float]:
        n = len(candidates)
        if n == 0:
            return []
        if n == 1:
            return [1.0]
        if not _rerank_enabled():
            return [float(n - i) / n for i in range(n)]
        if not _api_key():
            logger.warning("qwen3-rerank 无 BAILIAN_API_KEY，保持原序")
            return [float(n - i) / n for i in range(n)]

        docs = []
        for c in candidates:
            snippet = (c or "").strip()
            if len(snippet) > 1800:
                snippet = snippet[:1800]
            docs.append(snippet)

        qh = hashlib.sha256((query or "").encode("utf-8", "ignore")).hexdigest()[:16]
        ch = hashlib.sha256("\n".join(docs).encode("utf-8", "ignore")).hexdigest()[:16]
        cached = self._cache_get(qh, ch)
        if cached is not None:
            return cached[:top_k] if top_k else cached

        payload = {
            "model": _rerank_model(),
            "input": {"query": query or "", "documents": docs},
            "parameters": {"top_n": n, "return_documents": False},
        }
        # qwen3-rerank 支持 instruct；gte-rerank-v2 忽略未知字段通常也可
        if _rerank_model().startswith("qwen3"):
            payload["parameters"]["instruct"] = os.environ.get("NEBULA_RERANK_INSTRUCT") or DEFAULT_INSTRUCT

        try:
            data = self._post(payload)
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "replace")[:180]
            logger.warning("qwen3-rerank HTTP %s: %s", e.code, err)
            return [float(n - i) / n for i in range(n)][:top_k] if top_k else [float(n - i) / n for i in range(n)]
        except Exception as e:
            logger.warning("qwen3-rerank 调用失败: %s", e)
            return [float(n - i) / n for i in range(n)][:top_k] if top_k else [float(n - i) / n for i in range(n)]

        rows = data.get("results")
        if rows is None:
            rows = (data.get("output") or {}).get("results")
        scores = [0.0] * n
        if not rows:
            logger.warning("qwen3-rerank 空结果 keys=%s", list(data)[:8])
            return [float(n - i) / n for i in range(n)][:top_k] if top_k else [float(n - i) / n for i in range(n)]
        for row in rows:
            try:
                idx = int(row.get("index"))
                if 0 <= idx < n:
                    scores[idx] = float(row.get("relevance_score") or 0.0)
            except (TypeError, ValueError):
                continue
        self._cache_put(qh, ch, scores)
        return scores[:top_k] if top_k else scores

    def unload(self) -> None:
        self._cache.clear()

    def get_memory_usage(self) -> Dict[str, float]:
        return {"allocated_mb": 0.0, "reserved_mb": 0.0}


# 旧 import 兼容
LLMReranker = BailianReranker
CrossEncoderReranker = BailianReranker


def get_reranker(device: str = "auto", dtype: str = "auto") -> Optional[BailianReranker]:
    if not _rerank_enabled():
        return None
    try:
        return BailianReranker.get_instance()
    except Exception as e:
        logger.warning("Reranker 不可用: %s", e)
        return None


def _query_window(text: str, query: str, max_chars: int = 260) -> str:
    """截与 query 最重合的窗口。不 import vector_memory，避免循环依赖。"""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    tokens = re.findall(r"[\w\u4e00-\u9fff]{2,}", (query or "").lower())
    stop = {"的", "了", "和", "是", "在", "与", "或", "什么", "怎么", "如何", "一下", "这个", "那个", "一个"}
    tokens = [t for t in tokens if t not in stop]
    lower = text.lower()
    best_i, best = 0, -1
    step = max(20, max_chars // 5)
    for i in range(0, max(1, len(text) - max_chars + 1), step):
        chunk = lower[i:i + max_chars]
        sc = 0
        for tok in tokens:
            if tok not in chunk:
                continue
            sc += chunk.count(tok) * (4 if len(tok) >= 6 else 1)
        if sc > best:
            best, best_i = sc, i
    return text[best_i:best_i + max_chars]


def _query_proper_nouns(query: str) -> List[str]:
    """只抽英文/数字标识（Vaultwarden、GATEWAY_LOCK、torrent-panel）。"""
    toks = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", query or "")
    out = []
    for tok in toks:
        if tok.lower() in {"the", "and", "for", "how", "what", "where"}:
            continue
        if tok not in out:
            out.append(tok)
    return out[:8]


def _candidate_card(r: dict, query: str = "") -> str:
    """给排序模型看：路径 + 问句窗口。不展示 trust，避免追光环。"""
    src = r.get("source_file") or r.get("src") or ""
    src_s = src.rsplit("/", 1)[-1] if src else "-"
    raw = r.get("content") or r.get("abstract") or r.get("content_preview") or ""
    body = _query_window(raw, query, 260)
    stub = "nonce=" in raw[:220] or raw.lstrip().startswith("# 独立主题-")
    blob = (body + " " + raw[:400]).lower()
    missing = [t for t in _query_proper_nouns(query) if t.lower() not in blob]
    tags = []
    if stub:
        tags.append("[STUB]")
    if missing:
        tags.append("[缺:" + ",".join(missing[:3]) + "]")
    tag = ((" ".join(tags) + " ") if tags else "")
    return f"{tag}file={src_s} | {body}"


def _slot_bonus(query: str, text: str, source: str = "") -> float:
    """问什么补什么：路径/禁令/模型名 + 来源文件名对得上。"""
    q = query or ""
    t = text or ""
    src = (source or "").rsplit("/", 1)[-1].lower()
    b = 0.0
    if any(w in q for w in ("目录", "路径", "哪改", "哪个目录")):
        if re.search(r"/(?:home|opt|etc|usr)/\S+", t):
            b += 3.0
        if re.search(r"systemd|\.service", t, re.I) and not re.search(r"/(?:home|opt)/", t):
            b -= 2.0
    if any(w in q for w in ("能不能", "能否", "改代理", "冻结")):
        if "禁止" in t or "默认禁止" in t:
            b += 2.5
        elif "GATEWAY_LOCK" in t and "禁止" not in t:
            b -= 1.0
        if "gateway_lock.md" in src:
            b += 3.0
    if "模型" in q or "重排" in q:
        if re.search(r"qwen3-rerank|MiniMax-M3|bge-reranker|改为 LLM|LLM 重排", t):
            b += 2.5
    for tok in _query_proper_nouns(q):
        tl = tok.lower()
        if len(tl) >= 4 and tl in src:
            b += 3.0
            break
    return b


def seed_already_answers(query: str, r: dict) -> bool:
    """粗排第一名已经填上槽位 → 不必再打排序 API。"""
    if not r:
        return False
    text = r.get("content") or r.get("abstract") or ""
    src = r.get("source_file") or r.get("src") or ""
    if not text:
        return False
    blob = (text + " " + src).lower()
    nouns = _query_proper_nouns(query)
    if nouns and any(n.lower() not in blob for n in nouns):
        return False
    q = query or ""
    if any(w in q for w in ("目录", "路径", "哪个目录")):
        return bool(re.search(r"/(?:home|opt|etc)/", text))
    if any(w in q for w in ("能不能", "能否", "改代理", "冻结")):
        return ("禁止" in text or "默认禁止" in text)
    if "模型" in q or "重排" in q:
        return bool(re.search(r"qwen3-rerank|MiniMax-M3|bge-reranker|改为 LLM", text))
    if any(w.lower() in q.lower() for w in ("Vaultwarden", "密码本")):
        tl = text.lower()
        return "vaultwarden" in tl and any(x in tl for x in ("bw-", "vault.lan", "密码本", "密码库"))
    return False


def apply_rerank(query: str, results: List[dict], candidates: int = 8) -> List[dict]:
    """按相关性重排。trust 不加分；仅 superseded 沉底。同分保持原序。"""
    if not results or len(results) <= 1 or not _rerank_enabled():
        return results
    rr = get_reranker()
    if not rr:
        return results
    actual_k = min(max(int(candidates or 8), 2), len(results))
    head = results[:actual_k]
    texts = [_candidate_card(r, query) for r in head]
    try:
        scores = rr.rerank(query, texts)
        max_vec = max((float(r.get("score") or 0) for r in head), default=0.0) or 1.0
        for i, (r, s) in enumerate(zip(head, scores)):
            trust = r.get("trust") or ""
            penalty = 100.0 if trust == "superseded" else 0.0
            blob = r.get("content") or r.get("abstract") or ""
            if "nonce=" in blob[:220] or blob.lstrip().startswith("# 独立主题-"):
                penalty += 5.0
            miss = [tkn for tkn in _query_proper_nouns(query) if tkn.lower() not in blob.lower()]
            if miss:
                penalty += 4.0
            slot = _slot_bonus(query, blob, r.get("source_file") or r.get("src") or "")
            vec = 2.5 * (float(r.get("score") or 0) / max_vec)
            # CE 分 0~1，放大到与 slot/vec 同量级
            r["rerank_raw"] = round(float(s), 4)
            r["rerank_score"] = round(float(s) * 10.0 - penalty + slot + vec, 4)
        results[:actual_k] = sorted(
            head, key=lambda x: x.get("rerank_score") or 0, reverse=True,
        )
    except Exception as e:
        logger.warning("apply_rerank 失败: %s", e)
    return results
