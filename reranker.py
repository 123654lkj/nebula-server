#!/usr/bin/env python3
"""
Cross-Encoder Reranker v3 (evolved)
====================================
Model: BAAI/bge-reranker-v2-m3
Changes from v2:
  [e3] Preload support: preload() method for background warming
  Simplified: 3-layer cache -> 2-layer (LRU + disk). Score cache removed (redundant for personal use).
  Cleaner error handling with logging.
"""

import os
import sys
import hashlib
import pickle
import time
import logging
from typing import List, Optional, Tuple, Dict
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("nebula.reranker")

CACHE_DIR = os.path.expanduser("~/.cache/modelscope/hub/models/BAAI/bge-reranker-v2-m3")
DISK_CACHE_DIR = os.path.join(CACHE_DIR, "cache_disk")
DISK_TTL_SECONDS = 7 * 24 * 3600
DISK_MAX_FILES = 500


class CrossEncoderReranker:
    """
    Cross-Encoder reranker with:
    - Lazy loading (first call loads model)
    - GPU auto / CPU fallback
    - FP16 on CUDA
    - 2-layer cache: LRU (10min TTL) + Disk (7d TTL)
    - Batch inference
    - [e3] preload() for background warming
    """

    _instance = None

    def __init__(self, device: str = "auto", cache_size: int = 100, dtype: str = "auto"):
        self._device = device
        self._dtype = dtype
        self._model = None
        self._tokenizer = None
        self._loaded = False
        self.cache_size = cache_size
        self._cache: OrderedDict = OrderedDict()  # (query_hash, cand_hash) -> (scores, timestamp)
        os.makedirs(DISK_CACHE_DIR, exist_ok=True)

    @property
    def device(self) -> str:
        return self._device

    @property
    def dtype(self) -> str:
        return self._dtype

    @classmethod
    def get_instance(cls, device: str = "auto", dtype: str = "auto") -> "CrossEncoderReranker":
        """Singleton pattern."""
        if cls._instance is None or cls._instance._device != device or cls._instance._dtype != dtype:
            if cls._instance is not None and cls._instance._model is not None:
                try:
                    del cls._instance._model
                    del cls._instance._tokenizer
                    cls._instance._model = None
                    cls._instance._tokenizer = None
                    cls._instance._loaded = False
                    import gc; gc.collect()
                    try:
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except ImportError:
                        pass
                except Exception:
                    pass
            cls._instance = cls(device=device, dtype=dtype)
        return cls._instance

    def _load_model(self) -> None:
        """Load model lazily (first call only)."""
        if self._loaded:
            return

        try:
            try:
                import torch
                self._torch = torch
            except ImportError:
                torch = None

            if self._device == "auto":
                if torch and torch.cuda.is_available():
                    self._device = "cuda"
                else:
                    self._device = "cpu"

            if self._dtype == "auto":
                if self._device == "cuda":
                    self._dtype = torch.float16
                    logger.info("CrossEncoderReranker: FP16 (CUDA)")
                else:
                    self._dtype = torch.float32
                    logger.info("CrossEncoderReranker: FP32 (CPU)")
            else:
                self._dtype = torch.float16 if self._dtype == "float16" else torch.float32

            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            if not os.path.isdir(CACHE_DIR):
                raise FileNotFoundError(f"Model path not found: {CACHE_DIR}")

            self._tokenizer = AutoTokenizer.from_pretrained(CACHE_DIR)
            self._model = AutoModelForSequenceClassification.from_pretrained(
                CACHE_DIR, torch_dtype=self._dtype,
            )
            self._model.eval()
            self._model.to(self._device)
            self._loaded = True
            logger.info(f"CrossEncoderReranker loaded (device={self._device}, dtype={self._dtype})")

        except ImportError as e:
            raise RuntimeError(f"Missing dependency (transformers/torch): {e}")
        except Exception as e:
            raise RuntimeError(f"Model load failed: {e}")

    def preload(self) -> bool:
        """[e3] Explicitly preload model. Returns True on success."""
        try:
            self._load_model()
            return self._loaded
        except Exception as e:
            logger.warning(f"Reranker preload failed: {e}")
            return False

    def _hash_query(self, query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]

    def _hash_candidates(self, candidates: List[str]) -> str:
        combined = "\n".join(candidates)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]

    def _cache_get(self, q_hash: str, c_hash: str) -> Optional[List[float]]:
        """LRU cache read (TTL 10min)."""
        key = (q_hash, c_hash)
        if key in self._cache:
            self._cache.move_to_end(key)
            scores, ts = self._cache[key]
            if time.time() - ts < 600:
                return scores
            else:
                del self._cache[key]
        return None

    def _cache_put(self, q_hash: str, c_hash: str, scores: List[float]) -> None:
        """LRU cache write."""
        key = (q_hash, c_hash)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (scores, time.time())
        if len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def _save_to_disk(self, q_hash: str, c_hash: str, scores: List[float]) -> None:
        """Save to disk cache."""
        try:
            os.makedirs(DISK_CACHE_DIR, exist_ok=True)
            try:
                files = [(os.path.join(DISK_CACHE_DIR, f), os.path.getmtime(os.path.join(DISK_CACHE_DIR, f)))
                         for f in os.listdir(DISK_CACHE_DIR) if f.endswith('.pkl')]
                if len(files) >= DISK_MAX_FILES:
                    files.sort(key=lambda x: x[1])
                    for fpath, _ in files[:len(files) - DISK_MAX_FILES + 10]:
                        try:
                            os.remove(fpath)
                        except OSError:
                            pass
            except Exception:
                pass
            path = os.path.join(DISK_CACHE_DIR, f"{q_hash}_{c_hash}.pkl")
            with open(path, "wb") as f:
                pickle.dump(scores, f)
        except Exception as e:
            logger.debug(f"Disk cache save failed: {e}")

    def _load_from_disk(self, q_hash: str, c_hash: str) -> Optional[List[float]]:
        """Load from disk cache with TTL check."""
        try:
            path = os.path.join(DISK_CACHE_DIR, f"{q_hash}_{c_hash}.pkl")
            if os.path.exists(path):
                file_age = time.time() - os.path.getmtime(path)
                if file_age > DISK_TTL_SECONDS:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    return None
                with open(path, "rb") as f:
                    return pickle.load(f)
        except Exception as e:
            logger.debug(f"Disk cache load failed: {e}")
        return None

    def rerank(self, query: str, candidates: List[str], top_k: Optional[int] = None, batch_size: int = 32) -> List[float]:
        """Batch rerank prediction."""
        self._load_model()

        q_hash = self._hash_query(query)
        c_hash = self._hash_candidates(candidates)

        # Layer 1: LRU cache
        cached = self._cache_get(q_hash, c_hash)
        if cached is not None:
            return cached[:top_k] if top_k else cached

        # Layer 2: Disk cache
        disk_scores = self._load_from_disk(q_hash, c_hash)
        if disk_scores is not None:
            self._cache_put(q_hash, c_hash, disk_scores)
            return disk_scores[:top_k] if top_k else disk_scores

        # Inference
        pairs = [(query, c) for c in candidates]
        scores = []

        try:
            import torch

            for i in range(0, len(pairs), batch_size):
                batch_pairs = pairs[i:i + batch_size]
                inputs = self._tokenizer(
                    batch_pairs, padding=True, truncation=True,
                    max_length=512, return_tensors="pt",
                )
                inputs = {k: v.to(self._device) for k, v in inputs.items()}

                with torch.no_grad():
                    outputs = self._model(**inputs)
                    batch_scores = outputs.logits[:, 0].cpu().numpy().tolist()
                    scores.extend(batch_scores)

            # Store in both cache layers
            self._cache_put(q_hash, c_hash, scores)
            self._save_to_disk(q_hash, c_hash, scores)

            return scores[:top_k] if top_k else scores

        except Exception as e:
            logger.warning(f"Reranker inference failed: {e}")
            return [0.0] * len(candidates)

    def rerank_batch_queries(self, queries: List[str], candidates: List[str],
                             batch_size: int = 32, max_workers: int = 4) -> Dict[str, List[float]]:
        """Multi-query batch rerank (concurrent)."""
        results = {}

        def _rerank_single(query: str):
            return query, self.rerank(query, candidates, batch_size=batch_size)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_rerank_single, q) for q in queries]
            for future in as_completed(futures):
                query, scores = future.result()
                results[query] = scores
        return results

    def get_memory_usage(self) -> Dict[str, float]:
        try:
            import torch
            if self._device == "cuda" and torch.cuda.is_available():
                torch.cuda.synchronize()
                allocated = torch.cuda.memory_allocated() / 1024 / 1024
                reserved = torch.cuda.memory_reserved() / 1024 / 1024
                return {"allocated_mb": round(allocated, 2), "reserved_mb": round(reserved, 2)}
            else:
                return {"allocated_mb": 0.0, "reserved_mb": 0.0}
        except Exception as e:
            return {"error": str(e)}

    def unload(self) -> None:
        """Release model memory."""
        self._model = None
        self._tokenizer = None
        self._loaded = False
        self._cache.clear()
        logger.info("CrossEncoderReranker unloaded")

    @property
    def is_loaded(self) -> bool:
        return self._loaded


