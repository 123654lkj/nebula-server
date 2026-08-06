#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星枢进化：会话抽取（Mem0 能力）+ 分层纪律辅助
- session_extract: 3.7 从会话抽事实 → 分流写入星枢/笔记草稿/密钥指针提示
- 明文密钥永不入向量
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional  # time used in return ts

logger = logging.getLogger("nebula.extract")

EXTRACT_SYSTEM = """你是记忆抽取器（Mem0 风格）。从会话材料中提取**可复用、已较明确**的记忆项。
只输出 JSON 数组，不要 markdown 围栏，不要解释。

每项格式：
{
  "kind": "fact|lesson|decision|preference|project|infra|skip",
  "content": "一句完整中文记忆（自洽、可检索）",
  "category": "fact|lesson|decision|preference|project|infrastructure|network|code|ai|security",
  "importance": 0.4-0.95,
  "write_memory": true/false,
  "vault_path": "04-项目笔记/xxx.md 或空",
  "vault_snippet": "若应写笔记则给短 markdown，否则空",
  "secret_hint": "若涉及密钥只写条目名建议如 api-xxx，禁止写明文密钥",
  "reason": "为何保留/跳过"
}

规则：
1. 跳过寒暄、重复、未验证猜测（kind=skip 或 write_memory=false）
2. 密钥/密码/token 明文：不要放进 content；secret_hint 只给名称建议
3. 网络/网关结论：importance>=0.8，category=infrastructure，提示以 GATEWAY_LOCK 为准
4. 最多 8 条；content 每条 <= 200 字
5. 用户明确偏好 → preference；踩坑+修法 → lesson；选型结论 → decision
"""


def _parse_json_array(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []
    text = text.strip()
    m = re.search(r"\[[\s\S]*\]", text)
    raw = m.group(0) if m else text
    try:
        data = json.loads(raw)
    except Exception:
        # 尝试逐行对象
        objs = re.findall(r"\{[^{}]*\}", text)
        data = []
        for o in objs:
            try:
                data.append(json.loads(o))
            except Exception:
                pass
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]



def fallback_extract(transcript: str, focus: str = "", max_items: int = 8) -> list:
    """LLM 不可用时的规则抽取，保证进化链路不空转。"""
    text = transcript or ""
    parts = re.split(r"[。！？\n]+", text)
    keys = ("确认", "禁止", "必须", "只认", "路径", "已验证", "优先", "不要", "不能", "默认", "端口", "服务")
    items = []
    for s in parts:
        s = s.strip()
        if len(s) < 12 or len(s) > 200:
            continue
        if not any(k in s for k in keys):
            continue
        kind = "lesson" if any(k in s for k in ("禁止", "不要", "坑", "失败")) else "fact"
        if "优先" in s or "喜欢" in s:
            kind = "preference"
        if "确认" in s or "只认" in s or "路径" in s:
            kind = "decision"
        cat = "project" if any(k in s for k in ("panel", "路径", "项目", "torrent")) else "fact"
        if "禁止" in s and any(k in s for k in ("key", "sk-", "密钥", "密码", "token")):
            cat = "security"
        items.append({
            "kind": kind,
            "content": s if s.endswith("。") else s + "。",
            "category": cat,
            "importance": 0.75 if kind in ("decision", "lesson") else 0.65,
            "write_memory": True,
            "vault_path": "",
            "vault_snippet": "",
            "secret_hint": "",
            "reason": "fallback_rule",
        })
        if len(items) >= max_items:
            break
    if not items and focus:
        items.append({
            "kind": "fact",
            "content": f"会话焦点：{focus}。材料过短或LLM不可用，仅记录焦点。",
            "category": "fact",
            "importance": 0.5,
            "write_memory": True,
            "reason": "fallback_focus",
        })
    return items


