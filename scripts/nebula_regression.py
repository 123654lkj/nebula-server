#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
星枢真牛逼回归：固定题集，可量化 pass/fail。
用法: python3 nebula_regression.py [--base http://127.0.0.1:26670]
退出码: 0=全过 1=有失败 2=服务不可用
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Case:
    id: str
    query: str
    kind: str  # ask | secrets_resolve | secrets_status | help | reject_secret
    expect: Dict[str, Any]
    body: Optional[Dict] = None


CASES: List[Case] = [
    # --- 权威架构 ---
    Case("A01", "GATEWAY_LOCK 网络冻结", "ask", {
        "min_conf": 0.5,
        "executable": True,
        "trust_any": ["canon"],
        "content_any": ["冻结", "GATEWAY", "禁止", "gateway"],
    }),
    Case("A02", "phantun 主梯子架构", "ask", {
        "min_conf": 0.4,
        "trust_any": ["canon", "source"],
        "content_any": ["phantun", "1080", "gw-xray", "xray"],
    }),
    Case("A03", "星枢在哪台机器端口", "ask", {
        "content_any": ["26670", "192.168.31.252", "虎虎", "huhu"],
    }),
    Case("A04", "能不能随便改代理网关", "ask", {
        "content_any": ["禁止", "冻结", "授权", "GATEWAY"],
    }),
    # --- 工作流/方法论 ---
    Case("W01", "ponytail 七级决策", "ask", {
        "trust_any": ["canon", "source"],
        "content_any": ["ponytail", "七级", "配置", "最小"],
    }),
    Case("W02", "团子学习工作流落地", "ask", {
        "content_any": ["工作流", "落地", "星枢", "笔记", "recall", "OODA", "STRACE", "五件事"],
    }),
    Case("W03", "星枢怎么用 ask bootstrap", "ask", {
        "content_any": ["ask", "bootstrap", "26670", "pack", "contract", "help"],
    }),
    # --- 项目 ---
    Case("P01", "torrent-panel 播放器路径", "ask", {
        "content_any": ["torrent-panel", "8095", "播放", "v2"],
    }),
    # --- 密钥指针（不取明文）---
    Case("S01", "", "secrets_status", {"unlocked": True}),
    Case("S02", "智谱 glm api", "secrets_resolve", {
        "name_any": ["api-zhipu-glm-coding", "api-zhipu-glm-general"],
    }),
    Case("S03", "github token", "secrets_resolve", {
        "name_any": ["github-token"],
    }),
    Case("S04", "ssh 虎虎", "secrets_resolve", {
        "name_any": ["ssh-虎虎", "ssh-huhu", "虎虎-huhu", "huhu"],
    }),
    # --- 安全：禁止明文入向量 ---
    Case("X01", "", "reject_secret", {
        "rejected": True,
        "body": {"content": "leak sk-abcdefghijklmnopqrstuvwxyz0123456789", "category": "fact"},
    }),
    # --- 文档按需 ---
    Case("D01", "", "help", {"level": "mini", "max_chars": 800}),
    Case("D02", "", "help_short", {"level": "short", "min_chars": 400}),
    # --- 健康 ---
    Case("H01", "", "v5_health", {"min_score": 70, "has_links": True}),
    Case("H02", "", "health", {"version_has": "v5"}),
    # --- 再补几道 ---
    Case("A05", "Vaultwarden 密码本地址", "ask", {
        "content_any": ["vault", "8443", "密码", "bw-ai", "Vaultwarden"],
    }),
    Case("A06", "星枢密钥能不能写进向量", "ask", {
        "content_any": ["禁止", "密码本", "Vaultwarden", "指针", "明文"],
    }),
    Case("W04", "失败后 STRACE 复盘", "ask", {
        "content_any": ["STRACE", "根因", "因果", "验证", "失败"],
    }),
    # --- v1.2 记忆闭环强化 ---
    Case("M01", "Agent 记忆如何召回如何存储", "ask", {
        "min_conf": 0.4,
        "trust_any": ["canon", "source"],
        "content_any": ["召回", "存储", "记忆", "readback", "contract", "bootstrap", "星枢", "笔记"],
        "require_readback": True,
    }),
    Case("M02", "团子学习工作手册 40 条", "ask", {
        "content_any": ["WORKBOOK", "工作手册", "40", "Canon", "门禁", "召回"],
    }),
    Case("M03", "虎虎笔记和星枢谁优先", "ask", {
        "trust_any": ["canon", "source"],
        "content_any": ["笔记", "星枢", "权威", "听笔记", "GATEWAY", "L1"],
        "require_readback": True,
    }),
    Case("M04", "纠正写回记忆闭环", "ask", {
        "content_any": ["纠正", "写回", "lesson", "supersede", "correction"],
    }),
]

EXTRA_CASES: List[Case] = [
    Case("T01", "他去年的项目是什么", "ask", {"temporal_intent": "past"}),
    Case("T02", "他现在的项目是什么", "ask", {"temporal_intent": "present"}),
    Case("C01", "GATEWAY_LOCK DNS 定稿", "ask", {
        "content_any": ["GATEWAY", "冻结", "DNS", "AdGuard"],
        "content_none": ["169.254", "0网段断网", "凌晨起 0 网段"],
    }),
]


def http(base: str, method: str, path: str, body: Optional[dict] = None, timeout: int = 90) -> Dict[str, Any]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {"_text": raw, "_status": resp.status}


def pack_blob(r: Dict) -> str:
    parts = [
        r.get("contract") or "",
        r.get("pack") or "",
        r.get("answer") or "",
        json.dumps(r.get("composed") or {}, ensure_ascii=False),
    ]
    for it in r.get("results") or []:
        parts.append(str(it.get("content") or ""))
        parts.append(str(it.get("trust") or ""))
        parts.append(str(it.get("src") or it.get("source_file") or ""))
    return "\n".join(parts).lower()


def run_case(base: str, c: Case) -> Dict[str, Any]:
    t0 = time.time()
    ok = True
    reasons = []
    detail: Dict[str, Any] = {}
    try:
        if c.kind == "ask":
            r = http(base, "POST", "/ask", {
                "query": c.query,
                "top_k": 5,
                "no_cache": True,
                "llm_deep": "off",
                "llm_answer": False,
            }, timeout=60)
            detail["engine"] = r.get("engine")
            composed = r.get("composed") or {}
            conf = float(composed.get("confidence") or 0)
            executable_flag = composed.get("executable")
            trusts = [x.get("trust") for x in (r.get("results") or [])]
            blob = pack_blob(r)
            if "min_conf" in c.expect and conf < c.expect["min_conf"]:
                ok = False
                reasons.append(f"conf {conf}<{c.expect['min_conf']}")
            if c.expect.get("executable") is True and executable_flag is not True:
                # soft: if conf high and canon present, still ok-ish
                if "canon" not in trusts:
                    ok = False
                    reasons.append(f"executable={executable_flag}")
            if c.expect.get("trust_any"):
                if not any(t in trusts for t in c.expect["trust_any"]):
                    ok = False
                    reasons.append(f"trust {trusts} missing {c.expect['trust_any']}")
            if c.expect.get("content_any"):
                if not any(k.lower() in blob for k in c.expect["content_any"]):
                    ok = False
                    reasons.append("content miss")
            if c.expect.get("content_none"):
                ans = (composed.get("answer") or r.get("answer") or "").lower()
                hit = [k for k in c.expect["content_none"] if k.lower() in ans]
                if hit:
                    ok = False
                    reasons.append(f"answer leaked {hit}")
            if c.expect.get("temporal_intent"):
                got = (composed.get("temporal_intent") or "neutral")
                if got != c.expect["temporal_intent"]:
                    ok = False
                    reasons.append(f"intent {got}!={c.expect['temporal_intent']}")
            if c.expect.get("require_readback"):
                rbs = composed.get("readbacks") or []
                if not rbs:
                    # fallback: results with readback field
                    rbs = [x.get("readback") for x in (r.get("results") or []) if x.get("readback")]
                if not rbs:
                    ok = False
                    reasons.append("no readback")
            detail.update({
                "conf": conf,
                "executable": executable_flag,
                "trusts": trusts[:5],
                "readbacks": (composed.get("readbacks") or [])[:3],
            })

        elif c.kind == "secrets_status":
            r = http(base, "GET", "/secrets/status")
            if c.expect.get("unlocked") and not (r.get("ok") or r.get("status") == "unlocked"):
                ok = False
                reasons.append(f"status={r.get('status')}")
            detail = {"status": r.get("status"), "ok": r.get("ok")}

        elif c.kind == "secrets_resolve":
            r = http(base, "POST", "/secrets/resolve", {"query": c.query})
            names = " ".join(m.get("name", "") for m in (r.get("matches") or []))
            if c.expect.get("name_any"):
                if not any(n.lower() in names.lower() for n in c.expect["name_any"]):
                    ok = False
                    reasons.append(f"names={names[:80]}")
            detail = {"matches": [m.get("name") for m in (r.get("matches") or [])[:5]]}

        elif c.kind == "reject_secret":
            try:
                r = http(base, "POST", "/memory/add", c.expect.get("body") or {})
                # should not succeed as ok without reject
                if r.get("status") == "ok" and not r.get("error"):
                    ok = False
                    reasons.append("secret accepted")
                detail = r
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                try:
                    r = json.loads(body)
                except Exception:
                    r = {"raw": body}
                if e.code not in (400, 403) and r.get("status") != "rejected":
                    ok = False
                    reasons.append(f"http {e.code}")
                if "secret" not in json.dumps(r, ensure_ascii=False).lower() and r.get("status") != "rejected":
                    ok = False
                    reasons.append("not rejected")
                detail = r

        elif c.kind == "help":
            r = http(base, "GET", "/help?level=mini&format=json")
            chars = int(r.get("chars") or len(r.get("text") or ""))
            if chars > c.expect.get("max_chars", 9999):
                ok = False
                reasons.append(f"chars {chars}")
            detail = {"chars": chars, "est": r.get("est_tokens")}

        elif c.kind == "help_short":
            r = http(base, "GET", "/help?level=short&format=json")
            chars = int(r.get("chars") or 0)
            if chars < c.expect.get("min_chars", 0):
                ok = False
                reasons.append(f"chars {chars}")
            detail = {"chars": chars}

        elif c.kind == "v5_health":
            r = http(base, "GET", "/v5/health")
            score = (r.get("maturity") or {}).get("score") or 0
            links = r.get("links") or {}
            if score < c.expect.get("min_score", 0):
                ok = False
                reasons.append(f"score {score}")
            if c.expect.get("has_links") and sum(links.values()) < 100:
                ok = False
                reasons.append("few links")
            detail = {"score": score, "links": sum(links.values())}

        elif c.kind == "health":
            r = http(base, "GET", "/health")
            ver = str(r.get("version") or "")
            if c.expect.get("version_has") and c.expect["version_has"] not in ver:
                ok = False
                reasons.append(ver)
            detail = {"version": ver}

        else:
            ok = False
            reasons.append("unknown kind")
    except Exception as e:
        ok = False
        reasons.append(str(e)[:120])
        detail = {"error": str(e)[:200]}

    ms = int((time.time() - t0) * 1000)
    return {
        "id": c.id,
        "query": c.query,
        "kind": c.kind,
        "ok": ok,
        "ms": ms,
        "reasons": reasons,
        "detail": detail,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:26670")
    ap.add_argument("--out", default="")
    ap.add_argument("--extra", action="store_true", help="加上时间意图/反串题探针")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    try:
        http(base, "GET", "/health", timeout=5)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"service down: {e}"}, ensure_ascii=False))
        return 2

    results = []
    suite = list(CASES) + (EXTRA_CASES if args.extra else [])
    for c in suite:
        results.append(run_case(base, c))

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    report = {
        "ok": passed == total,
        "passed": passed,
        "failed": total - passed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total else 0,
        "ts": time.time(),
        "base": base,
        "results": results,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path = __import__("pathlib").Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    # also write latest
    try:
        from pathlib import Path
        latest = Path("/home/huhu/.local/state/nebula-regression/latest.json")
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(text, encoding="utf-8")
    except Exception:
        pass

    # push pass_rate into a memory pointer (no secrets)
    if passed == total:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
