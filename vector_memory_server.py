#!/usr/bin/env python3
"""
星枢 (Nebula) — REST API + MCP Server v3 (进化版)
===================================================
优化清单:
  [e1] 清理死代码: 删除 6 个死路由 (decay/compress/context)
  [e3] Reranker 启动预加载: 后台线程预热模型
  [e5] EmbeddingCache: 去掉 O(n) 子串扫描, 精确匹配 + query 归一化
  [e8] Warmup 改为后台异步 + 动态热词 (从 DB 统计)
  [e9] 统一错误处理和日志: 全局 errorhandler + logging
  [e10] 暴露 search_function/search_class 为 MCP 工具
  [e11] 健康检查日志降噪: health/stats 不记 access log
"""

import sys
import os
import json
import sqlite3
import threading
import time
import logging
import re
from collections import OrderedDict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, send_from_directory
try:
    from nebula_meta import RELEASE, EMBED_MODEL, RERANK_MODEL, PRODUCT
except Exception:
    RELEASE, EMBED_MODEL, RERANK_MODEL, PRODUCT = "v5.1.0", "qwen2.5-vl-embedding", "qwen3-rerank", "Nebula Memory"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from vector_memory import (
    MemoryManager, MemoryIngestor, MemoryCompressor, Embedder, init_db, QueryRewriter,
    embedding_to_blob, content_hash,
    classify_temporal_intent, parse_as_of,
)

# --- Logging [e9] ---
logger = logging.getLogger("nebula.server")

# --- Config ---
_DEFAULT_DB = os.path.join(os.path.dirname(__file__), 'data', 'memory_vectors.db')
DB_PATH = os.environ.get('NEBULA_DB_PATH') or _DEFAULT_DB
NEBULA_PORT = int(os.environ.get('NEBULA_PORT', '26670'))
NEBULA_BIND = os.environ.get('NEBULA_BIND', '0.0.0.0')
WORKSPACE = os.path.join(os.path.dirname(__file__), '..', '..', '..')

ARK_API_KEY = os.environ.get('ARK_API_KEY', '')
MINIMAX_API_KEY = os.environ.get('MINIMAX_API_KEY', '')
BAILIAN_API_KEY = os.environ.get('BAILIAN_API_KEY', '')
EMBED_PROVIDER = EMBED_MODEL

# 百炼 qwen3-vl-embedding 配置 (2026-07-11 从 doubao 迁移)
BAILIAN_BASE_URL = os.environ.get(
    'BAILIAN_BASE_URL',
    'https://ws-lioxzt2jv93g9u38.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
)
# 密钥只走环境 / EnvironmentFile，禁止写进源码
if BAILIAN_API_KEY:
    os.environ['BAILIAN_API_KEY'] = BAILIAN_API_KEY
os.environ['BAILIAN_BASE_URL'] = BAILIAN_BASE_URL
os.environ.setdefault('NEBULA_LLM_MODEL', 'MiniMax-M3')  # Token Plan M3

if not ARK_API_KEY:
    try:
        with open(os.path.expanduser('~/.openclaw/openclaw.json'), encoding='utf-8') as f:
            _oc = json.load(f)
        ARK_API_KEY = _oc.get('models', {}).get('providers', {}).get('volcengine', {}).get('apiKey', '')
    except Exception:
        pass
if not MINIMAX_API_KEY:
    MINIMAX_API_KEY = os.environ.get('MINIMAX_API_KEY', '')

os.environ['ARK_API_KEY'] = ARK_API_KEY
os.environ['MINIMAX_API_KEY'] = MINIMAX_API_KEY


# --- EmbeddingCache [e5] ---

class EmbeddingCache:
    """LRU + SQLite 持久化：query -> vector，重启后仍可命中。"""

    def __init__(self, maxsize=512, db_path=None):
        self.cache = OrderedDict()
        self.maxsize = maxsize
        self.hits = 0
        self.misses = 0
        self.disk_hits = 0
        self.lock = threading.Lock()
        self.db_path = db_path
        self._disk_ready = False
        if db_path:
            try:
                self._init_disk()
            except Exception as e:
                logger.warning("emb disk cache init fail: %s", e)

    def _init_disk(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS emb_query_cache (
                qkey TEXT PRIMARY KEY,
                vector BLOB NOT NULL,
                dim INTEGER NOT NULL,
                created_at REAL,
                access_count INTEGER DEFAULT 0
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emb_qcache_created ON emb_query_cache(created_at)")
        conn.commit()
        conn.close()
        self._disk_ready = True

    @staticmethod
    def _normalize(text):
        return re.sub(r'\s+', ' ', text.strip()).lower()

    def _disk_get(self, key):
        if not self._disk_ready:
            return None
        try:
            import numpy as _np
            conn = sqlite3.connect(self.db_path, timeout=5)
            row = conn.execute(
                "SELECT vector, dim FROM emb_query_cache WHERE qkey=?", (key,)
            ).fetchone()
            conn.close()
            if not row:
                return None
            blob, dim = row
            vec = _np.frombuffer(blob, dtype=_np.float32).copy()
            if dim and len(vec) != int(dim):
                return None
            return vec
        except Exception:
            return None

    def _disk_put(self, key, vector):
        if not self._disk_ready:
            return
        try:
            import numpy as _np
            vec = _np.asarray(vector, dtype=_np.float32)
            conn = sqlite3.connect(self.db_path, timeout=5)
            conn.execute(
                """INSERT INTO emb_query_cache(qkey, vector, dim, created_at, access_count)
                   VALUES(?,?,?,?,1)
                   ON CONFLICT(qkey) DO UPDATE SET vector=excluded.vector, dim=excluded.dim,
                   created_at=excluded.created_at""",
                (key, vec.tobytes(), int(vec.shape[0]), time.time()),
            )
            n = conn.execute("SELECT COUNT(*) FROM emb_query_cache").fetchone()[0]
            if n > 5000:
                conn.execute(
                    """DELETE FROM emb_query_cache WHERE qkey IN (
                        SELECT qkey FROM emb_query_cache ORDER BY access_count ASC, created_at ASC LIMIT ?
                    )""",
                    (n - 4000,),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug("emb disk put fail: %s", e)

    def get(self, text):
        key = self._normalize(text)
        with self.lock:
            if key in self.cache:
                self.hits += 1
                self.cache.move_to_end(key)
                return self.cache[key]
        disk_vec = self._disk_get(key)
        if disk_vec is not None:
            with self.lock:
                self.disk_hits += 1
                self.hits += 1
                if key in self.cache:
                    self.cache.move_to_end(key)
                else:
                    if len(self.cache) >= self.maxsize:
                        self.cache.popitem(last=False)
                    self.cache[key] = disk_vec
                return self.cache[key]
        with self.lock:
            self.misses += 1
            return None

    def put(self, text, vector):
        key = self._normalize(text)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                self.cache[key] = vector
            else:
                if len(self.cache) >= self.maxsize:
                    self.cache.popitem(last=False)
                self.cache[key] = vector
        self._disk_put(key, vector)

    def stats(self):
        total = self.hits + self.misses
        rate = self.hits / total if total > 0 else 0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "disk_hits": self.disk_hits,
            "size": len(self.cache),
            "hit_rate": f"{rate:.1%}",
            "disk": self._disk_ready,
        }


# 查询向量缓存必须独立文件，禁止跟 memories 抢同一把 SQLite 锁
_EMB_CACHE_DB = os.path.splitext(DB_PATH)[0] + ".embcache.db"
embedding_cache = EmbeddingCache(maxsize=512, db_path=_EMB_CACHE_DB)


class ResultCache:
    """打包后的检索结果 TTL 缓存 — 重复问题秒回，省 embed。"""

    def __init__(self, maxsize=64, ttl_sec=300):
        self.cache = OrderedDict()
        self.maxsize = maxsize
        self.ttl = ttl_sec
        self.hits = 0
        self.misses = 0
        self.lock = threading.Lock()

    @staticmethod
    def make_key(kind: str, payload: dict) -> str:
        items = sorted((k, payload[k]) for k in payload if payload[k] is not None)
        raw = kind + "|" + json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
        return raw

    def get(self, key: str):
        now = time.time()
        with self.lock:
            if key not in self.cache:
                self.misses += 1
                return None
            exp, val = self.cache[key]
            if now > exp:
                del self.cache[key]
                self.misses += 1
                return None
            self.hits += 1
            self.cache.move_to_end(key)
            return val

    def put(self, key: str, val):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            elif len(self.cache) >= self.maxsize:
                self.cache.popitem(last=False)
            self.cache[key] = (time.time() + self.ttl, val)

    def stats(self):
        total = self.hits + self.misses
        rate = self.hits / total if total else 0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "size": len(self.cache),
            "hit_rate": f"{rate:.1%}",
            "ttl_sec": self.ttl,
        }


result_cache = ResultCache(maxsize=512, ttl_sec=600)



# --- Warmup [e8] ---

def warmup_cache(embedder, db_path=None):
    """[e8] Background warmup with dynamic hot words from DB."""
    seed_queries = [
        "session-start", "虎虎现行架构 星枢用法 工作流", "GATEWAY_LOCK",
        "团子", "透明代理", "mihomo", "VPS", "Hysteria", "HY2",
        "Docker", "容器", "服务器", "配置", "星枢", "向量记忆",
        "Home Assistant", "代码", "Python", "模型", "API",
    ]

    if db_path and os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.execute(
                "SELECT DISTINCT category FROM memories WHERE is_compressed = 0 AND category IS NOT NULL ORDER BY category LIMIT 20"
            )
            categories = [row[0] for row in cur.fetchall()]
            for cat in categories:
                cur2 = conn.execute(
                    "SELECT content FROM memories WHERE category = ? AND is_compressed = 0 ORDER BY created_at DESC LIMIT 3",
                    (cat,),
                )
                for row in cur2.fetchall():
                    words = re.findall(r'[\u4e00-\u9fff]+', row[0][:100])
                    seed_queries.extend(words[:2])
            conn.close()
        except Exception as e:
            logger.debug(f"Dynamic hot words failed (non-fatal): {e}")

    seen = set()
    unique = []
    for q in seed_queries:
        if q not in seen and len(q) >= 2:
            seen.add(q)
            unique.append(q)

    try:
        vecs = embedder.embed_batch(unique[:30])
        for q, v in zip(unique, vecs):
            embedding_cache.put(q, v)
        logger.info(f"Cache warmup done: {len(unique[:30])} queries")
    except Exception as e:
        logger.warning(f"Cache warmup failed: {e}")


# --- Flask App ---

app = Flask(__name__)


@app.before_request
def _nebula_gate():
    """企业：可选 Bearer。/health /help 放行。家用未设 token 则不鉴权。"""
    try:
        from nebula_site import API_TOKEN
    except Exception:
        API_TOKEN = ""
    if not API_TOKEN:
        return None
    if request.method == "OPTIONS":
        return None
    path = request.path or ""
    if path in ("/health", "/help", "/v5/health") or path.startswith("/help"):
        return None
    auth = request.headers.get("Authorization") or ""
    got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if got != API_TOKEN:
        return jsonify({"error": "unauthorized", "hint": "Authorization: Bearer <NEBULA_API_TOKEN>"}), 401
    return None


@app.errorhandler(Exception)
def handle_error(e):
    logger.error(f"Uncaught exception: {e}", exc_info=True)
    return jsonify({'error': 'Internal server error', 'message': str(e)}), 500


@app.errorhandler(404)
def handle_404(e):
    return jsonify({'error': 'Not found'}), 404


# [e11] Health log noise filter
class HealthLogFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if '/health' in msg or '/stats' in msg:
            return False
        return True

werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.addFilter(HealthLogFilter())


_init_lock = threading.Lock()
_embedder = None
_memory_manager = None


def get_engine():
    global _embedder, _memory_manager
    if _memory_manager is not None:
        return _memory_manager
    with _init_lock:
        if _memory_manager is None:
            try:
                _embedder = Embedder(provider=EMBED_PROVIDER)
                _embedder.set_cache(embedding_cache)  # [perf] ask/MCP/多跳共享向量缓存
                _memory_manager = MemoryManager(DB_PATH, _embedder)
            except Exception as e:
                logger.error(f"Init failed: {e}")
                raise
    return _memory_manager


# --- Health ---

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'nebula-memory', 'product': PRODUCT, 'version': RELEASE, 'docs': '/help', 'docs_hint': 'GET /help (mini) | /help?level=short|full'})


