#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星枢 × Vaultwarden 密钥桥（可选模块）
========================
此模块为可选增强：未安装 bw CLI / 未配置 Vaultwarden 时，
核心 REST 检索与记忆功能完全不受影响，仅 /secrets/* 端点返回错误。

铁律：
1. 密钥明文只进 Vaultwarden，永不写入向量库 / 笔记
2. 星枢只存引用：vaultwarden:item=<name>
3. 取密需显式 reveal=true，且默认不写日志正文

依赖（可选）：
- Bitwarden CLI (bw) + 已解锁 session（BW_SESSION_FILE 或 /tmp/bw-session-<user>）
- 或跳过：不启用任何 /secrets 端点即可
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nebula.secrets")

BW_BIN = os.environ.get("BW_BIN", "bw")
SESSION_CANDIDATES = [
    os.environ.get("BW_SESSION_FILE", ""),
    f"/tmp/bw-session-{os.environ.get('USER', 'root')}",
]
CA_CANDIDATES = [
    os.environ.get("NEBULA_CA_PATH", ""),
    "/etc/ssl/certs/",
]

# 常见密钥形态（写入拦截）
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai_like_sk"),
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"), "anthropic_key"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "github_pat"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws_access_key"),
    (re.compile(r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----"), "private_key_pem"),
    (re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]"), "kv_secret"),
    (re.compile(r"(?i)Bearer\s+[A-Za-z0-9\-_\.]{20,}"), "bearer_token"),
]


def _session_path() -> Optional[str]:
    for p in SESSION_CANDIDATES:
        if p and os.path.isfile(p) and os.path.getsize(p) > 10:
            return p
    return None


def _ca_path() -> Optional[str]:
    for p in CA_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _bw_env() -> Dict[str, str]:
    env = os.environ.copy()
    # 默认用当前用户 bw 数据目录（BITWARDENCLI_APPDATA_DIR 可覆盖）
    appdata = os.environ.get("BITWARDENCLI_APPDATA_DIR") or os.path.expanduser("~/.config/Bitwarden CLI")
    if os.path.isdir(appdata):
        env["BITWARDENCLI_APPDATA_DIR"] = appdata
    sp = _session_path()
    if sp:
        try:
            with open(sp, "r", encoding="utf-8") as f:
                env["BW_SESSION"] = f.read().strip()
        except Exception as e:
            logger.warning("read session fail: %s", e)
    ca = _ca_path()
    if ca:
        env["NODE_EXTRA_CA_CERTS"] = ca
    # 不在日志里打印 session
    return env


def bw_status() -> Dict[str, Any]:
    env = _bw_env()
    try:
        r = subprocess.run(
            [BW_BIN, "status"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        out = (r.stdout or "").strip()
        if not out:
            return {
                "ok": False,
                "status": "error",
                "detail": (r.stderr or "")[:200],
                "session_file": _session_path(),
            }
        data = json.loads(out)
        data["ok"] = data.get("status") == "unlocked"
        data["session_file"] = _session_path()
        data["server"] = data.get("serverUrl")
        return data
    except Exception as e:
        return {"ok": False, "status": "error", "detail": str(e), "session_file": _session_path()}


def _bw_json(args: List[str], timeout: int = 30) -> Any:
    env = _bw_env()
    if not env.get("BW_SESSION"):
        raise RuntimeError("BW_SESSION 不可用：请先 unlock（bw unlock --raw 存入 session 文件，或设置 BW_SESSION_FILE）")
    cmd = [BW_BIN, "--nointeraction"] + args
    if "--session" not in args:
        cmd += ["--session", env["BW_SESSION"]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[:300]
        raise RuntimeError(f"bw failed: {err}")
    out = (r.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def list_items(limit: int = 100) -> List[Dict[str, str]]:
    items = _bw_json(["list", "items"]) or []
    rows = []
    for it in items[:limit]:
        login = it.get("login") or {}
        rows.append(
            {
                "name": it.get("name") or "",
                "id": it.get("id") or "",
                "username": login.get("username") or "",
                "folderId": it.get("folderId") or "",
            }
        )
    return rows


def get_password(name: str) -> str:
    """取明文密码 — 调用方禁止写入向量/日志。"""
    env = _bw_env()
    r = subprocess.run(
        [BW_BIN, "--nointeraction", "get", "password", name, "--session", env["BW_SESSION"]],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "get failed")[:200])
    return (r.stdout or "").strip()


def get_username(name: str) -> str:
    env = _bw_env()
    r = subprocess.run(
        [BW_BIN, "--nointeraction", "get", "username", name, "--session", env["BW_SESSION"]],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "get-user failed")[:200])
    return (r.stdout or "").strip()


def create_login_item(name: str, username: str, password: str, notes: str = "") -> Dict[str, Any]:
    """创建 login 条目，返回 id/name（不含密码）。"""
    # 若已存在则拒绝覆盖
    for it in list_items(500):
        if it["name"] == name:
            raise RuntimeError(f"条目已存在: {name}（请换名或先 bw-ai rm）")
    payload = {
        "type": 1,
        "name": name,
        "notes": notes or "",
        "login": {"username": username or "", "password": password},
    }
    env = _bw_env()
    r = subprocess.run(
        [BW_BIN, "--nointeraction", "create", "item", "--session", env["BW_SESSION"]],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if r.returncode != 0:
        # bw create item 可能读 stdin 不同 — 用 encode
        r = subprocess.run(
            [BW_BIN, "--nointeraction", "create", "item", "--session", env["BW_SESSION"]],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "create failed")[:300])
    try:
        data = json.loads(r.stdout or "{}")
    except Exception:
        data = {"raw": (r.stdout or "")[:100]}
    return {"name": name, "id": data.get("id"), "username": username}


def pointer_content(name: str, purpose: str = "", username: str = "") -> str:
    purpose = purpose or "密钥/API"
    return (
        f"[vaultwarden指针] item={name}\n"
        f"用途: {purpose}\n"
        f"用户名: {username or '(见密码本)'}\n"
        f"取用: bw get {name}  或  POST /secrets/get {{\"name\":\"{name}\",\"reveal\":true}}\n"
        f"禁止: 把明文密钥写入笔记/星枢/prompt\n"
        f"更新: {time.strftime('%Y-%m-%d')}"
    )


def scan_secrets(text: str) -> List[str]:
    """返回命中的密钥模式标签。"""
    if not text:
        return []
    hits = []
    for pat, label in SECRET_PATTERNS:
        if pat.search(text):
            hits.append(label)
    return hits


def redact_secrets(text: str) -> Tuple[str, List[str]]:
    hits = scan_secrets(text)
    if not hits:
        return text, []
    out = text
    for pat, _ in SECRET_PATTERNS:
        out = pat.sub("[REDACTED_SECRET]", out)
    return out, hits


def register_pointer_memory(
    manager,
    name: str,
    purpose: str = "",
    username: str = "",
    importance: float = 0.85,
) -> Dict[str, Any]:
    content = pointer_content(name, purpose=purpose, username=username)
    # 去重：搜同名指针
    try:
        existing = manager.search(query=f"vaultwarden指针 item={name}", top_k=3, use_hybrid=True)
        for e in existing or []:
            if f"item={name}" in (e.get("content") or ""):
                return {
                    "id": e.get("id"),
                    "is_duplicate": True,
                    "name": name,
                    "pointer": True,
                }
    except Exception:
        pass
    r = manager.add(
        content=content,
        source="vaultwarden",
        category="credential",
        importance=importance,
        metadata={"trust": "canon", "secret_ref": name, "kind": "vaultwarden_pointer"},
    )
    return {**r, "name": name, "pointer": True}


def sync_catalog_to_memory(manager, limit: int = 80) -> Dict[str, Any]:
    """把密码本条目名同步为指针记忆（无任何明文）。"""
    items = list_items(limit=limit)
    added, skipped = 0, 0
    for it in items:
        name = it.get("name") or ""
        if not name:
            continue
        # 用途粗分
        purpose = "密钥"
        if name.startswith("api-") or "API" in name or "api" in name:
            purpose = "API Key"
        elif name.startswith("ssh-"):
            purpose = "SSH 凭据"
        elif name.startswith("infra-") or name.startswith("github"):
            purpose = "基础设施/令牌"
        r = register_pointer_memory(
            manager, name=name, purpose=purpose, username=it.get("username") or "", importance=0.8
        )
        if r.get("is_duplicate"):
            skipped += 1
        else:
            added += 1
    return {"added": added, "skipped": skipped, "listed": len(items)}


def resolve_for_query(query: str, items: Optional[List[Dict]] = None) -> List[Dict[str, str]]:
    """根据问题匹配密码本条目名（仅名称）。"""
    if items is None:
        try:
            items = list_items(100)
        except Exception:
            return []
    q_raw = query or ""
    q = q_raw.lower()
    scored = []
    for it in items:
        name = it.get("name") or ""
        nl = name.lower()
        score = 0
        if name and (name in q_raw or nl in q):
            score += 12
        for part in re.findall(r"[\u4e00-\u9fff]{2,}", name):
            if part in q_raw:
                score += 8
        for part in re.split(r"[\s\-_/]+", nl):
            if len(part) >= 3 and part in q:
                score += 3
        if "api" in q and (nl.startswith("api-") or "api" in nl):
            score += 1
        if ("ssh" in q or "SSH" in q_raw) and nl.startswith("ssh-"):
            score += 1
            for kw in ("homelab", "server", "nas", "router", "gateway", "proxy", "vps"):
                if kw in q_raw and kw in name:
                    score += 10
                if kw.lower() in q and kw.lower() in nl:
                    score += 10
        if "github" in q and "github" in nl:
            score += 3
        if "智谱" in q_raw or "zhipu" in q or "glm" in q:
            if "zhipu" in nl or "glm" in nl:
                score += 6
        if score > 0:
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "name": it["name"],
            "username": it.get("username") or "",
            "how": f"bw-ai get {it['name']}",
            "api": f'POST /secrets/get {{"name":"{it["name"]}","reveal":true}}',
        }
        for _, it in scored[:8]
    ]