def session_extract(
    manager,
    transcript: str,
    focus: str = "",
    dry_run: bool = False,
    max_items: int = 8,
    auto_write: bool = True,
) -> Dict[str, Any]:
    """
    Mem0 型会话抽取。
    dry_run=True 或 auto_write=False：只返回草稿不落库。
    """
    from nebula_v5 import _llm_chat
    from nebula_secrets import scan_secrets

    transcript = (transcript or "").strip()
    if len(transcript) < 20:
        return {"status": "ok", "items": [], "written": [], "skipped": [], "reason": "transcript too short"}
    used_fallback = False

    # 先扫整段：若大量密钥，截断提醒
    hits = scan_secrets(transcript)
    safe_transcript = transcript
    if hits:
        from nebula_secrets import redact_secrets
        safe_transcript, _ = redact_secrets(transcript)

    user = f"焦点：{focus or '无'}\n\n会话材料：\n{safe_transcript[:8000]}"
    raw = _llm_chat(EXTRACT_SYSTEM, user, max_tokens=1200, temperature=0.2)
    items = _parse_json_array(raw)[:max_items]
    if not items:
        items = fallback_extract(safe_transcript, focus=focus, max_items=max_items)
        used_fallback = True

    written = []
    skipped = []
    vault_drafts = []
    secret_hints = []

    for it in items:
        kind = (it.get("kind") or "fact").lower()
        content = (it.get("content") or "").strip()
        if kind == "skip" or not content:
            skipped.append({"reason": it.get("reason") or "skip", "content": content[:80]})
            continue
        if not it.get("write_memory", True):
            skipped.append({"reason": it.get("reason") or "write_memory=false", "content": content[:80]})
            continue

        # 密钥保护
        sec_hits = scan_secrets(content)
        if sec_hits:
            hint = it.get("secret_hint") or ""
            secret_hints.append({"blocked": content[:60], "patterns": sec_hits, "secret_hint": hint})
            # 若有条目名建议，只写指针说明
            if hint and re.match(r"^[\w\-\u4e00-\u9fff]{3,64}$", hint.strip()):
                content = (
                    f"[vaultwarden指针] item={hint.strip()}\n"
                    f"用途: 会话抽取提及\n"
                    f"取用: bw-ai get {hint.strip()}\n"
                    f"禁止明文入向量"
                )
                it["category"] = "credential"
                it["importance"] = min(float(it.get("importance") or 0.8), 0.85)
            else:
                skipped.append({"reason": f"secret_blocked:{sec_hits}", "content": content[:60]})
                continue

        cat = it.get("category") or "fact"
        if cat not in (
            "fact", "lesson", "decision", "preference", "project",
            "infrastructure", "network", "code", "ai", "security", "credential",
        ):
            cat = "fact"
        imp = float(it.get("importance") or 0.6)
        imp = max(0.35, min(0.95, imp))

        rec = {
            "kind": kind,
            "content": content,
            "category": cat,
            "importance": imp,
            "reason": it.get("reason") or "",
        }

        if dry_run or not auto_write:
            rec["dry_run"] = True
            written.append(rec)
        else:
            try:
                r = manager.add(
                    content=content,
                    source="session-extract",
                    category=cat,
                    importance=imp,
                    metadata={
                        "trust": "source" if imp >= 0.75 else "synthesis",
                        "kind": kind,
                        "focus": focus or "",
                        "engine": "session_extract_v5",
                    },
                )
                rec["id"] = r.get("id")
                rec["is_duplicate"] = r.get("is_duplicate")
                written.append(rec)
            except ValueError as e:
                skipped.append({"reason": str(e)[:120], "content": content[:60]})
            except Exception as e:
                logger.warning("add fail: %s", e)
                skipped.append({"reason": f"add_error:{e}", "content": content[:60]})

        vp = (it.get("vault_path") or "").strip()
        vs = (it.get("vault_snippet") or "").strip()
        if vp and vs and not scan_secrets(vs):
            vault_drafts.append({"path": vp, "markdown": vs[:1500], "title_hint": kind})

        sh = (it.get("secret_hint") or "").strip()
        if sh:
            secret_hints.append({"secret_hint": sh, "note": "用 /secrets/store 或 bw-ai，勿写明文"})

    # 分层摘要（Letta 纪律提示）
    layers = {
        "L0_working": "当前会话正文（本请求 transcript，用完可丢）",
        "L1_core": "下次开场用 POST /v5/bootstrap；问答用 /ask 的 contract",
        "L2_archival": f"本次写入星枢 {len([w for w in written if not w.get('dry_run')])} 条；笔记草稿 {len(vault_drafts)} 条待确认",
        "L3_secrets": "密钥只走 Vaultwarden；星枢仅指针",
    }

    return {
        "status": "ok",
        "engine": "session_extract_v5",
        "model": "qwen3.7-plus",
        "used_fallback": used_fallback,
        "dry_run": dry_run or not auto_write,
        "focus": focus,
        "raw_llm_chars": len(raw or ""),
        "items_extracted": len(items),
        "written": written,
        "skipped": skipped,
        "vault_drafts": vault_drafts,
        "secret_hints": secret_hints,
        "layers": layers,
        "hint": "vault_drafts 需人工/Agent 确认后写入笔记；不要自动覆盖 GATEWAY_LOCK/MEMORY",
        "ts": time.time(),
    }


def layered_recall_plan(focus: str = "") -> Dict[str, Any]:
    """Letta 纪律：返回 Agent 应调用的分层步骤（不占大量上下文）。"""
    f = focus or "当前任务"
    return {
        "status": "ok",
        "engine": "layered_recall_v5",
        "focus": f,
        "steps": [
            {
                "layer": "L1_core",
                "action": "POST /v5/bootstrap",
                "body": {"focus": f, "budget_chars": 2000},
                "use": "bootstrap 字段注入",
            },
            {
                "layer": "L2_retrieve",
                "action": "POST /ask",
                "body": {"query": f, "top_k": 5, "llm_deep": "auto"},
                "use": "contract > pack；看 executable/trust",
            },
            {
                "layer": "L1_authority",
                "action": "若 readback/vault:",
                "body": None,
                "use": "rxt read 笔记原文，GATEWAY_LOCK 最高",
            },
            {
                "layer": "L3_secrets",
                "action": "POST /secrets/resolve",
                "body": {"query": f},
                "use": "只拿条目名；明文 bw-ai get / secrets/get reveal",
            },
            {
                "layer": "L0_work",
                "action": "执行任务",
                "body": None,
                "use": "工具输出压缩；勿 dump 星枢全文",
            },
            {
                "layer": "L2_writeback",
                "action": "POST /v5/session-extract",
                "body": {"transcript": "<会话摘要>", "focus": f, "auto_write": True},
                "use": "收尾自动抽记忆；vault_drafts 确认后写笔记",
            },
        ],
        "rules": [
            "权威：用户原话 > GATEWAY_LOCK > 知识库 > 星枢 canon > source > synthesis",
            "密钥永不入向量/笔记明文",
            "executable=false 禁止当现行执行",
            "full /help 禁止写进常驻 system prompt",
        ],
    }