# --- Search ---


def _parse_bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("1", "true", "yes", "on")


def _search_pack_params(data_or_args, is_get=False):
    """解析 compact/pack 相关参数。默认开启省 token。"""
    if is_get:
        def gb(key, default):
            raw = data_or_args.get(key, None)
            if raw is None:
                return default
            return str(raw).lower() in ("1", "true", "yes", "on")
        compact = gb("compact", True)
        if gb("full", False):
            compact = False
        return {
            "compact": compact,
            "max_chars": data_or_args.get("max_chars", 320, type=int),
            "per_source": max(1, data_or_args.get("per_source", 1, type=int)),
            "drop_superseded": gb("drop_superseded", True),
            "drop_hearsay_if_canon": gb("drop_hearsay", True),
            "pack": gb("pack", True),
        }
    compact = _parse_bool(data_or_args.get("compact"), True)
    if _parse_bool(data_or_args.get("full"), False):
        compact = False
    return {
        "compact": compact,
        "max_chars": int(data_or_args.get("max_chars", 320)),
        "per_source": max(1, int(data_or_args.get("per_source", 1))),
        "drop_superseded": _parse_bool(data_or_args.get("drop_superseded"), True),
        "drop_hearsay_if_canon": _parse_bool(data_or_args.get("drop_hearsay"), True),
        "pack": _parse_bool(data_or_args.get("pack"), True),
    }


def _apply_search_pack(query, results, top_k, pack_opts):
    from vector_memory import pack_results, results_token_stats
    if not pack_opts.get("pack", True):
        trimmed = (results or [])[:top_k]
        return trimmed, results_token_stats(trimmed)
    packed = pack_results(
        results or [],
        query=query,
        top_k=top_k,
        max_chars=pack_opts.get("max_chars", 320),
        per_source=pack_opts.get("per_source", 1),
        drop_superseded=pack_opts.get("drop_superseded", True),
        drop_hearsay_if_canon=pack_opts.get("drop_hearsay_if_canon", True),
        compact=pack_opts.get("compact", True),
    )
    return packed, results_token_stats(packed)




# --- 按需用法文档（默认 mini，防上下文暴增）---
@app.route('/help', methods=['GET'])
@app.route('/docs', methods=['GET'])
def api_help():
    """Agent 按需取用法说明。默认 mini；full 很长请勿默认注入 prompt。"""
    level = (request.args.get('level') or request.args.get('v') or 'mini').lower()
    fmt = (request.args.get('format') or 'text').lower()
    if level in ('full', 'long', 'all', '3'):
        level = 'full'
        path = os.path.join(os.path.dirname(__file__), 'docs', 'USAGE.md')
    elif level in ('short', 'quick', '2', 's'):
        level = 'short'
        path = os.path.join(os.path.dirname(__file__), 'docs', 'USAGE.short.md')
    else:
        level = 'mini'
        path = os.path.join(os.path.dirname(__file__), 'docs', 'USAGE.mini.txt')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        text = f"help unavailable: {e}. See /opt/nebula/docs/"
    chars = len(text)
    est = max(1, int(chars / 2.2))
    payload = {
        'status': 'ok',
        'level': level,
        'chars': chars,
        'est_tokens': est,
        'text': text,
        'hint': '默认 level=mini 省 token；需要时再 short/full。勿把 full 写入每轮 system prompt。',
        'links': {
            'mini': '/help?level=mini',
            'short': '/help?level=short',
            'full': '/help?level=full',
            'json': f'/help?level={level}&format=json',
        },
    }
    if fmt == 'json':
        return jsonify(payload)
    # text/plain 便于 curl
    from flask import Response
    return Response(
        text + f"\n\n---\n# meta: level={level} chars={chars} est_tokens≈{est} | JSON: /help?level={level}&format=json\n",
        mimetype='text/plain; charset=utf-8',
    )