def get_reranker(device: str = "auto", dtype: str = "auto") -> Optional[CrossEncoderReranker]:
    """Lazy-load reranker (try/except, returns None on failure)."""
    try:
        return CrossEncoderReranker.get_instance(device=device, dtype=dtype)
    except (ImportError, RuntimeError, FileNotFoundError) as e:
        logger.warning(f"Reranker load failed (fallback None): {e}")
        return None


if __name__ == "__main__":
    print("=== CrossEncoderReranker v3 - Test ===")

    try:
        reranker = get_reranker(device="auto", dtype="auto")
        if reranker is None:
            print("Model not found or load failed (skip test)")
            sys.exit(0)

        query = "What is a vector database"
        candidates = [
            "The weather is nice today",
            "A vector database stores and retrieves high-dimensional vectors",
            "Python is a programming language",
        ]

        scores = reranker.rerank(query, candidates)
        print(f"Query: {query}")
        print(f"Candidates: {candidates}")
        print(f"Scores: {[f'{s:.3f}' for s in scores]}")

        batch_scores = reranker.rerank(query, candidates * 3, batch_size=2)
        print(f"Batch test (batch_size=2): {len(batch_scores)} scores")

        mem = reranker.get_memory_usage()
        print(f"Memory: {mem}")

        reranker.unload()
        print("Test complete")

    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
