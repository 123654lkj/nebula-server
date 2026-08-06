#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Obsidian/黑曜石笔记 → 星枢 增量同步（v1.2：核心白名单 + 体积帽，不 bulk 灌库）

环境变量:
  NEBULA_URL / VAULT_ROOT / VAULT_NEBULA_STATE / VAULT_NEBULA_LOG
  VAULT_SYNC_MAX_BYTES / NEBULA_EXTRA_SYNC_FILES (os.pathsep 分隔的额外文件)
"""
from __future__ import annotations
import hashlib, json, os, re, sys, time, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

NEBULA = os.environ.get("NEBULA_URL", "http://127.0.0.1:26670")
VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", str(Path(__file__).resolve().parents[1] / "vault" / "notes")))
STATE_FILE = Path(os.environ.get("VAULT_NEBULA_STATE", str(Path(__file__).resolve().parents[1] / "data" / "vault-sync-state.json")))
LOG_FILE = Path(os.environ.get("VAULT_NEBULA_LOG", str(Path(__file__).resolve().parents[1] / "data" / "vault-sync.log")))

# 默认排除大体量；白名单可穿透排除目录
EXCLUDE_DIR_PARTS = ("/团子学习/", "/_归档/", "/.obsidian/", "/.trash/")
EXCLUDE_NAME_SUBSTR = ("gmail-最新邮件", "主密码哈希")
# 团子学习 / 记忆方法：只进「可执行核心」，禁止整库 2900+ 篇
INCLUDE_REL_GLOBS = (
    "团子学习/_WORKBOOK-40.md",
    "团子学习/_DASHBOARD.md",
    "团子学习/_METRICS.md",
    "团子学习/_LIBRARY-MAP.md",
    "团子学习/_PLAYABLE-摘录.md",
    "团子学习/_CANON-DELTA-*.md",
    "团子学习/记忆系统/_merged-*.md",
    "团子学习/记忆系统/Agent-记忆工作手册*.md",
    # v1.3：记忆系统核心专题（抬 vault_chunks，仍非整库）
    "团子学习/记忆系统/TencentDB*.md",
    "团子学习/记忆系统/*Memory*.md",
    "团子学习/记忆系统/*memory*.md",
    "团子学习/记忆系统/*Mem*.md",
    "团子学习/记忆系统/*RAG*.md",
    "团子学习/记忆系统/*rag*.md",
    "团子学习/记忆系统/*optmem*",
    "团子学习/记忆系统/*engram*",
    "团子学习/记忆系统/*Engram*",
    "团子学习/记忆系统/*Cortex*",
    "团子学习/记忆系统/*cognee*",
    "团子学习/记忆系统/*Cognee*",
    "团子学习/记忆系统/*jacobian*",
    "团子学习/记忆系统/*a-third-brain*",
    "团子学习/编码方法论/Ponytail*.md",
    "团子学习/本地沉淀Skill/*.md",
    "团子学习/趋势洞察/*趋势汇总*.md",
    "00-元信息/*.md",
    "01-用户画像/*.md",
    "02-密码与安全/*.md",
    "03-API与模型/*.md",
    "04-项目笔记/*.md",
    "05-临时记录/*.md",
    "06-Agent会话提炼/*.md",
    "HOME.md",
    "PROJECT.md",
)
MAX_FILE_BYTES = int(os.environ.get("VAULT_SYNC_MAX_BYTES", str(120_000)))  # 单文件体积帽
EXTRA_FILES = [
    Path(p) for p in os.environ.get("NEBULA_EXTRA_SYNC_FILES", "").split(os.pathsep) if p.strip()
]
CHUNK_SIZE, CHUNK_OVERLAP = 900, 100
SOURCE_PREFIX = "vault:"

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def http_json(method: str, path: str, body: Optional[dict] = None, timeout: int = 120) -> dict:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(NEBULA + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        # content_hash UNIQUE → 视为已存在
        if e.code == 500 and ("UNIQUE" in err_body or "content_hash" in err_body or "IntegrityError" in err_body):
            return {"status": "ok", "is_duplicate": True, "id": None, "duplicate_reason": "unique_content_hash"}
        raise RuntimeError(f"HTTP {e.code}: {err_body[:300]}") from e

def health_ok() -> bool:
    try:
        return http_json("GET", "/health", timeout=5).get("status") == "ok"
    except Exception as e:
        log(f"health fail: {e}")
        return False

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"files": {}}

def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)

def file_fingerprint(path: Path) -> dict:
    st = path.stat()
    return {"mtime": st.st_mtime, "size": st.st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

def _rel_posix(path: Path) -> Optional[str]:
    try:
        if path.is_relative_to(VAULT_ROOT):
            return path.relative_to(VAULT_ROOT).as_posix()
    except Exception:
        pass
    return None


def _glob_match(rel: str, pattern: str) -> bool:
    """简易 glob：* 不跨 /，** 不用。"""
    import fnmatch
    return fnmatch.fnmatch(rel, pattern)


def is_whitelisted(path: Path) -> bool:
    rel = _rel_posix(path)
    if not rel:
        return False
    return any(_glob_match(rel, g) for g in INCLUDE_REL_GLOBS)


def should_skip_path(path: Path) -> bool:
    s = str(path).replace("\\", "/")
    if path.suffix.lower() != ".md":
        return True
    if any(sub in path.name for sub in EXCLUDE_NAME_SUBSTR):
        return True
    # 白名单优先（可穿透 团子学习 排除）
    if is_whitelisted(path):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                log(f"skip oversize {path} size={path.stat().st_size}>{MAX_FILE_BYTES}")
                return True
            if path.stat().st_size == 0:
                return True
        except Exception:
            return True
        return False
    if any(p in s for p in EXCLUDE_DIR_PARTS):
        return True
    # 非排除目录：仍同步全部 md，但受体积帽
    try:
        if path.stat().st_size > MAX_FILE_BYTES or path.stat().st_size == 0:
            return True
    except Exception:
        return True
    return False

def collect_vault_files() -> List[Path]:
    files: List[Path] = []
    if VAULT_ROOT.exists():
        for p in VAULT_ROOT.rglob("*.md"):
            if not should_skip_path(p):
                files.append(p)
    for p in EXTRA_FILES:
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            files.append(p)
    return sorted(set(files), key=lambda x: str(x))

def vault_source_key(path: Path) -> str:
    try:
        if path.is_relative_to(VAULT_ROOT):
            return f"{SOURCE_PREFIX}notes/{path.relative_to(VAULT_ROOT).as_posix()}"
    except Exception:
        pass
    # 额外文件：用相对名，避免绑定某台机器路径
    return f"{SOURCE_PREFIX}extra/{path.name}"

def category_for(path: Path) -> str:
    s = str(path)
    if any(x in s for x in ("GATEWAY_LOCK", "NETWORK-FROZEN", "phantun", "网关")):
        return "infrastructure"
    if "01-用户画像" in s or "USER" in path.name or "MEMORY" in path.name:
        return "identity"
    if "02-密码" in s or "Vaultwarden" in s:
        return "security"
    if "03-API" in s:
        return "infrastructure"
    if "04-项目笔记" in s:
        return "project"
    if "05-临时" in s:
        return "fact"
    if "团子学习" in s or "记忆系统" in s or "WORKBOOK" in path.name or "CANON" in path.name:
        return "lesson"
    if path.name in ("HOME.md", "PROJECT.md"):
        return "infrastructure"
    if "infra/" in s or "gateway/" in s:
        return "infrastructure"
    return "fact"

def importance_for(path: Path) -> float:
    s, name = str(path), path.name
    if name in ("HOME.md", "PROJECT.md") or "GATEWAY_LOCK" in s:
        return 0.95
    if name in ("_WORKBOOK-40.md",) or "Agent-记忆工作手册" in name:
        return 0.93
    if "MEMORY" in name or "用户画像" in s or "AGENT操作手册" in name:
        return 0.9
    if name.startswith("_merged-") or "记忆系统" in s:
        return 0.88
    if "04-项目笔记" in s or "phantun-debug" in s or "dead-cleanup" in s:
        return 0.85
    if "05-临时" in s:
        return 0.55
    if "00-元信息" in s:
        return 0.7
    return 0.75

def strip_frontmatter(text: str) -> Tuple[str, dict]:
    meta = {}
    if text.startswith("---"):
        m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.S)
        if m:
            fm, body = m.group(1), m.group(2)
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip("\"'")
            return body, meta
    return text, meta

def extract_title(path: Path, body: str, meta: dict) -> str:
    if meta.get("title"):
        return meta["title"]
    for line in body.splitlines():
        if line.strip().startswith("# "):
            return line.strip()[2:].strip()
    return path.stem

def _hard_split(text: str, chunk_size: int, overlap: int) -> List[str]:
    if len(text) <= chunk_size:
        return [text]
    sents = re.split(r"(?<=[。！？.!?\n；;])", text)
    out, cur, n = [], [], 0
    for s in sents:
        if not s:
            continue
        if n + len(s) > chunk_size and cur:
            out.append("".join(cur).strip())
            keep, kn = [], 0
            for x in reversed(cur):
                if kn + len(x) > overlap:
                    break
                keep.insert(0, x)
                kn += len(x)
            cur, n = keep, sum(len(x) for x in cur)
        cur.append(s)
        n += len(s)
    if cur:
        out.append("".join(cur).strip())
    return [c for c in out if c]

def split_chunks(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    parts = re.split(r"(?=^#{1,3} )", text, flags=re.M)
    chunks, buf = [], ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(buf) + len(part) + 1 <= chunk_size:
            buf = (buf + "\n\n" + part).strip() if buf else part
        else:
            if buf:
                chunks.extend(_hard_split(buf, chunk_size, overlap))
            if len(part) <= chunk_size:
                buf = part
            else:
                chunks.extend(_hard_split(part, chunk_size, overlap))
                buf = ""
    if buf:
        chunks.extend(_hard_split(buf, chunk_size, overlap))
    return [c for c in chunks if c.strip()]

def wrap_chunk(path: Path, source_key: str, title: str, idx: int, total: int, body: str) -> str:
    rel = source_key.replace(SOURCE_PREFIX, "")
    return (
        f"[虎虎笔记] path={rel} source={source_key} title={title} chunk={idx+1}/{total}\n"
        f"回读命令: rxt read --host huhu \"{path.as_posix()}\"\n"
        f"---\n{body.strip()}"
    )

def delete_source(source_key: str) -> int:
    import sqlite3
    db = Path("/opt/nebula/data/memory_vectors.db")
    if not db.exists():
        return 0
    con = sqlite3.connect(str(db), timeout=30)
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM memories WHERE source_file = ?", (source_key,))]
        deleted = 0
        for mid in ids:
            try:
                http_json("DELETE", f"/memory/{mid}", timeout=30)
                deleted += 1
            except Exception:
                con.execute("DELETE FROM memories WHERE id = ?", (mid,))
                deleted += 1
        con.commit()
        return deleted
    finally:
        con.close()

def add_memory(content: str, source_key: str, category: str, importance: float) -> dict:
    # 不传 tags，避免 tag 相关锁竞争；source 已是 vault: 前缀
    return http_json("POST", "/memory/add", {
        "content": content,
        "source": source_key,
        "category": category,
        "importance": importance,
    }, timeout=90)

def sync_file(path: Path, state: dict, force: bool = False) -> dict:
    source_key = vault_source_key(path)
    fp = file_fingerprint(path)
    prev = state.get("files", {}).get(source_key)
    if not force and prev and prev.get("sha256") == fp["sha256"] and prev.get("size") == fp["size"]:
        return {"path": str(path), "status": "skip", "source": source_key}

    text = path.read_text(encoding="utf-8", errors="replace")
    body, meta = strip_frontmatter(text)
    title = extract_title(path, body, meta)
    cat, imp = category_for(path), importance_for(path)
    raw_chunks = split_chunks(body) or [body[:CHUNK_SIZE] or title]

    deleted = delete_source(source_key) if (prev or force) else 0
    added, ids = 0, []
    total = len(raw_chunks)
    for i, ch in enumerate(raw_chunks):
        content = wrap_chunk(path, source_key, title, i, total, ch)
        try:
            r = add_memory(content, source_key, cat, imp)
            if not r.get("is_duplicate"):
                added += 1
            if r.get("id"):
                ids.append(int(r["id"]))
        except Exception as e:
            log(f"  add fail {source_key}#{i}: {e}")

    state.setdefault("files", {})[source_key] = {
        **fp, "path": str(path), "title": title, "category": cat,
        "importance": imp, "chunks": total, "ids": ids[-30:], "synced_at": time.time(),
    }
    return {"path": str(path), "status": "synced", "source": source_key,
            "deleted": deleted, "added": added, "chunks": total, "title": title}

def prune_missing(state: dict, present_keys: set) -> int:
    removed = 0
    files = state.get("files", {})
    for key in list(files.keys()):
        if key not in present_keys:
            n = delete_source(key)
            del files[key]
            removed += 1
            log(f"pruned {key} chunks={n}")
    return removed

def rebuild_state_from_db(state: dict, files: List[Path]) -> None:
    """用磁盘指纹回填 state，避免重复删建。"""
    by_key = {vault_source_key(p): p for p in files}
    import sqlite3
    con = sqlite3.connect("/opt/nebula/data/memory_vectors.db")
    rows = con.execute(
        "SELECT source_file, COUNT(*) FROM memories WHERE source_file LIKE 'vault:%' GROUP BY source_file"
    ).fetchall()
    con.close()
    for key, cnt in rows:
        p = by_key.get(key)
        if not p or not p.exists():
            continue
        fp = file_fingerprint(p)
        body, meta = strip_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        state.setdefault("files", {})[key] = {
            **fp, "path": str(p), "title": extract_title(p, body, meta),
            "category": category_for(p), "importance": importance_for(p),
            "chunks": cnt, "ids": [], "synced_at": time.time(), "from_db": True,
        }

def main(argv: List[str]) -> int:
    force = "--force" in argv
    dry = "--dry-run" in argv
    rebuild = "--rebuild-state" in argv
    only = next((a.split("=",1)[1] for a in argv if a.startswith("--only=")), None)

    log("=== vault → 星枢 sync start ===")
    if not health_ok():
        log("星枢不可用"); return 1

    files = collect_vault_files()
    if only:
        files = [p for p in files if only in str(p)]
    log(f"candidates={len(files)} force={force} dry={dry}")

    if dry:
        for p in files:
            print(vault_source_key(p), category_for(p), importance_for(p), p)
        return 0

    state = load_state()
    if rebuild or not state.get("files"):
        rebuild_state_from_db(state, files)
        save_state(state)
        log(f"state rebuilt files={len(state.get('files', {}))}")

    stats = {"skip": 0, "synced": 0, "added": 0, "deleted": 0, "errors": 0}
    present = set()
    for p in files:
        key = vault_source_key(p)
        present.add(key)
        try:
            r = sync_file(p, state, force=force)
            if r["status"] == "skip":
                stats["skip"] += 1
            else:
                stats["synced"] += 1
                stats["added"] += r.get("added", 0)
                stats["deleted"] += r.get("deleted", 0)
                log(f"SYNC {r.get('title')} +{r.get('added')} -{r.get('deleted')} chunks={r.get('chunks')} {r['source']}")
            save_state(state)
        except Exception as e:
            stats["errors"] += 1
            log(f"ERR {p}: {e}")

    pruned = prune_missing(state, present)
    state["last_run"] = {"ts": time.time(), "stats": stats, "pruned": pruned, "files": len(files)}
    save_state(state)
    log(f"=== done {stats} pruned={pruned} ===")
    return 0 if stats["errors"] == 0 else 2

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))