@app.route('/search', methods=['GET', 'POST'])
def search():
    if request.method == 'GET':
        query = request.args.get('q', '')
        top_k = request.args.get('top_k', 5, type=int)
        rerank = request.args.get('rerank', 'false').lower() == 'true'
        rerank_candidates = request.args.get('rerank_candidates', 8, type=int)
        category = request.args.get('category') or request.args.get('cat')
        use_hybrid = request.args.get('use_hybrid', 'true').lower() == 'true'
        explain = request.args.get('explain', 'false').lower() == 'true'
        enable_time_decay = request.args.get('enable_time_decay', 'true').lower() == 'true'
        time_decay_lambda = request.args.get('time_decay_lambda', 0.05, type=float)
        use_cache = request.args.get('use_cache', 'true').lower() == 'true'
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        project_name = request.args.get('project_name')
        location = request.args.get('location')
        similarity_threshold = request.args.get('similarity_threshold', 0.0, type=float)
        rewrite = request.args.get('rewrite', 'false').lower() == 'true'
        n_rewrites = request.args.get('n_rewrites', 2, type=int)
        smart_rewrite = request.args.get('smart_rewrite', 'false').lower() == 'true'
        pack_opts = _search_pack_params(request.args, is_get=True)
    else:
        data = request.get_json() or {}
        query = data.get('query', '')
        top_k = data.get('top_k', 5)
        rerank = _parse_bool(data.get('rerank'), False)
        rerank_candidates = data.get('rerank_candidates', 8)
        category = data.get('category')
        use_hybrid = _parse_bool(data.get('use_hybrid'), True)
        explain = _parse_bool(data.get('explain'), False)
        enable_time_decay = _parse_bool(data.get('enable_time_decay'), True)
        time_decay_lambda = data.get('time_decay_lambda', 0.05)
        use_cache = _parse_bool(data.get('use_cache'), True)
        date_from = data.get('date_from')
        date_to = data.get('date_to')
        project_name = data.get('project_name')
        location = data.get('location')
        similarity_threshold = data.get('similarity_threshold', 0.0)
        rewrite = _parse_bool(data.get('rewrite'), False)
        n_rewrites = data.get('n_rewrites', 2)
        smart_rewrite = _parse_bool(data.get('smart_rewrite'), False)
        pack_opts = _search_pack_params(data, is_get=False)

    image_q = None
    if request.method == 'POST':
        image_q = data.get('image') or data.get('image_url')
        if (not image_q) and request.files and request.files.get('image'):
            image_q = request.files.get('image').read()
    if not query and not image_q:
        return jsonify({'error': 'query is required'}), 400
    if not query and image_q:
        query = '[image-query]'
        use_hybrid = False

    mm = get_engine()
    t0 = time.time()

    if rewrite:
        # [FIX Bug5] search_with_rewrite 也经过 embedding_cache
        # 原代码直接调 mm.search_with_rewrite(), 内部的 self.search()
        # 不经过 server 层 cache, 导致 cache hit/miss 计数始终为 0。
        # 修复: 在 server 层拆开 rewrite 流程, 每个子查询都查/写 cache。
        rewriter = QueryRewriter()
        rewrites = rewriter.rewrite(query, n=n_rewrites)

        all_results = {}
        rewrite_counts = {}
        cache_hits_in_rewrite = 0

        search_base = {
            'top_k': top_k * 3 if pack_opts.get('pack', True) else top_k, 'category': category,
            'rerank': rerank, 'rerank_candidates': rerank_candidates,
            'use_hybrid': use_hybrid, 'explain': explain,
            'enable_time_decay': enable_time_decay, 'time_decay_lambda': time_decay_lambda,
            'date_from': date_from, 'date_to': date_to,
            'project_name': project_name, 'location': location,
            'similarity_threshold': similarity_threshold,
        }

        for rw in rewrites:
            rw_params = dict(search_base)
            rw_params['query'] = rw

            # [perf] embedder 缓存；hybrid 也可命中
            cached_vec = embedding_cache.get(rw) if use_cache else None
            if cached_vec is not None:
                cache_hits_in_rewrite += 1
                if not use_hybrid:
                    rw_params['precomputed_vector'] = cached_vec
            rw_params['use_cache'] = use_cache
            results = mm.search(**rw_params)

            rewrite_counts[rw] = len(results)
            for r in results:
                mid = r["id"]
                if mid not in all_results or r["score"] > all_results[mid]["score"]:
                    all_results[mid] = r

        merged = sorted(all_results.values(), key=lambda x: x["score"], reverse=True)[: max(top_k * 3, top_k)]
        packed, tok_stats = _apply_search_pack(query, merged, top_k, pack_opts)

        elapsed = round((time.time() - t0) * 1000, 1)
        return jsonify({
            'status': 'ok',
            'results': packed,
            'count': len(packed),
            'elapsed_ms': elapsed,
            'rewrites': rewrites,
            'rewrite_counts': rewrite_counts,
            'original_count': rewrite_counts.get(query, 0),
            'cache_stats': embedding_cache.stats(),
            'cache_hits_in_rewrite': cache_hits_in_rewrite,
            'use_hybrid': use_hybrid,
            'rewrite': True,
            'pack': pack_opts.get('pack', True),
            'compact': pack_opts.get('compact', True),
            'token_stats': tok_stats,
        })

    _rc_key = result_cache.make_key("search", {
        "q": query, "top_k": top_k, "cat": category, "hybrid": use_hybrid,
        "rerank": rerank, "rerank_candidates": rerank_candidates,
        "pack": pack_opts.get("pack"), "compact": pack_opts.get("compact"),
        "max_chars": pack_opts.get("max_chars"), "per_source": pack_opts.get("per_source"),
        "drop_s": pack_opts.get("drop_superseded"), "drop_h": pack_opts.get("drop_hearsay_if_canon"),
    })
    _rc_hit = result_cache.get(_rc_key)
    if _rc_hit is not None:
        elapsed = round((time.time() - t0) * 1000, 1)
        out = dict(_rc_hit)
        out["elapsed_ms"] = elapsed
        out["result_cache_hit"] = True
        out["result_cache"] = result_cache.stats()
        out["cache_stats"] = embedding_cache.stats()
        return jsonify(out)

    fetch_k = top_k * 3 if pack_opts.get('pack', True) else top_k
    search_params = {
        'query': query, 'top_k': fetch_k, 'category': category,
        'rerank': rerank, 'rerank_candidates': rerank_candidates,
        'use_hybrid': use_hybrid, 'explain': explain,
        'enable_time_decay': enable_time_decay, 'time_decay_lambda': time_decay_lambda,
        'use_cache': use_cache, 'date_from': date_from, 'date_to': date_to,
        'project_name': project_name, 'location': location,
        'similarity_threshold': similarity_threshold,
    }

    # [perf] embedder 层已缓存；此处仅上报 hit 状态，禁止 miss 后再 embed 一次
    cached_vec = embedding_cache.get(query) if use_cache else None
    cache_hit = cached_vec is not None
    if cache_hit and not use_hybrid:
        search_params['precomputed_vector'] = cached_vec
    results = mm.search(**search_params)

    # 弱结果智能 rewrite：top1 分过低时再开一路改写（省默认延迟，补召回）
    used_smart_rewrite = False
    if smart_rewrite and results:
        top_score = float(results[0].get('score') or 0)
        # hybrid RRF 分通常较小；向量分通常 0.3~0.8
        weak = (use_hybrid and top_score < 0.02) or ((not use_hybrid) and top_score < 0.42)
        if weak:
            used_smart_rewrite = True
            try:
                rewriter = QueryRewriter()
                rewrites = rewriter.rewrite(query, n=min(2, n_rewrites))
                all_results = {r['id']: r for r in results}
                for rw in rewrites:
                    if rw == query:
                        continue
                    rw_params = dict(search_params)
                    rw_params['query'] = rw
                    rw_params.pop('precomputed_vector', None)
                    for r in mm.search(**rw_params):
                        mid = r['id']
                        if mid not in all_results or r['score'] > all_results[mid]['score']:
                            all_results[mid] = r
                results = sorted(all_results.values(), key=lambda x: x['score'], reverse=True)[:top_k]
            except Exception as e:
                logger.warning(f"smart_rewrite 失败: {e}")

    packed, tok_stats = _apply_search_pack(query, results or [], top_k, pack_opts)

    elapsed = round((time.time() - t0) * 1000, 1)
    resp = {
        'status': 'ok', 'results': packed, 'count': len(packed),
        'elapsed_ms': elapsed, 'cache_hit': cache_hit,
        'cache_stats': embedding_cache.stats(),
        'use_hybrid': use_hybrid,
        'rewrite': rewrite,
        'smart_rewrite_used': used_smart_rewrite,
        'pack': pack_opts.get('pack', True),
        'compact': pack_opts.get('compact', True),
        'token_stats': tok_stats,
        'result_cache_hit': False,
        'result_cache': result_cache.stats(),
    }
    try:
        result_cache.put(_rc_key, resp)
    except Exception:
        pass
    return jsonify(resp)




