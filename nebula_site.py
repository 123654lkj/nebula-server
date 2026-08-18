#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
站点/产品配置。代码里不写死某家实验室的路径与锁文件名。
环境变量覆盖；不设则用可对外交付的默认值。
"""
from __future__ import annotations

import os
import re
from typing import List, Optional


def _csv(name: str, default: str) -> List[str]:
    raw = os.environ.get(name, default) or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


# 精确钉：query 命中这些正则才按路径置顶（企业可改成 SOP.md|RUNBOOK）
PIN_REGEX = os.environ.get(
    "NEBULA_PIN_REGEX",
    r"[A-Za-z0-9._-]{3,}\.md",
)

# 命中则 canon（逗号分隔子串）
CANON_SUBSTR = _csv(
    "NEBULA_CANON_SUBSTR",
    "vault:gateway/,vault:notes/01-,vault:notes/02-,notes/HOME.md,notes/PROJECT.md",
)

# 命中则降为 source（事故分析/草稿）
DEMOTE_SUBSTR = _csv(
    "NEBULA_DEMOTE_SUBSTR",
    "/04-,/05-,notes/04-,notes/05-",
)

# 会话原文路径子串 → hearsay
HEARSAY_SUBSTR = _csv(
    "NEBULA_HEARSAY_SUBSTR",
    "SESSIONS/,session-extract,minimax-auto-sync,.zcode/obsidian/,openclaw/workspace/memory",
)

# 会话碎片：有 vault 笔记在场时不得当现行
SCRATCH_SUBSTR = _csv(
    "NEBULA_SCRATCH_SUBSTR",
    "session-extract,minimax-auto-sync,.zcode/obsidian/,openclaw/workspace/memory,/home/xiantuer/.openclaw/",
)

# 回读命令模板。空 = 只回逻辑路径。可用 {path}
READBACK_FMT = os.environ.get("NEBULA_READBACK_FMT", "").strip()
# 逻辑前缀 → 落地目录，如 gateway/=/opt,notes/=/data/vault
PATH_MAP = _csv("NEBULA_PATH_MAP", "")

# 可选：租户必填（企业开，家用关）
REQUIRE_TENANT = os.environ.get("NEBULA_REQUIRE_TENANT", "").lower() in ("1", "true", "yes")
DEFAULT_TENANT = os.environ.get("NEBULA_DEFAULT_TENANT", "").strip()

# 可选：Bearer token。空 = 不鉴权（仅受信网络）
API_TOKEN = (os.environ.get("NEBULA_API_TOKEN") or "").strip()


def pin_regex() -> re.Pattern:
    try:
        return re.compile(PIN_REGEX, re.I)
    except re.error:
        return re.compile(r"[A-Za-z0-9._-]{3,}\.md", re.I)


def is_canon_src(src: str) -> bool:
    s = src or ""
    return any(x and x in s for x in CANON_SUBSTR)


def is_demote_src(src: str) -> bool:
    s = src or ""
    return any(x and x in s for x in DEMOTE_SUBSTR)


def is_hearsay_src(src: str) -> bool:
    s = src or ""
    return any(x and x in s for x in HEARSAY_SUBSTR)


def is_scratch_src(src: str) -> bool:
    s = src or ""
    if s.startswith("vault:"):
        return False
    return is_hearsay_src(s) or any(x and x in s for x in SCRATCH_SUBSTR)


def is_vault_src(src: str) -> bool:
    s = src or ""
    return s.startswith("vault:") or s.startswith("notes/")


def format_readback(src: str) -> Optional[str]:
    if not (src or "").startswith("vault:"):
        return None
    path = src[len("vault:") :]
    logical = path
    for spec in PATH_MAP:
        if "=" not in spec:
            continue
        pre, dest = spec.split("=", 1)
        if path.startswith(pre):
            logical = dest.rstrip("/") + "/" + path
            break
    if READBACK_FMT:
        return READBACK_FMT.replace("{path}", logical)
    return f"vault:{path}"


def resolve_tenant(explicit: Optional[str] = None) -> str:
    t = (explicit or "").strip() or DEFAULT_TENANT
    return t
