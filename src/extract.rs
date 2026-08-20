//! 会话抽取 v6。

use crate::engine::Engine;
use crate::llm::llm_chat;
use crate::rank::ReflectOpts;
use crate::secrets::scan_secrets;
use anyhow::Result;
use serde_json::{json, Value};

const EXTRACT_SYSTEM: &str = r#"你是记忆抽取器（星枢 v6 · 对齐 Memory≠RAG）。从会话材料中提取**可复用、已较明确**的记忆项。
只输出 JSON 数组，不要 markdown 围栏，不要解释。

每项格式：
{
  "kind": "fact|lesson|decision|preference|project|infra|skip",
  "memory_layer": "semantic|episodic|procedural",
  "abstract": "≤80字中文摘要（L0，可独立检索）",
  "content": "一句完整中文记忆（自洽、可检索，≤200字）",
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
2. 密钥/密码/token 明文：不要放进 content/abstract
3. 最多 8 条；content 每条 <= 200 字；abstract 必填且 <= 80 字
"#;

const EPISODE_SYSTEM: &str = r#"你是会话情景摘要器。根据材料写**一条** episodic 记忆 JSON 对象（不要数组、不要围栏）：
{
  "abstract": "≤80字：本次会话做了什么",
  "content": "≤220字：任务目标+关键结论+未决项（可检索）",
  "importance": 0.45-0.8,
  "category": "project|lesson|fact|infrastructure|decision"
}
跳过纯寒暄；无实质则 importance=0 且 content 为空。
"#;

fn parse_json_array(text: &str) -> Vec<Value> {
    if text.is_empty() {
        return vec![];
    }
    let re = regex::Regex::new(r"\[[\s\S]*\]").unwrap();
    let raw = re.find(text).map(|m| m.as_str()).unwrap_or(text);
    if let Ok(Value::Array(a)) = serde_json::from_str::<Value>(raw) {
        return a.into_iter().filter(|x| x.is_object()).collect();
    }
    let re2 = regex::Regex::new(r"\{[^{}]*\}").unwrap();
    re2.find_iter(text)
        .filter_map(|m| serde_json::from_str::<Value>(m.as_str()).ok())
        .filter(|x| x.is_object())
        .collect()
}

fn parse_json_obj(text: &str) -> Option<Value> {
    let re = regex::Regex::new(r"\{[\s\S]*\}").unwrap();
    let raw = re.find(text.trim()).map(|m| m.as_str()).unwrap_or(text);
    serde_json::from_str::<Value>(raw).ok().filter(|x| x.is_object())
}

fn norm_layer(layer: &str, kind: &str) -> String {
    let l = layer.trim().to_lowercase();
    if ["semantic", "episodic", "procedural"].contains(&l.as_str()) {
        return l;
    }
    match kind {
        "lesson" | "episode" => "episodic".into(),
        _ => "semantic".into(),
    }
}

pub async fn session_extract(
    eng: &Engine,
    transcript: &str,
    focus: &str,
    dry_run: bool,
    max_items: usize,
    auto_write: bool,
) -> Result<Value> {
    if transcript.trim().chars().count() < 20 {
        return Ok(json!({
            "status": "ok", "items": [], "written": [], "skipped": [],
            "reason": "transcript too short"
        }));
    }
    let timeout: f64 = std::env::var("NEBULA_EXTRACT_LLM_TIMEOUT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(45.0);
    let user = format!("焦点：{focus}\n材料：\n{}", transcript.chars().take(8000).collect::<String>());
    let raw = llm_chat(EXTRACT_SYSTEM, &user, 800, 0.2, timeout).await;
    let used_fallback = raw.is_empty();
    let items = parse_json_array(&raw);
    let mut written = vec![];
    let mut skipped = vec![];
    let mut vault_drafts = vec![];
    let mut secret_hints = vec![];
    let mut layer_counts = serde_json::Map::new();
    let write = auto_write && !dry_run;
    for it in items.iter().take(max_items.max(1).min(8)) {
        let kind = it.get("kind").and_then(|x| x.as_str()).unwrap_or("fact");
        if kind == "skip" || it.get("write_memory") == Some(&json!(false)) {
            skipped.push(json!({"reason": it.get("reason"), "kind": kind}));
            continue;
        }
        let content = it.get("content").and_then(|x| x.as_str()).unwrap_or("").trim().to_string();
        if content.is_empty() {
            skipped.push(json!({"reason": "empty"}));
            continue;
        }
        if !scan_secrets(&content).is_empty() {
            skipped.push(json!({"reason": "secret_in_content"}));
            continue;
        }
        if let Some(h) = it.get("secret_hint").and_then(|x| x.as_str()) {
            if !h.is_empty() {
                secret_hints.push(h);
            }
        }
        if let Some(vp) = it.get("vault_path").and_then(|x| x.as_str()) {
            if !vp.is_empty() {
                vault_drafts.push(json!({"path": vp, "snippet": it.get("vault_snippet")}));
            }
        }
        let layer = norm_layer(
            it.get("memory_layer").and_then(|x| x.as_str()).unwrap_or(""),
            kind,
        );
        *layer_counts.entry(layer.clone()).or_insert(json!(0)) = json!(
            layer_counts.get(&layer).and_then(|x| x.as_i64()).unwrap_or(0) + 1
        );
        let abstract_s = it
            .get("abstract")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .chars()
            .take(80)
            .collect::<String>();
        let imp = it.get("importance").and_then(|x| x.as_f64()).unwrap_or(0.6).min(0.85);
        let cat = it.get("category").and_then(|x| x.as_str()).unwrap_or("fact");
        if write {
            let vec = match eng.embedder.embed(&content).await {
                Ok(v) => v,
                Err(e) => {
                    skipped.push(json!({"reason": format!("embed:{e}")}));
                    continue;
                }
            };
            match eng.add(
                &content,
                "session-extract",
                Some(cat),
                None,
                imp,
                json!({"memory_layer": layer, "abstract": abstract_s, "kind": kind, "trust": if imp>=0.75 {"source"} else {"synthesis"}}),
                true,
                None,
                vec,
            ) {
                Ok(r) => written.push(r),
                Err(e) => skipped.push(json!({"reason": e.to_string()})),
            }
        } else {
            written.push(json!({"dry_run": true, "content": content, "kind": kind, "memory_layer": layer}));
        }
    }
    let mut episode = json!(null);
    let ep_raw = llm_chat(EPISODE_SYSTEM, &user, 400, 0.2, timeout).await;
    if let Some(ep) = parse_json_obj(&ep_raw) {
        let ep_content = ep.get("content").and_then(|x| x.as_str()).unwrap_or("").trim().to_string();
        let ep_imp = ep.get("importance").and_then(|x| x.as_f64()).unwrap_or(0.0);
        if !ep_content.is_empty() && ep_imp > 0.0 && scan_secrets(&ep_content).is_empty() && write {
            if let Ok(vec) = eng.embedder.embed(&ep_content).await {
                let r = eng.add(
                    &ep_content,
                    "session-extract",
                    ep.get("category").and_then(|x| x.as_str()),
                    None,
                    ep_imp,
                    json!({"memory_layer":"episodic","episode":true,"kind":"episode","abstract": ep.get("abstract")}),
                    true,
                    None,
                    vec,
                );
                if let Ok(rr) = r {
                    if rr.get("is_duplicate") != Some(&json!(true)) {
                        *layer_counts.entry("episodic".to_string()).or_insert(json!(0)) = json!(
                            layer_counts.get("episodic").and_then(|x| x.as_i64()).unwrap_or(0) + 1
                        );
                        written.push(rr.clone());
                    }
                    episode = rr;
                }
            }
        } else {
            episode = json!({"dry_run": true, "content": ep_content});
        }
    }
    Ok(json!({
        "status": "ok",
        "engine": "session_extract_v6",
        "model": std::env::var("NEBULA_LLM_MODEL").unwrap_or_else(|_| "MiniMax-M3".into()),
        "used_fallback": used_fallback,
        "dry_run": dry_run || !auto_write,
        "focus": focus,
        "raw_llm_chars": raw.chars().count(),
        "items_extracted": items.len(),
        "written": written,
        "skipped": skipped,
        "vault_drafts": vault_drafts,
        "secret_hints": secret_hints,
        "episode": episode,
        "layer_counts": layer_counts,
        "hint": "vault_drafts 需确认后写笔记；abstract 已作 L0；memory_layer 可过滤检索",
        "ts": crate::temporal::now_ts(),
    }))
}

pub fn layered_recall_plan(focus: &str) -> Value {
    let f = if focus.is_empty() { "当前任务" } else { focus };
    json!({
        "status": "ok",
        "engine": "layered_recall_v6",
        "focus": f,
        "steps": [
            {"layer":"L1_core","action":"POST /v5/bootstrap","body":{"focus": f, "budget_chars": 2000},"use":"bootstrap 注入（L0 摘要优先）"},
            {"layer":"L2_retrieve","action":"POST /ask","body":{"query": f, "top_k": 5, "llm_deep": "auto"},"use":"contract > pack；看 executable/trust/memory_layer"},
            {"layer":"L1_authority","action":"若 readback/vault:","body":null,"use":"rxt read 原文，GATEWAY_LOCK 最高"},
            {"layer":"L3_secrets","action":"POST /secrets/resolve","body":{"query": f},"use":"只拿条目名；明文 bw-ai"},
            {"layer":"L0_work","action":"执行任务","body":null,"use":"勿 dump 星枢全文"},
            {"layer":"L2_writeback","action":"POST /v5/session-extract","body":{"transcript":"<会话摘要>","focus": f, "auto_write": true},"use":"收尾抽取 v6 layer+abstract+episode"}
        ],
        "memory_layers": {
            "semantic": "稳定事实/偏好/架构",
            "episodic": "任务过程/情景",
            "procedural": "怎么做/步骤"
        },
        "rules": [
            "权威：用户原话 > GATEWAY_LOCK > 虎虎笔记 > 星枢 canon > source > synthesis",
            "密钥永不入向量/笔记明文",
            "executable=false 禁止当现行执行",
            "决定记什么 > 如何检索；收尾必 session-extract"
        ]
    })
}

pub async fn promote_draft(eng: &Engine, query: &str, notes: &str) -> Result<Value> {
    let ask = crate::rank::reflect_ask(
        eng,
        if query.is_empty() { "任务结论" } else { query },
        ReflectOpts {
            top_k: 5,
            max_chars: 280,
            max_total: 1800,
            use_hybrid: true,
            category: None,
            use_graph: true,
            hops: 2,
            drop_hearsay: true,
            max_synthesis: 1,
            temporal_intent: None,
            as_of: None,
            prefer_layers: None,
            tenant_id: None,
            precomputed: None,
        },
    )?;
    let pack = ask.get("pack").and_then(|x| x.as_str()).unwrap_or("");
    let system = "你是记忆晋升助手。根据证据判断是否应写入虎虎笔记。只输出 JSON，不要 markdown 围栏。格式: {\"should_write_vault\":true/false,\"path\":\"04-项目笔记/xxx.md\",\"title\":\"...\",\"markdown\":\"中文正文\",\"category\":\"lesson|project|fact\",\"importance\":0.0-1.0,\"memory_summary\":\"给星枢的一句话\",\"reason\":\"理由\"}。密钥/密码不要写进 markdown。权威配置冲突时以 GATEWAY_LOCK 为准。未验证的推断 should_write_vault=false。";
    let user = format!("线索/会话:\n{}\n\n星枢证据:\n{}", notes.chars().take(1500).collect::<String>(), pack.chars().take(2000).collect::<String>());
    let text = llm_chat(system, &user, 800, 0.2, 12.0).await;
    let mut data = parse_json_obj(&text).unwrap_or(json!({"should_write_vault": false, "reason": "LLM不可用"}));
    data["evidence_pack"] = json!(pack);
    data["engine"] = json!("promote_draft_v5");
    if let Some(md) = data.get("markdown").and_then(|x| x.as_str()) {
        if !scan_secrets(md).is_empty() {
            data["should_write_vault"] = json!(false);
            data["secret_blocked"] = json!(true);
            data["markdown"] = json!("[REDACTED]");
        }
    }
    Ok(data)
}