# --- Agent 省 token 问答包 ---
@app.route('/ask', methods=['POST', 'GET'])
def ask_memory():
    """v4 省 token 问答：默认 reflect（多跳+缺口补搜+图扩展）。"""
    if request.method == 'GET':
        query = request.args.get('q') or request.args.get('query') or ''
        top_k = request.args.get('top_k', 5, type=int)
        max_chars = request.args.get('max_chars', 280, type=int)
        max_total = request.args.get('max_total_chars', 1800, type=int)
        use_hybrid = request.args.get('use_hybrid', 'true').lower() == 'true'
        category = request.args.get('category')
        hops = request.args.get('hops', 2, type=int)
        no_cache = request.args.get('no_cache', 'false').lower() == 'true'
        engine = request.args.get('engine', 'ultimate')
        use_graph = request.args.get('use_graph', 'true').lower() == 'true'
        reader_in = request.args.get('reader')
    else:
        data = request.get_json() or {}
        query = data.get('query') or data.get('q') or ''
        top_k = int(data.get('top_k', 5))
        max_chars = int(data.get('max_chars', 280))
        max_total = int(data.get('max_total_chars', 1800))
        use_hybrid = _parse_bool(data.get('use_hybrid'), True)
        category = data.get('category')
        hops = int(data.get('hops', 2))
        no_cache = _parse_bool(data.get('no_cache'), False)
        engine = data.get('engine', 'ultimate')
        use_graph = _parse_bool(data.get('use_graph'), True)
        reader_in = data.get('reader')

    # 时间意图 / 分层 prefer：可显式传入，否则规则推断（无时间词=neutral）
    if request.method == 'GET':
        temporal_intent = request.args.get('temporal_intent')
        as_of_raw = request.args.get('as_of')
        layer_raw = request.args.get('memory_layer')
    else:
        data_t = request.get_json(silent=True) or {}
        temporal_intent = data_t.get('temporal_intent')
        as_of_raw = data_t.get('as_of')
        layer_raw = data_t.get('memory_layer') or data_t.get('prefer_layers')
    as_of = parse_as_of(as_of_raw)
    if not temporal_intent:
        temporal_intent = classify_temporal_intent(query)
    prefer_layers = None
    if isinstance(layer_raw, list):
        prefer_layers = [str(x) for x in layer_raw if x]
    elif isinstance(layer_raw, str) and layer_raw.strip():
        prefer_layers = [x.strip() for x in layer_raw.split(",") if x.strip()]
    # 不自动推断 layer：家用题「怎么用」会被当成 procedural 把 SOP 挤掉。
    # 调用方要分层时显式传 memory_layer。
    try:
        from nebula_site import resolve_tenant, REQUIRE_TENANT
    except Exception:
        resolve_tenant = lambda x=None: (x or "")
        REQUIRE_TENANT = False
    tenant_hdr = request.headers.get("X-Nebula-Tenant")
    if request.method == "POST":
        tenant_body = (request.get_json(silent=True) or {}).get("tenant_id")
    else:
        tenant_body = request.args.get("tenant_id")
    tenant_id = resolve_tenant(tenant_body or tenant_hdr)
    if REQUIRE_TENANT and not tenant_id:
        return jsonify({"error": "tenant_id required"}), 400

    image_q = None
    if request.method == 'POST':
        _body = request.get_json(silent=True) or {}
        image_q = _body.get('image') or _body.get('image_url')
        if (not image_q) and request.files and request.files.get('image'):
            image_q = request.files.get('image').read()
    if not query and not image_q:
        return jsonify({'error': 'query is required'}), 400
    if not query and image_q:
        query = '[image-query]'
        use_hybrid = False

    t0 = time.time()
    # llm 参数先解析，纳入 result_cache key
    llm_deep = 'auto'
    llm_answer = False  # [perf] 默认关闭润色；compose_answer 抽取式足够
    rerank = True
    reader = _parse_bool(reader_in, False)
    if request.method == 'GET':
        llm_deep = request.args.get('llm_deep', 'auto')
        llm_answer = request.args.get('llm_answer', 'false').lower() == 'true'
        rerank = request.args.get('rerank', 'true').lower() != 'false'
    else:
        data_llm = request.get_json(silent=True) or {}
        llm_deep = data_llm.get('llm_deep', 'auto')
        llm_answer = _parse_bool(data_llm.get('llm_answer'), False)
        rerank = _parse_bool(data_llm.get('rerank'), True)

    rc_key = result_cache.make_key("ask_v4", {
        "q": query, "top_k": top_k, "max_chars": max_chars, "max_total": max_total,
        "hybrid": use_hybrid, "cat": category, "hops": hops, "engine": engine, "graph": use_graph,
        "llm_deep": llm_deep, "llm_answer": llm_answer, "rerank": rerank, "reader": reader,
        "ti": temporal_intent, "as_of": as_of, "layers": prefer_layers, "tenant": tenant_id,
        "img": bool(image_q),
    })
    if not no_cache:
        hit = result_cache.get(rc_key)
        if hit is not None:
            out = dict(hit)
            out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            out["result_cache_hit"] = True
            out["result_cache"] = result_cache.stats()
            return jsonify(out)

    mm = get_engine()
    if engine in ('ultimate', 'v5', 'auto'):
        from nebula_v5 import ultimate_ask
        pre_vec = None
        if image_q:
            from vector_memory import resolve_image
            cap = None if query == '[image-query]' else query
            pre_vec = mm.embedder.embed_image(image_q, caption=cap)
        resp = ultimate_ask(
            mm, query=query, top_k=top_k, max_chars=max_chars,
            max_total_chars=max_total, use_hybrid=use_hybrid,
            category=category, use_graph=use_graph, hops=hops,
            llm_deep=llm_deep, llm_answer=llm_answer, rerank=rerank,
            temporal_intent=temporal_intent, as_of=as_of, prefer_layers=prefer_layers,
            reader=reader,
            tenant_id=tenant_id,
            precomputed_vector=pre_vec,
        )
    elif engine == 'reflect':
        from nebula_v4 import reflect_ask
        resp = reflect_ask(
            mm, query=query, top_k=top_k, max_chars=max_chars,
            max_total_chars=max_total, use_hybrid=use_hybrid,
            category=category, use_graph=use_graph, hops=hops,
            temporal_intent=temporal_intent, as_of=as_of, prefer_layers=prefer_layers,
            tenant_id=tenant_id,
        )
    else:
        from vector_memory import pack_results, results_token_stats, format_ask_pack, multi_hop_search
        if hops and hops > 1:
            mh = multi_hop_search(
                mm, query=query, top_k=top_k, hops=hops,
                use_hybrid=use_hybrid, category=category, max_chars=max_chars,
            )
            packed = mh["results"]
            followups = mh.get("followups") or []
            raw_n = mh.get("raw_candidates", 0)
            hops_used = mh.get("hops", hops)
        else:
            raw = mm.search(
                query=query, top_k=max(top_k * 4, 12), category=category,
                use_hybrid=use_hybrid, enable_time_decay=True,
            )
            packed = pack_results(
                raw, query=query, top_k=top_k, max_chars=max_chars,
                per_source=1, drop_superseded=True, drop_hearsay_if_canon=True, compact=True,
            )
            followups, raw_n, hops_used = [], len(raw or []), 1
        pack_text = format_ask_pack(query, packed, max_total_chars=max_total)
        stats = results_token_stats(packed)
        stats["pack_chars"] = len(pack_text)
        stats["pack_est_tokens"] = max(1, int(len(pack_text) / 2.2))
        resp = {
            'status': 'ok', 'query': query, 'pack': pack_text, 'results': packed,
            'count': len(packed), 'token_stats': stats, 'hops': hops_used,
            'followups': followups, 'raw_candidates': raw_n, 'engine': 'legacy',
            'hint': 'pack 塞上下文；readback 回读 vault',
        }
    resp['elapsed_ms'] = round((time.time() - t0) * 1000, 1)
    resp['result_cache_hit'] = False
    resp['result_cache'] = result_cache.stats()
    resp['version'] = 'v4.0'
    if not no_cache:
        try:
            result_cache.put(rc_key, resp)
        except Exception:
            pass
    return jsonify(resp)



@app.route('/search_pack', methods=['POST', 'GET'])
def search_pack_alias():
    """/ask 别名。"""
    return ask_memory()


# --- Search Rerank ---

@app.route('/search_rerank', methods=['POST'])
def search_rerank():
    data = request.get_json() or {}
    query = data.get('query', '') or data.get('q', '')
    top_k = int(data.get('top_k', 10))
    rerank_candidates = int(data.get('rerank_candidates', 5))
    category = data.get('category')

    if not query:
        return jsonify({'error': 'query is required'}), 400

    mm = get_engine()
    result = mm.search_with_rewrite(query=query, top_k=top_k, category=category)

    if result.get('results'):
        try:
            from reranker import apply_rerank
            result['results'] = apply_rerank(query, result['results'], candidates=rerank_candidates)
            result['rerank_backend'] = 'qwen3-rerank'
            result['rerank_model'] = __import__('os').environ.get('NEBULA_RERANK_MODEL') or 'qwen3-rerank'
        except Exception as e:
            result['rerank_error'] = str(e)
    return jsonify({'status': 'ok', **result})


# --- Reranker Status ---

@app.route('/reranker/status', methods=['GET'])
def reranker_status():
    try:
        from reranker import get_reranker, _rerank_enabled
        import os
        reranker = get_reranker()
        status = {
            'loaded': bool(reranker and reranker.is_loaded),
            'enabled': _rerank_enabled(),
            'backend': (reranker.backend if reranker else 'qwen3-rerank'),
            'device': 'bailian',
            'model': os.environ.get('NEBULA_RERANK_MODEL') or 'qwen3-rerank',
            'chat_url': os.environ.get('NEBULA_RERANK_URL', ''),
        }
    except Exception as e:
        status = {'loaded': False, 'backend': 'qwen3-rerank', 'device': 'none', 'error': str(e)}
    return jsonify(status)


# --- Rewrite Search ---

@app.route('/rewrite_search', methods=['POST'])
def rewrite_search():
    data = request.get_json() or {}
    query = data.get('query', '') or data.get('q', '')
    top_k = int(data.get('top_k', 10))
    n_rewrites = int(data.get('n_rewrites', 2))
    category = data.get('category')

    if not query:
        return jsonify({'error': 'query is required'}), 400

    mm = get_engine()
    result = mm.search_with_rewrite(query=query, top_k=top_k, n_rewrites=n_rewrites, category=category)
    return jsonify({'status': 'ok', **result})


# --- Ingest ---

@app.route('/ingest', methods=['POST'])
def ingest():
    data = request.get_json(force=True, silent=True) or {}
    filepath = data.get('filepath')
    if not filepath:
        return jsonify({'error': 'filepath is required'}), 400
    mm = get_engine()
    ingestor = MemoryIngestor(mm)
    added = ingestor.ingest_file(
        filepath=filepath, category=data.get('category'),
        chunk_size=data.get('chunk_size', 500), importance=data.get('importance', 0.5),
    )
    return jsonify({'status': 'ok', 'added': added})


# --- Stats ---

@app.route('/stats', methods=['GET'])
def stats():
    mm = get_engine()
    conn = mm.conn
    total = conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
    cats = [r[0] for r in conn.execute('SELECT DISTINCT category FROM memories WHERE category IS NOT NULL').fetchall()]
    db_size_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    if db_size_bytes > 1024 * 1024:
        db_size = f"{db_size_bytes / 1024 / 1024:.1f}MB"
    elif db_size_bytes > 1024:
        db_size = f"{db_size_bytes / 1024:.0f}KB"
    else:
        db_size = f"{db_size_bytes}B"
    return jsonify({
        'total_memories': total, 'categories': cats,
        'db_size': db_size, 'embedding_cache': embedding_cache.stats(),
    })


# --- Tags ---

@app.route('/tags/cloud', methods=['GET'])
def tags_cloud():
    mm = get_engine()
    limit = request.args.get('limit', 30, type=int)
    rows = mm.conn.execute(
        'SELECT name, usage_count FROM tags ORDER BY usage_count DESC LIMIT ?', (limit,)
    ).fetchall()
    return jsonify([{'name': r[0], 'count': r[1]} for r in rows])


@app.route('/tags/by-category', methods=['GET'])
def tags_by_category():
    mm = get_engine()
    rows = mm.conn.execute(
        'SELECT category, COUNT(*) as cnt FROM tags WHERE category IS NOT NULL GROUP BY category ORDER BY cnt DESC'
    ).fetchall()
    return jsonify({r[0]: r[1] for r in rows})


@app.route('/tags/rename', methods=['POST'])
def rename_tag():
    data = request.get_json() or {}
    old_name = data.get('old')
    new_name = data.get('new')
    if not old_name or not new_name:
        return jsonify({'error': 'old and new names required'}), 400
    mm = get_engine()
    cur = mm.conn.execute('UPDATE tags SET name = ? WHERE name = ?', (new_name, old_name))
    mm.conn.commit()
    return jsonify({'status': 'ok', 'affected': cur.rowcount})


@app.route('/tags/merge', methods=['POST'])
def merge_tags():
    data = request.get_json() or {}
    from_tags = data.get('from', [])
    to_tag = data.get('to', '')
    if not from_tags or not to_tag:
        return jsonify({'error': 'from list and to name required'}), 400
    mm = get_engine()
    total = 0
    for tag in from_tags:
        cur = mm.conn.execute('UPDATE tags SET name = ? WHERE name = ?', (to_tag, tag))
        total += cur.rowcount
    mm.conn.commit()
    return jsonify({'status': 'ok', 'affected': total})


@app.route('/tags/cleanup', methods=['POST'])
def cleanup_tags():
    mm = get_engine()
    cur = mm.conn.execute('DELETE FROM tags WHERE usage_count = 0 OR usage_count IS NULL')
    mm.conn.commit()
    return jsonify({'status': 'ok', 'cleaned': cur.rowcount})


# --- Memory CRUD ---

@app.route('/memory/add', methods=['POST'])
def add_memory():
    data = request.get_json(silent=True) or {}
    if not data and request.form:
        data = request.form.to_dict()
    mm = get_engine()
    try:
        from nebula_site import resolve_tenant, REQUIRE_TENANT
    except Exception:
        resolve_tenant = lambda x=None: (x or "")
        REQUIRE_TENANT = False
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    meta = dict(meta)
    tenant = resolve_tenant(data.get("tenant_id") or request.headers.get("X-Nebula-Tenant") or meta.get("tenant_id"))
    if REQUIRE_TENANT and not tenant:
        return jsonify({"error": "tenant_id required"}), 400
    if tenant:
        meta["tenant_id"] = tenant
    image = data.get('image') or data.get('image_url') or data.get('image_path')
    if request.files and request.files.get('image'):
        fs = request.files.get('image')
        image = fs.read()
        if not meta.get("image_name"):
            meta["image_name"] = fs.filename or "upload"
    try:
        result = mm.add(
            content=data.get('content', ''), source=data.get('source', 'api'),
            category=data.get('category'), tags=data.get('tags'),
            importance=data.get('importance', 0.5),
            metadata=meta or None,
            semantic_dedup=_parse_bool(data.get('semantic_dedup'), True),
            created_at=parse_as_of(data.get('created_at')),
            image=image,
        )
    except ValueError as e:
        msg = str(e)
        if msg.startswith('SECRET_REJECTED'):
            return jsonify({
                'status': 'rejected',
                'error': 'secret_in_content',
                'message': msg,
                'hint': 'POST /secrets/store 写入 Vaultwarden；POST /secrets/register 只登记指针',
            }), 400
        raise
    return jsonify({'status': 'ok', **result})




@app.route('/memory/image/<int:memory_id>', methods=['GET'])
def serve_memory_image(memory_id):
    """回显入库图片。路径必须落在 NEBULA_IMAGE_DIR / data/images。"""
    from vector_memory import image_dir
    from flask import send_file, abort
    mm = get_engine()
    row = mm.conn.execute("SELECT metadata FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if not row or not row[0]:
        return jsonify({"error": "not found"}), 404
    try:
        meta = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    except Exception:
        meta = {}
    if (meta.get("modality") or "") != "image":
        return jsonify({"error": "not an image memory"}), 404
    path = meta.get("image_path") or ""
    if not path:
        src = meta.get("image_src") or ""
        if src.startswith("http"):
            return jsonify({"status": "redirect", "url": src}), 302
        return jsonify({"error": "no image_path"}), 404
    root = os.path.realpath(image_dir(mm.db_path))
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep) and real != root:
        abort(403)
    if not os.path.isfile(real):
        return jsonify({"error": "file missing"}), 404
    return send_file(real, mimetype=meta.get("image_mime") or None)




@app.route("/memory/promote", methods=["POST"])
def promote_to_vault():
    """把一条记忆写成 Obsidian 笔记（真理库写回）。进 06-Agent会话提炼/。"""
    import re as _re
    from datetime import date
    data = request.get_json(silent=True) or {}
    mm = get_engine()
    mid = data.get("id")
    title = (data.get("title") or "").strip()
    body = (data.get("content") or "").strip()
    src = data.get("source") or "promote"
    if mid and not body:
        row = mm.conn.execute(
            "SELECT id, content, source_file, category FROM memories WHERE id=?", (mid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not_found", "id": mid}), 404
        body = (row[1] or "").strip()
        src = row[2] or src
        if not title:
            title = (body.splitlines()[0] if body else "untitled")[:40]
    if not body:
        return jsonify({"error": "content or id required"}), 400
    if not title:
        title = body.splitlines()[0][:40]
    slug = _re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title).strip("-")[:40] or "note"
    day = date.today().isoformat()
    dest_dir = Path("/home/huhu/obsidian-vault/notes/06-Agent会话提炼")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("%s-%s.md" % (day, slug))
    n = 2
    while dest.exists():
        dest = dest_dir / ("%s-%s-%d.md" % (day, slug, n))
        n += 1
    text = (
        "---\n"
        "title: %s\n"
        "date: %s\n"
        "source: %s\n"
        "nebula_id: %s\n"
        "trust: source\n"
        "---\n\n"
        "# %s\n\n"
        "%s\n\n"
        "> 由星枢 /memory/promote 写回。冲突以本笔记为准。\n"
        % (title.replace("\n", " "), day, src, mid or "", title, body)
    )
    dest.write_text(text, encoding="utf-8")
    rel = "notes/06-Agent会话提炼/" + dest.name
    vault_key = "vault:" + rel
    try:
        added = mm.add(
            content="[虎虎笔记] path=%s source=%s title=%s\n回读命令: rxt read --host huhu \"%s\"\n---\n%s"
            % (rel, vault_key, title, dest.as_posix(), body[:4000]),
            source=vault_key,
            category="lesson",
            importance=0.8,
            semantic_dedup=False,
        )
    except Exception as e:
        added = {"error": str(e)}
    return jsonify({
        "status": "ok",
        "path": str(dest),
        "vault_key": vault_key,
        "nebula": added,
    })


@app.route('/memory/<int:memory_id>', methods=['DELETE'])
def delete_memory(memory_id):
    mm = get_engine()
    mm.delete(memory_id)
    return jsonify({'status': 'ok'})


@app.route('/memory/<int:memory_id>', methods=['PUT'])
def update_memory(memory_id):
    """原地更新记忆字段(id 不变)。新增 2026-06-23:替代 daemon 的 delete+insert,
    避免 id 漂移。支持 content/category/importance/tags 的部分更新。
    content 变化时重算 content_hash + embedding;hash 冲突则保留旧 content。
    """
    import time
    data = request.get_json(force=True, silent=True) or {}
    mm = get_engine()

    # 读原记录
    row = mm.conn.execute(
        'SELECT id, content, category, importance, content_hash FROM memories WHERE id = ?',
        (memory_id,)
    ).fetchone()
    if not row:
        return jsonify({'status': 'error', 'error': 'not_found', 'id': memory_id}), 404

    old_id, old_content, old_cat, old_imp, old_hash = row
    new_content = data.get('content', old_content)
    new_cat = data.get('category', old_cat)
    new_imp = data.get('importance', old_imp)
    content_changed = ('content' in data) and (new_content != old_content)
    now = time.time()

    with mm._db_lock:
        try:
            if content_changed:
                new_hash = content_hash(new_content)
                # UPDATE content + category + importance + hash + ts;memories_fts_update trigger 自动同步 FTS
                mm.conn.execute(
                    """UPDATE memories SET content=?, category=?, importance=?,
                       content_hash=?, updated_at=?, last_accessed=? WHERE id=?""",
                    (new_content, new_cat, new_imp, new_hash, now, now, memory_id)
                )
            else:
                mm.conn.execute(
                    """UPDATE memories SET category=?, importance=?, updated_at=?, last_accessed=?
                       WHERE id=?""",
                    (new_cat, new_imp, now, now, memory_id)
                )
            mm.conn.commit()
        except Exception as e:
            mm.conn.rollback()
            return jsonify({'status': 'error', 'error': str(e), 'id': memory_id}), 500

    # tags 更新(只在明确传入 tags 时)
    if 'tags' in data:
        new_tags = data.get('tags') or []
        with mm._db_lock:
            mm.conn.execute('DELETE FROM memory_tags WHERE memory_id=?', (memory_id,))
            for tag_name in new_tags:
                tag_name = (tag_name or '').strip()
                if not tag_name:
                    continue
                mm.conn.execute('INSERT OR IGNORE INTO tags(name) VALUES(?)', (tag_name,))
                tid = mm.conn.execute('SELECT id FROM tags WHERE name=? COLLATE NOCASE', (tag_name,)).fetchone()[0]
                mm.conn.execute(
                    'INSERT OR IGNORE INTO memory_tags(memory_id, tag_id, auto_tagged, confidence) VALUES(?,?,0,1.0)',
                    (memory_id, tid)
                )
            mm.conn.commit()

    # content 变化时重算 embedding 并写库;矩阵缓存置失效,下次 search 全量重建。
    # (不用增量 mark_deleted+append:同 id 追加后 dirty 集合残留会误删,矩阵未加载时
    #  dirty 也无法正确生效。直接失效缓存最简单可靠。)
    if content_changed:
        try:
            vec = mm.embedder.embed(new_content)
            blob = embedding_to_blob(vec)
            dim = int(len(vec))
            with mm._db_lock:
                mm.conn.execute(
                    'UPDATE memories SET embedding=?, embedding_dim=?, updated_at=? WHERE id=?',
                    (blob, dim, now, memory_id)
                )
                mm.conn.commit()
            # 置矩阵缓存失效,下次 search 从 db 全量加载(含新 embedding)
            with mm._emb_lock:
                mm._emb_matrix = None
                mm._emb_ids = None
                mm._emb_dirty_deletes.clear()
        except Exception as e:
            app.logger.warning(f'update {memory_id} embedding 重算失败(字段已更新): {e}')

    return jsonify({'status': 'ok', 'id': memory_id, 'fields': list(data.keys())})


@app.route('/memory/reclassify', methods=['POST'])
def reclassify():
    data = request.get_json(force=True, silent=True) or {}
    mm = get_engine()
    result = mm.batch_recategorize(
        category_filter=data.get('category'), method='rule',
        limit=data.get('limit', 100), dry_run=data.get('dry_run', False),
    )
    return jsonify({'status': 'ok', 'result': result})


# --- Compress [e6] re-enabled ---

@app.route('/compress', methods=['POST'])
def compress():
    """Compress memories by date (or auto-compress memories older than N days)."""
    data = request.get_json(force=True, silent=True) or {}
    mm = get_engine()
    compressor = MemoryCompressor(mm)

    date_str = data.get('date')
    if date_str:
        summary = compressor.compress_date(date_str)
        return jsonify({'status': 'ok', 'date': date_str, 'summary': summary})
    else:
        days_ago = data.get('days_ago', 30)
        results = compressor.compress_range(days_ago=days_ago)
        return jsonify({'status': 'ok', 'compressed_dates': len(results), 'results': results})


# --- Search UI ---

UI_DIR = os.path.join(os.path.dirname(__file__), 'scripts')

@app.route('/ui', methods=['GET'])
def serve_ui():
    return send_from_directory(UI_DIR, 'vm-search-ui.html')





# ==================== Vaultwarden 密钥桥 ====================

@app.route('/secrets/status', methods=['GET'])
def secrets_status():
    from nebula_secrets import bw_status
    st = bw_status()
    st['policy'] = '明文只存 Vaultwarden；星枢仅 vaultwarden 指针'
    return jsonify(st)


@app.route('/secrets/list', methods=['GET'])
def secrets_list():
    """仅返回条目名/用户名，无密码。"""
    try:
        from nebula_secrets import list_items
        limit = request.args.get('limit', 100, type=int)
        rows = list_items(limit=limit)
        return jsonify({'status': 'ok', 'count': len(rows), 'items': rows})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 503


@app.route('/secrets/get', methods=['POST'])
def secrets_get():
    """取明文。必须 reveal=true。禁止把返回值再写入 /memory/add。"""
    data = request.get_json() or {}
    name = data.get('name') or data.get('item')
    reveal = _parse_bool(data.get('reveal'), False)
    if not name:
        return jsonify({'error': 'name required'}), 400
    if not reveal:
        return jsonify({
            'status': 'ok',
            'name': name,
            'revealed': False,
            'hint': '加 "reveal":true 才返回明文；推荐本机 bw-ai get <name>',
            'command': f'bw-ai get {name}',
        })
    try:
        from nebula_secrets import get_password, get_username
        # 不写 access log 的 password 字段
        pw = get_password(name)
        try:
            user = get_username(name)
        except Exception:
            user = ''
        return jsonify({
            'status': 'ok',
            'name': name,
            'username': user,
            'password': pw,
            'revealed': True,
            'warning': '禁止写入星枢/笔记/prompt 日志；用完即弃',
        })
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 503


@app.route('/secrets/store', methods=['POST'])
def secrets_store():
    """明文 → Vaultwarden；星枢只登记指针。"""
    data = request.get_json() or {}
    name = data.get('name')
    password = data.get('password') or data.get('secret') or data.get('value')
    username = data.get('username') or data.get('user') or ''
    notes = data.get('notes') or data.get('purpose') or ''
    register = _parse_bool(data.get('register_memory'), True)
    if not name or not password:
        return jsonify({'error': 'name and password/secret required'}), 400
    try:
        from nebula_secrets import create_login_item, register_pointer_memory
        created = create_login_item(name, username, password, notes=notes)
        mem = None
        if register:
            mm = get_engine()
            mem = register_pointer_memory(
                mm, name=name, purpose=notes or 'API/密钥',
                username=username, importance=float(data.get('importance', 0.85)),
            )
        return jsonify({
            'status': 'ok',
            'vaultwarden': {'name': created.get('name'), 'id': created.get('id')},
            'memory_pointer': mem,
            'hint': '明文仅在密码本；星枢只有指针',
        })
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 503


@app.route('/secrets/register', methods=['POST'])
def secrets_register():
    """已有密码本条目 → 星枢指针（不读明文）。"""
    data = request.get_json() or {}
    name = data.get('name')
    if not name:
        return jsonify({'error': 'name required'}), 400
    mm = get_engine()
    from nebula_secrets import register_pointer_memory
    r = register_pointer_memory(
        mm, name=name,
        purpose=data.get('purpose') or data.get('notes') or '',
        username=data.get('username') or '',
        importance=float(data.get('importance', 0.85)),
    )
    return jsonify({'status': 'ok', **r})


@app.route('/secrets/catalog-sync', methods=['POST'])
def secrets_catalog_sync():
    """全量同步条目名→星枢指针（无明文）。"""
    mm = get_engine()
    from nebula_secrets import sync_catalog_to_memory
    r = sync_catalog_to_memory(mm)
    return jsonify({'status': 'ok', **r})


@app.route('/secrets/resolve', methods=['POST'])
def secrets_resolve():
    """问题 → 匹配密码本条目名（不返回明文）。"""
    data = request.get_json() or {}
    q = data.get('query') or data.get('q') or ''
    from nebula_secrets import resolve_for_query, list_items
    try:
        items = list_items(100)
        matches = resolve_for_query(q, items)
        return jsonify({'status': 'ok', 'query': q, 'matches': matches, 'count': len(matches)})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 503



@app.route('/v5/session-extract', methods=['POST'])
def v5_session_extract():
    """Mem0 型：会话抽取事实并分流写入星枢（密钥拦截）。"""
    data = request.get_json() or {}
    transcript = data.get('transcript') or data.get('notes') or data.get('session') or ''
    focus = data.get('focus') or data.get('query') or ''
    dry_run = _parse_bool(data.get('dry_run'), False)
    auto_write = _parse_bool(data.get('auto_write'), True)
    if dry_run:
        auto_write = False
    mm = get_engine()
    from nebula_session_extract import session_extract
    r = session_extract(
        mm,
        transcript=transcript,
        focus=focus,
        dry_run=dry_run,
        max_items=int(data.get('max_items', 8)),
        auto_write=auto_write,
    )
    return jsonify(r)


@app.route('/v5/layered-recall', methods=['GET', 'POST'])
def v5_layered_recall():
    """Letta 纪律：返回分层调用计划（极短）。"""
    if request.method == 'GET':
        focus = request.args.get('focus') or request.args.get('q') or ''
    else:
        data = request.get_json() or {}
        focus = data.get('focus') or data.get('query') or data.get('q') or ''
    from nebula_session_extract import layered_recall_plan
    return jsonify(layered_recall_plan(focus))



@app.route('/v5/promote-draft', methods=['POST'])
def v5_promote_draft():
    """3.7 起草笔记晋升草稿（不自动写 vault 文件）。"""
    data = request.get_json() or {}
    mm = get_engine()
    from nebula_v5 import promote_draft
    r = promote_draft(
        mm,
        evidence_query=data.get('query') or data.get('evidence_query') or '',
        session_notes=data.get('notes') or data.get('session_notes') or '',
    )
    return jsonify({'status': 'ok', **r})



# ==================== Nebula v5 Ultimate ====================

@app.route('/v5/bootstrap', methods=['POST', 'GET'])
def v5_bootstrap():
    """会话启动 L0 注入包。带 result_cache，重复 focus 秒回。"""
    t0 = time.time()
    if request.method == 'GET':
        focus = request.args.get('focus') or request.args.get('q') or ''
        budget = request.args.get('budget_chars', 2400, type=int)
        no_cache = request.args.get('no_cache', 'false').lower() == 'true'
    else:
        data = request.get_json() or {}
        focus = data.get('focus') or data.get('query') or data.get('q') or ''
        budget = int(data.get('budget_chars', 2400))
        no_cache = _parse_bool(data.get('no_cache'), False)

    rc_key = result_cache.make_key("bootstrap_v5", {"focus": focus or "", "budget": budget})
    if not no_cache:
        hit = result_cache.get(rc_key)
        if hit is not None:
            out = dict(hit)
            out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            out["result_cache_hit"] = True
            out["result_cache"] = result_cache.stats()
            return jsonify(out)

    mm = get_engine()
    from nebula_v5 import bootstrap_context
    resp = bootstrap_context(mm, focus_query=focus, budget_chars=budget)
    resp = dict(resp)
    resp["result_cache_hit"] = False
    resp["result_cache"] = result_cache.stats()
    if not no_cache:
        try:
            result_cache.put(rc_key, resp)
        except Exception:
            pass
    return jsonify(resp)


@app.route('/v5/answer', methods=['POST'])
def v5_answer():
    """终极问答：返回 contract + pack + composed。"""
    data = request.get_json() or {}
    query = data.get('query') or ''
    if not query:
        return jsonify({'error': 'query required'}), 400
    mm = get_engine()
    from nebula_v5 import ultimate_ask
    r = ultimate_ask(
        mm, query=query,
        top_k=int(data.get('top_k', 5)),
        hops=int(data.get('hops', 2)),
        llm_deep=data.get('llm_deep', 'auto'),
        llm_answer=_parse_bool(data.get('llm_answer'), False),
        use_graph=_parse_bool(data.get('use_graph'), True),
    )
    return jsonify(r)


@app.route('/v5/lifecycle', methods=['POST'])
def v5_lifecycle():
    data = request.get_json(force=True, silent=True) or {}
    mm = get_engine()
    from nebula_v5 import lifecycle_run
    r = lifecycle_run(
        mm,
        demote_days=int(data.get('demote_days', 21)),
        demote_max=int(data.get('demote_max', 300)),
        digest=_parse_bool(data.get('digest'), True),
    )
    return jsonify({'status': 'ok', **r})


@app.route('/v5/health', methods=['GET'])
def v5_health():
    mm = get_engine()
    from nebula_v5 import health_report
    return jsonify(health_report(mm))



# ==================== Nebula v4 关系 / 进化 ====================

@app.route('/memory/<int:memory_id>/related', methods=['GET'])
def memory_related(memory_id):
    mm = get_engine()
    from nebula_v4 import related_ids
    limit = request.args.get('limit', 8, type=int)
    return jsonify({'status': 'ok', 'id': memory_id, 'related': related_ids(mm.conn, memory_id, limit=limit)})


@app.route('/memory/supersede', methods=['POST'])
def memory_supersede():
    data = request.get_json() or {}
    old_id = data.get('old_id') or data.get('id')
    new_id = data.get('new_id')
    note = data.get('note') or ''
    if not old_id:
        return jsonify({'error': 'old_id required'}), 400
    mm = get_engine()
    from nebula_v4 import supersede_memory
    r = supersede_memory(mm.conn, int(old_id), int(new_id) if new_id else None, note=note)
    try:
        mm._invalidate_emb_cache()
    except Exception:
        pass
    return jsonify({'status': 'ok', **r})


@app.route('/v4/migrate', methods=['POST'])
def v4_migrate():
    data = request.get_json(force=True, silent=True) or {}
    mm = get_engine()
    from nebula_v4 import migrate_schema, backfill_trust, rebuild_links
    m = migrate_schema(mm.conn)
    n = backfill_trust(mm.conn, limit=int(data.get('limit', 0) or 0))
    links = rebuild_links(mm.conn) if data.get('rebuild_links', True) else {}
    return jsonify({'status': 'ok', 'migrate': m, 'trust_updated': n, 'links': links})


@app.route('/v4/reflect', methods=['POST'])
def v4_reflect():
    data = request.get_json() or {}
    query = data.get('query') or ''
    if not query:
        return jsonify({'error': 'query required'}), 400
    mm = get_engine()
    from nebula_v4 import reflect_ask
    r = reflect_ask(
        mm, query=query,
        top_k=int(data.get('top_k', 5)),
        max_chars=int(data.get('max_chars', 280)),
        hops=int(data.get('hops', 2)),
        use_graph=_parse_bool(data.get('use_graph'), True),
        category=data.get('category'),
    )
    r['version'] = 'v4.0'
    return jsonify(r)



# ==================== MCP Server [e10] ==========================

from mcp.server.fastmcp import FastMCP

mcp_server = FastMCP(name='nebula-memory')


@mcp_server.tool()
def search_memories(query: str, top_k: int = 5, category: str = None):
    """语义检索（pack 省 token）。优先 ask_memories。"""
    mm = get_engine()
    from vector_memory import pack_results
    raw = mm.search(query, top_k=top_k * 3, category=category, use_hybrid=True)
    packed = pack_results(
        raw, query=query, top_k=top_k, max_chars=280, per_source=1,
        drop_superseded=True, drop_hearsay_if_canon=True, compact=True,
    )
    return {'status': 'ok', 'results': packed, 'count': len(packed), 'compact': True}


@mcp_server.tool()
def ask_memories(query: str, top_k: int = 5):
    """省 token 终极问答（v5 ultimate）。Agent 优先调用。"""
    mm = get_engine()
    from nebula_v5 import ultimate_ask
    return ultimate_ask(mm, query=query, top_k=top_k, hops=2, use_graph=True, llm_deep='auto', llm_answer=False)


@mcp_server.tool()
def session_extract_memories(transcript: str, focus: str = "", dry_run: bool = False):
    """Mem0型：从会话文本抽取记忆并写入星枢（自动拦密钥）。"""
    mm = get_engine()
    from nebula_session_extract import session_extract
    return session_extract(mm, transcript=transcript, focus=focus, dry_run=dry_run, auto_write=not dry_run)


@mcp_server.tool()
def layered_recall(focus: str = ""):
    """Letta纪律：返回记忆分层调用步骤。"""
    from nebula_session_extract import layered_recall_plan
    return layered_recall_plan(focus)


@mcp_server.tool()
def bootstrap_memories(focus_query: str = "", budget_chars: int = 2400):
    """会话启动记忆注入包（L0）。"""
    mm = get_engine()
    from nebula_v5 import bootstrap_context
    return bootstrap_context(mm, focus_query=focus_query, budget_chars=budget_chars)


@mcp_server.tool()
def related_memories(memory_id: int, limit: int = 8):
    """记忆关系邻居。"""
    mm = get_engine()
    from nebula_v4 import related_ids
    return {'status': 'ok', 'id': memory_id, 'related': related_ids(mm.conn, memory_id, limit=limit)}


@mcp_server.tool()
def supersede_memory_tool(old_id: int, new_id: int = None, note: str = ''):
    """标记旧记忆 superseded。"""
    mm = get_engine()
    from nebula_v4 import supersede_memory
    return supersede_memory(mm.conn, old_id, new_id, note=note)


@mcp_server.tool()
def add_memory_tool(content: str, importance: float = 0.5, category: str = 'fact'):
    """Add a new memory to the system."""
    mm = get_engine()
    result = mm.add(content=content, source='mcp', category=category, importance=importance)
    return {'status': 'ok', **result}


@mcp_server.tool()
def get_memory_stats():
    """Get memory system statistics."""
    mm = get_engine()
    total = mm.conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
    categories = [r[0] for r in mm.conn.execute('SELECT DISTINCT category FROM memories WHERE category IS NOT NULL').fetchall()]
    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    return {'status': 'ok', 'total_memories': total, 'categories': categories, 'db_size_bytes': db_size}


@mcp_server.tool()
def get_tags_cloud(limit: int = 20):
    """Get popular tags from the memory system."""
    mm = get_engine()
    rows = mm.conn.execute('SELECT name, usage_count FROM tags ORDER BY usage_count DESC LIMIT ?', (limit,)).fetchall()
    return [{'name': r[0], 'count': r[1]} for r in rows]


@mcp_server.tool()
def delete_memory_by_id(memory_id: int):
    """Delete a memory by its ID."""
    mm = get_engine()
    result = mm.delete(memory_id)
    return {'status': 'ok', 'deleted': result, 'memory_id': memory_id}


@mcp_server.tool()
def search_function(func_name: str, top_k: int = 5):
    """[e10] Search for a function by name in the memory system."""
    mm = get_engine()
    results = mm.search_function(func_name, top_k=top_k)
    return {'status': 'ok', 'results': results, 'count': len(results)}


@mcp_server.tool()
def search_class(class_name: str, top_k: int = 5):
    """[e10] Search for a class by name in the memory system."""
    mm = get_engine()
    results = mm.search_class(class_name, top_k=top_k)
    return {'status': 'ok', 'results': results, 'count': len(results)}


@mcp_server.tool()
def compress_memories(date: str = None, days_ago: int = 30):
    """Compress old memories into summaries. Specify a date or days_ago."""
    mm = get_engine()
    compressor = MemoryCompressor(mm)
    if date:
        summary = compressor.compress_date(date)
        return {'status': 'ok', 'date': date, 'summary': summary}
    else:
        results = compressor.compress_range(days_ago=days_ago)
        return {'status': 'ok', 'compressed_dates': len(results), 'results': results}


# --- MCP JSON-RPC ---

@app.route('/mcp', methods=['POST'])
def mcp_endpoint():
    """MCP JSON-RPC endpoint for tool calls."""
    import json as json_mod
    import inspect
    import asyncio

    data = request.get_json(force=True)
    method = data.get('method', '')
    request_id = data.get('id')
    params = data.get('params', {})

    try:
        if method == 'initialize':
            return jsonify({
                'jsonrpc': '2.0', 'id': request_id,
                'result': {
                    'protocolVersion': '2024-11-05',
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': 'nebula-memory', 'version': '5.0.0'},
                },
            })
        elif method == 'tools/list':
            tools = []
            for name, tool in mcp_server._tool_manager._tools.items():
                tools.append({
                    'name': name, 'description': tool.description or '',
                    'inputSchema': tool.parameters,
                })
            return jsonify({'jsonrpc': '2.0', 'id': request_id, 'result': {'tools': tools}})
        elif method == 'tools/call':
            tool_name = params.get('name')
            arguments = params.get('arguments', {})
            if tool_name not in mcp_server._tool_manager._tools:
                return jsonify({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': f'Tool not found: {tool_name}'}})
            tool = mcp_server._tool_manager._tools[tool_name]
            fn = tool.fn
            if inspect.iscoroutinefunction(fn):
                result = asyncio.run(fn(**arguments))
            else:
                result = fn(**arguments)
            return jsonify({'jsonrpc': '2.0', 'id': request_id, 'result': {'content': [{'type': 'text', 'text': json_mod.dumps(result, ensure_ascii=False)}]}})
        elif method == 'notifications/initialized':
            return jsonify({'jsonrpc': '2.0'})
        else:
            return jsonify({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': f'Method not found: {method}'}})
    except Exception as e:
        return jsonify({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32603, 'message': str(e)}}), 500


if __name__ == '__main__':
    print('星枢 %s  %s' % (PRODUCT, RELEASE))
    print('  bind    %s:%s' % (NEBULA_BIND, NEBULA_PORT))
    print('  db      %s' % DB_PATH)
    print('  embed   %s' % EMBED_PROVIDER)
    print('  rerank  %s' % RERANK_MODEL)
    print('  docs    http://127.0.0.1:%s/help' % NEBULA_PORT)

    def _startup_warmup():
        time.sleep(1)
        try:
            mm = get_engine()
            try:
                t1 = time.time()
                mat, ids = mm._load_emb_matrix()
                logger.info("emb matrix warmup %s rows %.0fms", len(ids), (time.time() - t1) * 1000)
            except Exception as e:
                logger.warning("emb matrix warmup fail: %s", e)
            if mm.embedder:
                warmup_cache(mm.embedder, db_path=DB_PATH)
            try:
                from nebula_v5 import bootstrap_context
                for fq in ("session-start", "虎虎现行架构 星枢用法 工作流"):
                    for budget in (1200, 2400):
                        t1 = time.time()
                        resp = bootstrap_context(mm, focus_query=fq, budget_chars=budget)
                        key = result_cache.make_key("bootstrap_v5", {"focus": fq, "budget": budget})
                        result_cache.put(key, dict(resp))
                        logger.info(
                            "bootstrap warmup focus=%s budget=%s %.0fms",
                            fq, budget, (time.time() - t1) * 1000,
                        )
            except Exception as e:
                logger.warning(f'Bootstrap warmup failed (non-fatal): {e}')
            try:
                from nebula_v5 import health_report
                t1 = time.time()
                health_report(mm)
                logger.info("health warmup %.0fms", (time.time() - t1) * 1000)
            except Exception as e:
                logger.warning(f'Health warmup failed (non-fatal): {e}')
        except Exception as e:
            logger.error(f'Startup warmup failed: {e}')

    threading.Thread(target=_startup_warmup, daemon=True).start()
    app.run(host=NEBULA_BIND, port=NEBULA_PORT, debug=False, threaded=True)
