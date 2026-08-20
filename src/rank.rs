//! 权威加权、pack、v4 reflect、v5 ultimate。对齐 Python 行为。

use crate::engine::Engine;
use crate::rerank::{apply_rerank, seed_already_answers};
use crate::site::{format_readback, is_canon_src, is_demote_src, is_hearsay_src, is_scratch_src, is_vault_src, pin_regex};
use crate::temporal::{extract_dated_events, solve_temporal};
use crate::types::{Hit, PackOpts, SearchOpts};
use anyhow::Result;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

const DEMOTE_MARKERS: &[&str] = &["【已过时", "SUPERSEDED", "已废弃", "不再使用", "仅历史", "假信息", "误导", "deprecated"];
const NOISE: &[&str] = &[
    "explain", "bm25_score", "vector_score", "time_decay", "authority_mult", "created_at",
    "level", "node_type", "node_name", "project_name", "location", "metadata",
];

pub fn infer_trust(content: &str, source_file: &str, importance: f64, explicit: Option<&str>) -> String {
    if let Some(e) = explicit {
        if ["canon", "source", "synthesis", "hearsay", "superseded"].contains(&e) {
            return e.into();
        }
    }
    let src = source_file;
    let head: String = content.chars().take(500).collect();
    if ["【已过时", "SUPERSEDED", "已废弃", "仅历史"].iter().any(|m| head.contains(m)) {
        return "superseded".into();
    }
    if is_scratch_src(src) {
        return "hearsay".into();
    }
    if is_vault_src(src) {
        return if is_demote_src(src) { "source".into() } else { "canon".into() };
    }
    if src.starts_with("vault:gateway/") || src.contains("GATEWAY_LOCK") {
        return "canon".into();
    }
    if head.contains("现行权威") || head.contains("Agent 必读") || head.contains("[现行") {
        return "canon".into();
    }
    if src.contains("SESSIONS/") || (src.contains("SESSIONS") && src.ends_with(".md")) {
        return "hearsay".into();
    }
    if src == "session-extract" || src == "minimax-auto-sync" {
        return "hearsay".into();
    }
    if ["manual", "grok", "hermes", "api", "mcp", "tuanzi-distill"].contains(&src) || src.is_empty() {
        return if importance >= 0.85 { "source".into() } else { "synthesis".into() };
    }
    if importance >= 0.9 {
        return "source".into();
    }
    "synthesis".into()
}

pub fn apply_authority_boost(eng: &Engine, mut results: Vec<Hit>, explain: bool) -> Result<Vec<Hit>> {
    if results.is_empty() {
        return Ok(results);
    }
    let mut need: Vec<i64> = vec![];
    for r in &results {
        let filled = !r.content.is_empty()
            && r.importance.is_some()
            && (r.source_file.is_some() || r.src.is_some())
            && r.trust.is_some();
        if !filled {
            need.push(r.id);
        }
    }
    let mut meta: HashMap<i64, (f64, String, Option<String>, String, Option<String>, Option<String>)> = HashMap::new();
    for r in &results {
        if !need.contains(&r.id) {
            meta.insert(
                r.id,
                (
                    r.importance.unwrap_or(0.5),
                    r.src_file().to_string(),
                    r.category.clone(),
                    r.content.clone(),
                    None,
                    r.trust.clone(),
                ),
            );
        }
    }
    if !need.is_empty() {
        let ph = vec!["?"; need.len()].join(",");
        let sql = format!(
            "SELECT id, importance, source_file, category, content, metadata, trust FROM memories WHERE id IN ({ph})"
        );
        let _ = eng.with_conn(|c| {
            let mut stmt = c.prepare(&sql)?;
            let bind: Vec<&dyn rusqlite::types::ToSql> =
                need.iter().map(|id| id as &dyn rusqlite::types::ToSql).collect();
            let rows = stmt.query_map(bind.as_slice(), |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, Option<f64>>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, Option<String>>(5)?,
                    row.get::<_, Option<String>>(6)?,
                ))
            })?;
            for row in rows.flatten() {
                meta.insert(
                    row.0,
                    (
                        row.1.unwrap_or(0.5),
                        row.2.unwrap_or_default(),
                        row.3,
                        row.4.unwrap_or_default(),
                        row.5,
                        row.6,
                    ),
                );
            }
            Ok(())
        });
    }
    for r in results.iter_mut() {
        let Some((imp, src, cat, content, metadata_json, db_trust)) = meta.get(&r.id).cloned() else {
            continue;
        };
        let mut mult = 1.0;
        let mut trust = db_trust.clone().unwrap_or_else(|| "synthesis".into());
        if imp >= 0.95 {
            mult *= 1.45;
        } else if imp >= 0.9 {
            mult *= 1.35;
        } else if imp >= 0.7 {
            mult *= 1.15;
        } else if imp < 0.35 {
            mult *= 0.45;
        } else if imp < 0.5 {
            mult *= 0.85;
        }
        if is_hearsay_src(&src) || is_scratch_src(&src) {
            trust = "hearsay".into();
            if !is_canon_src(&src) {
                mult *= 0.55;
            }
        }
        if is_canon_src(&src) {
            mult *= if src.starts_with("vault:gateway/") { 1.55 } else { 1.4 };
            trust = "canon".into();
        } else if is_demote_src(&src) {
            mult *= 1.08;
            if trust == "canon" {
                trust = "source".into();
            }
        } else if src.starts_with("vault:") {
            mult *= 1.12;
            if trust == "canon" {
                trust = "source".into();
            }
        } else if is_hearsay_src(&src) {
            mult *= 0.75;
            trust = "hearsay".into();
        } else if ["manual", "grok", "api", "hermes", "session-extract"].contains(&src.as_str()) || src.is_empty() {
            trust = if imp >= 0.85 { "source".into() } else { "synthesis".into() };
            if src == "session-extract" && imp >= 0.75 {
                mult *= 1.08;
            }
        }
        let head: String = content.chars().take(400).collect();
        if DEMOTE_MARKERS.iter().any(|m| head.contains(m)) {
            mult *= 0.22;
            trust = "superseded".into();
        }
        if head.contains("现行权威") || head.contains("Agent 必读") || head.contains("[2026-07-11 现行") {
            mult *= 1.25;
            if trust != "superseded" {
                trust = "canon".into();
            }
        }
        if r.importance.is_none() {
            r.importance = Some(imp);
        }
        if r.source_file.as_deref().unwrap_or("").is_empty() {
            r.source_file = if src.is_empty() { None } else { Some(src.clone()) };
        }
        if r.category.is_none() {
            r.category = cat.clone();
        }
        if db_trust.as_deref() == Some("superseded") && trust != "superseded" {
            trust = "superseded".into();
            mult *= 0.22;
        }
        r.trust = Some(trust);
        r.authority_mult = Some(((mult as f64) * 10000.0).round() / 10000.0);
        if let Some(mj) = metadata_json {
            if r.metadata.is_none() {
                if let Ok(v) = serde_json::from_str::<Value>(&mj) {
                    r.metadata = Some(v);
                }
            }
        }
        r.score = ((r.score * mult) * 1_000_000.0).round() / 1_000_000.0;
        if explain || r.explain.is_some() {
            let mut exp = r.explain.clone().unwrap_or(json!({}));
            exp["authority_mult"] = json!(mult);
            exp["trust"] = json!(r.trust);
            exp["importance"] = json!(imp);
            exp["final_score"] = json!(r.score);
            r.explain = Some(exp);
        }
        if src.starts_with("vault:") {
            if let Some(rb) = format_readback(&src) {
                r.readback = Some(rb);
            }
        }
    }
    results.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
    Ok(results)
}

fn extract_snippet(content: &str, query: &str, max_chars: usize) -> String {
    if content.is_empty() {
        return String::new();
    }
    let re = regex::Regex::new(r"(?m)^\[虎虎笔记\][^\n]*\n?").unwrap();
    let mut text = re.replace_all(content, "").trim().to_string();
    if text.starts_with("---") {
        if let Some(end) = text[3..].find("\n---") {
            text = text[end + 7..].trim().to_string();
        }
    }
    let mut lines = vec![];
    for ln in text.lines() {
        let s = ln.trim();
        if s.is_empty() || s == "---" {
            continue;
        }
        if s.starts_with("source=vault:") || s.starts_with("回读命令:") {
            continue;
        }
        if s.starts_with("chunk=") && s.len() < 40 {
            continue;
        }
        if s.starts_with("title=") && s.len() < 80 {
            continue;
        }
        lines.push(ln);
    }
    let mut text = lines.join("\n").trim().to_string();
    if text.is_empty() {
        text = content.trim().to_string();
    }
    if text.chars().count() <= max_chars {
        return text;
    }
    let q = query.to_lowercase();
    let tok_re = regex::Regex::new(r"[\w\u4e00-\u9fff]{2,}").unwrap();
    let stop = ["的", "了", "和", "是", "在", "与", "或", "什么", "怎么", "如何", "一下", "这个", "那个", "一个", "我们", "可以", "需要"];
    let tokens: Vec<String> = tok_re
        .find_iter(&q)
        .map(|m| m.as_str().to_string())
        .filter(|t| !stop.contains(&t.as_str()))
        .collect();
    let phrases: Vec<String> = if q.chars().count() >= 4 {
        vec![q.chars().take(40).collect()]
    } else {
        vec![]
    };
    let window = max_chars;
    let step = (max_chars / 6).max(24);
    let lower = text.to_lowercase();
    let chars: Vec<char> = text.chars().collect();
    let limit = chars.len().saturating_sub(window).max(1);
    let mut best_i = 0usize;
    let mut best_score = -1e18f64;
    let mut i = 0;
    while i < limit {
        let chunk: String = chars[i..chars.len().min(i + window)].iter().collect::<String>().to_lowercase();
        let mut sc = 0.0;
        for t in &tokens {
            let c = chunk.matches(t).count();
            if c > 0 {
                sc += (3.0 + (c.min(5) as f64)) * if t.chars().count() >= 4 { 1.2 } else { 1.0 };
            }
        }
        for ph in &phrases {
            if !ph.is_empty() && chunk.contains(ph) {
                sc += 6.0;
            }
        }
        for (kw, w) in [
            ("禁止", 4.0), ("冻结", 4.0), ("现行", 3.0), ("权威", 3.0), ("必读", 3.0),
            ("结论", 2.0), ("默认", 1.5), ("必须", 2.0), ("不要", 2.0), ("只能", 2.0), ("端口", 1.0),
        ] {
            if chunk.contains(kw) {
                sc += w;
            }
        }
        if chunk.contains("相关文档") || chunk.contains("参见") || chunk.contains("目录") {
            sc -= 5.0;
        }
        if chunk.trim_start().starts_with('|') && chunk.matches('|').count() > 8 {
            sc -= 1.0;
        }
        sc -= i as f64 / chars.len().max(1) as f64 * 1.5;
        if sc > best_score {
            best_score = sc;
            best_i = i;
        }
        i += step;
    }
    let mut snippet: String = chars[best_i..chars.len().min(best_i + window)].iter().collect();
    snippet = snippet.trim().to_string();
    if best_i > 0 {
        snippet = format!("…{snippet}");
    }
    if best_i + window < chars.len() {
        snippet.push('…');
    }
    snippet
}

fn query_hit_score(content: &str, query: &str) -> f64 {
    if content.is_empty() {
        return 0.0;
    }
    let lower = content.to_lowercase();
    let tok_re = regex::Regex::new(r"[\w\u4e00-\u9fff]{2,}").unwrap();
    let mut sc = 0.0;
    for t in tok_re.find_iter(&query.to_lowercase()) {
        let t = t.as_str();
        sc += lower.matches(t).count() as f64 * if t.chars().count() >= 4 { 1.5 } else { 1.0 };
    }
    for kw in ["现行", "权威", "禁止", "冻结", "必读"] {
        if content.contains(kw) {
            sc += 2.0;
        }
    }
    let head: String = content.chars().take(200).collect();
    if head.contains("相关文档") {
        sc -= 2.0;
    }
    sc
}

pub fn pack_results(results: &[Hit], query: &str, opts: &PackOpts) -> Vec<Hit> {
    if results.is_empty() {
        return vec![];
    }
    let mut ordered = results.to_vec();
    ordered.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
    let qlow = query.to_lowercase();
    let img_keys = ["图片", "照片", "截图", "这张图", "图里", "看图", "image", "photo", "screenshot"];
    if img_keys.iter().any(|k| query.contains(k) || qlow.contains(k)) {
        ordered.sort_by(|a, b| {
            let ia = (a.modality.as_deref() == Some("image")) as i32;
            let ib = (b.modality.as_deref() == Some("image")) as i32;
            ib.cmp(&ia).then(b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal))
        });
    }
    if let Some(pref) = &opts.prefer_layers {
        let pref: HashSet<String> = pref.iter().map(|s| s.to_lowercase()).collect();
        if !pref.is_empty() {
            ordered.sort_by(|a, b| {
                let la = a.memory_layer.as_deref().unwrap_or("").to_lowercase();
                let lb = b.memory_layer.as_deref().unwrap_or("").to_lowercase();
                let ia = pref.contains(&la) as i32;
                let ib = pref.contains(&lb) as i32;
                ib.cmp(&ia).then(b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal))
            });
        }
    }
    fn is_vault(r: &Hit) -> bool {
        let s = r.src_file();
        s.starts_with("vault:") || s.starts_with("notes/") || s.starts_with("gateway/") || s.contains("/opt/gateway/")
    }
    let wants_session = ["上次", "刚才", "那次会", "会话里", "会话说", "session-extract", "提炼报告"]
        .iter()
        .any(|k| query.contains(k));
    let npeek = (opts.top_k * 4).max(16);
    let has_vault = ordered.iter().take(npeek).any(is_vault);
    if has_vault && !wants_session {
        let kept: Vec<Hit> = ordered
            .iter()
            .filter(|r| is_vault(r) || !is_scratch_src(r.src_file()))
            .cloned()
            .collect();
        if !kept.is_empty() {
            ordered = kept;
        }
        ordered.sort_by(|a, b| {
            let ia = if is_vault(a) { 2 } else { 0 };
            let ib = if is_vault(b) { 2 } else { 0 };
            ib.cmp(&ia).then(b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal))
        });
    }
    if opts.drop_superseded {
        ordered.retain(|r| r.trust.as_deref() != Some("superseded"));
    }
    let has_canon = ordered
        .iter()
        .take((opts.top_k * 2).max(6))
        .any(|r| r.trust.as_deref() == Some("canon"));
    if opts.drop_hearsay_if_canon && has_canon {
        let mut filtered = vec![];
        let mut synth_kept = 0usize;
        for r in &ordered {
            match r.trust.as_deref() {
                Some("hearsay") => continue,
                Some("synthesis") => {
                    if synth_kept >= opts.max_synthesis.max(1) {
                        continue;
                    }
                    synth_kept += 1;
                    filtered.push(r.clone());
                }
                _ => filtered.push(r.clone()),
            }
        }
        if !filtered.is_empty() {
            ordered = filtered;
        }
    }
    let mut best_by: HashMap<String, Hit> = HashMap::new();
    let mut best_hit: HashMap<String, f64> = HashMap::new();
    let key_of = |r: &Hit| -> String {
        let src = r.source_file.clone().unwrap_or_default();
        if ["manual", "grok", "api", "hermes", "session-extract", "tuanzi-distill", ""].contains(&src.as_str()) {
            format!("id:{}", r.id)
        } else {
            src
        }
    };
    for r in &ordered {
        let key = key_of(r);
        let hit = query_hit_score(&r.content, query);
        let score = r.score;
        if let Some(prev) = best_by.get(&key) {
            let prev_score = prev.score;
            if score > prev_score * 1.05 || (score >= prev_score * 0.95 && hit > *best_hit.get(&key).unwrap_or(&-1.0)) {
                best_by.insert(key.clone(), r.clone());
                best_hit.insert(key, hit);
            }
        } else {
            best_by.insert(key.clone(), r.clone());
            best_hit.insert(key, hit);
        }
    }
    let mut seen = HashSet::new();
    let mut diversified = vec![];
    for r in &ordered {
        let key = key_of(r);
        let chosen = match best_by.get(&key) {
            Some(c) if c.id == r.id => c,
            _ => continue,
        };
        if seen.contains(&key) {
            continue;
        }
        if opts.per_source <= 1 {
            seen.insert(key);
            diversified.push(chosen.clone());
        } else {
            diversified.push(r.clone());
        }
        if diversified.len() >= opts.top_k * 2 {
            break;
        }
    }
    let mut packed = vec![];
    for mut item in diversified.into_iter().take(opts.top_k) {
        let full = item.content.clone();
        let snippet = extract_snippet(&full, query, opts.max_chars);
        let mut abstract_s = item
            .abstract_out
            .clone()
            .or(item.abstract_text.clone())
            .or(item.content_preview.clone())
            .unwrap_or_default();
        abstract_s = abstract_s.trim().to_string();
        let layer = item.memory_layer.clone().unwrap_or_default();
        if !abstract_s.is_empty() && full.starts_with(&abstract_s) {
            if item.metadata.as_ref().and_then(|m| m.get("abstract")).is_none() {
                abstract_s.clear();
            }
        }
        let mut use_abs = false;
        if !abstract_s.is_empty() && opts.compact {
            let hit_abs = if query.is_empty() { 1.0 } else { query_hit_score(&abstract_s, query) };
            let hit_snip = if query.is_empty() { 0.0 } else { query_hit_score(&snippet, query) };
            if query.is_empty() || hit_abs >= hit_snip * 0.85 || hit_abs >= 1.0 {
                use_abs = true;
            }
        }
        if opts.compact {
            let mut body = if use_abs {
                abstract_s.chars().take(opts.max_chars).collect::<String>()
            } else {
                snippet
            };
            if !layer.is_empty() && use_abs && !body.starts_with('[') {
                body = format!("[{}] {body}", layer.chars().next().unwrap_or('?'));
            }
            let kept_created = item.created_at;
            item.full = Some(full.chars().take(6000).collect());
            item.dated_facts = Some(Value::Array(extract_dated_events(&full, kept_created, 12)));
            item.content_chars = Some(body.chars().count());
            item.full_chars = Some(full.chars().count());
            item.content = body;
            if !abstract_s.is_empty() {
                item.abstract_out = Some(abstract_s.chars().take(120).collect());
            }
            if !layer.is_empty() {
                item.memory_layer = Some(layer);
            }
            // 清噪声字段
            item.explain = None;
            item.bm25_score = None;
            item.vector_score = None;
            item.time_decay = None;
            item.authority_mult = None;
            item.level = None;
            item.node_type = None;
            item.node_name = None;
            item.project_name = None;
            item.location = None;
            item.metadata = None;
            if kept_created.is_some() {
                item.created_at = kept_created;
            }
            let sf = item.source_file.clone().unwrap_or_default();
            if let Some(rest) = sf.strip_prefix("vault:") {
                item.src = Some(rest.to_string());
            } else if !sf.is_empty() {
                item.src = Some(if sf.chars().count() <= 60 {
                    sf
                } else {
                    let tail: String = sf.chars().rev().take(57).collect::<String>().chars().rev().collect();
                    format!("…{tail}")
                });
            }
        } else {
            item.content = if full.chars().count() > opts.max_chars * 2 {
                snippet
            } else {
                full.clone()
            };
            item.content_chars = Some(item.content.chars().count());
            item.full_chars = Some(full.chars().count());
        }
        packed.push(item);
    }
    packed
}

pub fn results_token_stats(results: &[Hit]) -> Value {
    let mut total = 0usize;
    for r in results {
        total += r.content.chars().count();
        total += r.source_file.as_deref().or(r.src.as_deref()).unwrap_or("").chars().count();
        total += r.readback.as_deref().unwrap_or("").chars().count();
        total += 24;
    }
    let est = if results.is_empty() { 0 } else { (total as f64 / 2.2).max(1.0) as i64 };
    json!({"chars": total, "est_tokens": est, "count": results.len()})
}

pub fn format_ask_pack(query: &str, results: &[Hit], max_total: usize) -> String {
    let q: String = query.trim().chars().take(80).collect();
    let mut lines = vec![format!("【星枢】q={q} | hits={}", results.len())];
    let mut used = lines[0].chars().count();
    for (i, r) in results.iter().enumerate() {
        let trust = r.trust.as_deref().unwrap_or("?");
        let score_s = format!("{:.3}", r.score);
        let src = r.src.as_deref().or(r.source_file.as_deref()).unwrap_or("");
        let head = format!("{}. [{trust}|{score_s}] #{} {src}", i + 1, r.id);
        let body: String = r.content.replace('\n', " ").trim().to_string();
        let mut block = format!("{head}\n   {body}");
        if let Some(rb) = &r.readback {
            block.push_str(&format!("\n   → {rb}"));
        }
        if used + block.chars().count() + 1 > max_total {
            lines.push(format!("…截断，剩余 {} 条未展开", results.len() - i));
            break;
        }
        used += block.chars().count() + 1;
        lines.push(block);
    }
    lines.join("\n")
}

pub fn pin_exact_matches(query: &str, packed: Vec<Hit>) -> Vec<Hit> {
    if packed.is_empty() {
        return packed;
    }
    let mut keys = vec![];
    for m in pin_regex().find_iter(query) {
        keys.push(m.as_str().to_string());
    }
    let book = regex::Regex::new(r"《([^》]{2,40})》").unwrap();
    for c in book.captures_iter(query) {
        keys.push(c[1].to_string());
    }
    if keys.is_empty() {
        return packed;
    }
    let mut head = vec![];
    let mut rest = vec![];
    for mut r in packed {
        let blob = format!(
            "{} {}",
            r.source_file.as_deref().unwrap_or(""),
            r.src.as_deref().unwrap_or("")
        )
        .to_lowercase();
        if keys.iter().any(|k| blob.contains(&k.to_lowercase())) {
            r.pinned = Some(true);
            head.push(r);
        } else {
            rest.push(r);
        }
    }
    if head.is_empty() {
        rest
    } else {
        head.extend(rest);
        head
    }
}

fn improve_followups(query: &str, results: &[Hit], max_followups: usize) -> Vec<String> {
    if results.is_empty() {
        return vec![];
    }
    let tok_re = regex::Regex::new(r"[\w\u4e00-\u9fff]{2,}").unwrap();
    let q_tokens: HashSet<String> = tok_re.find_iter(&query.to_lowercase()).map(|m| m.as_str().to_string()).collect();
    let stop: HashSet<&str> = [
        "的", "了", "和", "是", "在", "与", "或", "什么", "怎么", "如何", "一下", "这个", "那个", "可以", "需要",
        "我们", "一个", "title", "chunk", "source", "path", "host", "read", "notes", "vault", "md", "txt",
        "json", "html", "http", "https", "com", "org", "the", "and", "for", "with", "from", "null", "true",
        "false", "none", "data", "file", "files", "line", "wiki", "link", "回读", "命令", "相关", "文档", "参见",
    ]
    .into_iter()
    .collect();
    let mut scores: HashMap<String, f64> = HashMap::new();
    let ext = regex::Regex::new(r"(?i)\.(md|txt|json|py|yml|yaml)$").unwrap();
    for r in results.iter().take(8) {
        let src = r.src.as_deref().or(r.source_file.as_deref()).unwrap_or("");
        let mut base = src.rsplit('/').next().unwrap_or("").to_string();
        base = ext.replace(&base, "").to_string();
        if base.chars().count() >= 3 && !stop.contains(base.to_lowercase().as_str()) {
            *scores.entry(base).or_insert(0.0) += 5.0;
        }
        let text = format!("{} {src}", r.content);
        for t in tok_re.find_iter(&text) {
            let t = t.as_str();
            let tl = t.to_lowercase();
            if stop.contains(tl.as_str()) || q_tokens.contains(&tl) || t.chars().all(|c| c.is_ascii_digit()) {
                continue;
            }
            if regex::Regex::new(r"^[a-z]{1,3}$").unwrap().is_match(&tl) {
                continue;
            }
            let mut w = 1.0;
            if ["禁止", "冻结", "权威", "故障", "端口", "架构", "skill", "agent", "phantun", "xray"]
                .iter()
                .any(|k| t.contains(k))
            {
                w = 3.0;
            }
            if t.chars().count() >= 4 {
                w *= 1.4;
            }
            if r.trust.as_deref() == Some("canon") {
                w *= 1.3;
            }
            *scores.entry(t.to_string()).or_insert(0.0) += w;
        }
    }
    let mut ranked: Vec<_> = scores.into_iter().collect();
    ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
    let mut followups = vec![];
    for (term, sc) in ranked {
        if sc < 2.0 || query.to_lowercase().contains(&term.to_lowercase()) || stop.contains(term.to_lowercase().as_str()) {
            continue;
        }
        let fq = format!("{query} {term}");
        if !followups.contains(&fq) {
            followups.push(fq);
        }
        if followups.len() >= max_followups {
            break;
        }
    }
    followups
}

fn detect_gaps(query: &str, packed: &[Hit]) -> Vec<String> {
    let mut gaps = vec![];
    if packed.is_empty() {
        return vec![query.into()];
    }
    let trusts: Vec<_> = packed.iter().map(|r| r.trust.as_deref().unwrap_or("")).collect();
    if !trusts.contains(&"canon") && !trusts.contains(&"source") {
        gaps.push(format!("{query} 权威 现行"));
    }
    let q = query.to_lowercase();
    let blob = packed
        .iter()
        .map(|r| format!("{}{}", r.content, r.src.as_deref().unwrap_or("")))
        .collect::<String>()
        .to_lowercase();
    if ["网关", "冻结", "代理", "phantun", "gateway"].iter().any(|k| q.contains(k))
        && !blob.contains("gateway")
        && !blob.contains("冻结")
    {
        gaps.push(format!("{query} GATEWAY_LOCK"));
    }
    if ["星枢", "记忆", "nebula"].iter().any(|k| q.contains(k))
        && !blob.contains("26670")
        && !blob.contains("sop")
        && !blob.contains("ask")
    {
        gaps.push(format!("{query} 星枢 SOP /ask"));
    }
    if packed[0].score < 0.02 {
        gaps.push(format!("{query} 结论 根因"));
    }
    gaps.truncate(2);
    gaps
}

pub fn reflect_ask(eng: &Engine, query: &str, opts: ReflectOpts) -> Result<Value> {
    let t0 = std::time::Instant::now();
    let mut all: HashMap<i64, Hit> = HashMap::new();
    let mut stages = vec![];
    let mut timing = json!({});
    let ingest = |all: &mut HashMap<i64, Hit>, raw: Vec<Hit>, hop: Value, mult: f64| {
        for mut r in raw {
            r.score *= mult;
            r.hop = Some(hop.clone());
            let e = all.entry(r.id).or_insert_with(|| r.clone());
            if r.score > e.score {
                *e = r;
            }
        }
    };
    let t1 = std::time::Instant::now();
    let raw1 = eng.search(
        query,
        SearchOpts {
            top_k: (opts.top_k * 3).max(10),
            category: opts.category.clone(),
            use_hybrid: opts.use_hybrid,
            enable_time_decay: true,
            temporal_intent: opts.temporal_intent.clone(),
            as_of: opts.as_of,
            tenant_id: opts.tenant_id.clone(),
            precomputed: opts.precomputed.clone(),
            ..Default::default()
        },
    )?;
    ingest(&mut all, raw1.clone(), json!(0), 1.0);
    timing["seed_ms"] = json!((t1.elapsed().as_secs_f64() * 1000.0 * 10.0).round() / 10.0);
    stages.push(json!({"stage":"seed","n": raw1.len(), "ms": timing["seed_ms"]}));

    let vis = ["图片", "照片", "截图", "这张图", "图里", "看图", "image", "photo", "screenshot"];
    let mut q2 = query.to_string();
    for k in vis {
        q2 = q2.replace(k, " ");
    }
    q2 = q2.split_whitespace().collect::<Vec<_>>().join(" ");
    if !q2.is_empty() && q2 != query {
        if let Ok(raw_img) = eng.search(
            &q2,
            SearchOpts {
                top_k: opts.top_k.max(4),
                category: Some("image".into()),
                use_hybrid: true,
                enable_time_decay: true,
                temporal_intent: opts.temporal_intent.clone(),
                as_of: opts.as_of,
                tenant_id: opts.tenant_id.clone(),
                precomputed: opts.precomputed.clone(),
                ..Default::default()
            },
        ) {
            let n = raw_img.len();
            ingest(&mut all, raw_img, json!(0), 1.25);
            stages.push(json!({"stage":"image_recall","q": q2, "n": n}));
        }
    }
    let seed_packed = pack_results(
        &all.values().cloned().collect::<Vec<_>>(),
        query,
        &PackOpts {
            top_k: opts.top_k.max(4),
            max_chars: opts.max_chars,
            per_source: 1,
            drop_superseded: true,
            drop_hearsay_if_canon: opts.drop_hearsay,
            compact: true,
            max_synthesis: opts.max_synthesis,
            prefer_layers: opts.prefer_layers.clone(),
        },
    );
    let followups = improve_followups(query, &seed_packed, 2);
    let gaps = detect_gaps(query, &seed_packed);
    let mut extra_q = vec![];
    for q in followups.iter().chain(gaps.iter()) {
        if q != query && !extra_q.contains(q) {
            extra_q.push(q.clone());
        }
    }
    let top_sc = seed_packed.first().map(|r| r.score).unwrap_or(0.0);
    let trusts: HashSet<_> = seed_packed.iter().filter_map(|r| r.trust.clone()).collect();
    let strong_seed = !seed_packed.is_empty()
        && (trusts.contains("canon") || (trusts.contains("source") && top_sc >= 0.008) || top_sc >= 0.025);
    if opts.hops >= 2 && !strong_seed {
        let t2 = std::time::Instant::now();
        let expand_qs: Vec<String> = extra_q.into_iter().take(2).collect();
        for q in &expand_qs {
            let mut raw = eng
                .bm25(
                    q,
                    &SearchOpts {
                        top_k: (opts.top_k * 2).max(8),
                        enable_time_decay: true,
                        temporal_intent: opts.temporal_intent.clone(),
                        as_of: opts.as_of,
                        category: opts.category.clone(),
                        tenant_id: opts.tenant_id.clone(),
                        ..Default::default()
                    },
                )
                .unwrap_or_default();
            for r in raw.iter_mut() {
                if r.score == 0.0 {
                    r.score = r.bm25_score.unwrap_or(0.0);
                }
            }
            if !raw.is_empty() {
                raw = apply_authority_boost(eng, raw, false).unwrap_or_default();
            }
            ingest(&mut all, raw, json!(1), 0.88);
        }
        timing["expand_ms"] = json!((t2.elapsed().as_secs_f64() * 1000.0 * 10.0).round() / 10.0);
        stages.push(json!({"stage":"expand_bm25","queries": expand_qs, "n": all.len(), "ms": timing["expand_ms"]}));
    } else if opts.hops >= 2 && strong_seed {
        stages.push(json!({"stage":"early_exit_strong_seed","top": top_sc, "trusts": trusts}));
    }
    let mut graph_added = 0i64;
    if opts.use_graph && opts.hops >= 2 && !strong_seed {
        let mut tops: Vec<_> = all.values().cloned().collect();
        tops.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap());
        for h in tops.into_iter().take(5) {
            if let Ok(Value::Array(rels)) = eng.related(h.id, 4) {
                for rel in rels {
                    let rid = rel.get("id").and_then(|x| x.as_i64()).unwrap_or(0);
                    if rid == 0 || all.contains_key(&rid) {
                        continue;
                    }
                    all.insert(
                        rid,
                        Hit {
                            id: rid,
                            content: rel.get("content").and_then(|x| x.as_str()).unwrap_or("").into(),
                            score: 0.015 * rel.get("weight").and_then(|x| x.as_f64()).unwrap_or(1.0),
                            trust: rel.get("trust").and_then(|x| x.as_str()).map(|s| s.into()),
                            importance: rel.get("importance").and_then(|x| x.as_f64()),
                            source_file: rel.get("source_file").and_then(|x| x.as_str()).map(|s| s.into()),
                            category: rel.get("category").and_then(|x| x.as_str()).map(|s| s.into()),
                            hop: Some(json!(2)),
                            ..Default::default()
                        },
                    );
                    graph_added += 1;
                }
            }
        }
        if graph_added > 0 {
            let boosted = apply_authority_boost(eng, all.values().cloned().collect(), false).unwrap_or_default();
            all = boosted.into_iter().map(|h| (h.id, h)).collect();
        }
        stages.push(json!({"stage":"graph","added": graph_added}));
    }
    let mut merged: Vec<_> = all.into_values().collect();
    merged.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap());
    let mut packed = pack_results(
        &merged,
        query,
        &PackOpts {
            top_k: opts.top_k,
            max_chars: opts.max_chars,
            per_source: 1,
            drop_superseded: true,
            drop_hearsay_if_canon: opts.drop_hearsay,
            compact: true,
            max_synthesis: opts.max_synthesis,
            prefer_layers: opts.prefer_layers.clone(),
        },
    );
    if !packed.is_empty() && packed.iter().all(|r| r.trust.as_deref() != Some("canon")) {
        let packed2 = pack_results(
            &merged,
            query,
            &PackOpts {
                top_k: opts.top_k,
                max_chars: opts.max_chars,
                drop_hearsay_if_canon: false,
                ..PackOpts {
                    top_k: opts.top_k,
                    max_chars: opts.max_chars,
                    per_source: 1,
                    drop_superseded: true,
                    drop_hearsay_if_canon: false,
                    compact: true,
                    max_synthesis: opts.max_synthesis,
                    prefer_layers: opts.prefer_layers.clone(),
                }
            },
        );
        if !packed2.is_empty() {
            packed = packed2;
        }
    }
    let pack_text = format_ask_pack(query, &packed, opts.max_total);
    let mut stats = results_token_stats(&packed);
    stats["pack_chars"] = json!(pack_text.chars().count());
    stats["pack_est_tokens"] = json!((pack_text.chars().count() as f64 / 2.2).max(1.0) as i64);
    let mut answer_hint = String::new();
    for r in &packed {
        if matches!(r.trust.as_deref(), Some("canon") | Some("source")) {
            answer_hint = r.content.chars().take(180).collect();
            break;
        }
    }
    if answer_hint.is_empty() {
        if let Some(r) = packed.first() {
            answer_hint = r.content.chars().take(180).collect();
        }
    }
    Ok(json!({
        "status": "ok",
        "query": query,
        "pack": pack_text,
        "results": packed,
        "count": packed.len(),
        "answer_hint": answer_hint,
        "followups": followups,
        "gaps": gaps,
        "stages": stages,
        "raw_candidates": merged.len(),
        "token_stats": stats,
        "elapsed_ms": (t0.elapsed().as_secs_f64()*1000.0*10.0).round()/10.0,
        "timing": timing,
        "engine": "reflect_v4",
        "hint": "优先读 pack；vault 结果用 readback 回读原文；trust=superseded 勿执行",
    }))
}

#[derive(Clone)]
pub struct ReflectOpts {
    pub top_k: usize,
    pub max_chars: usize,
    pub max_total: usize,
    pub use_hybrid: bool,
    pub category: Option<String>,
    pub use_graph: bool,
    pub hops: usize,
    pub drop_hearsay: bool,
    pub max_synthesis: usize,
    pub temporal_intent: Option<String>,
    pub as_of: Option<f64>,
    pub prefer_layers: Option<Vec<String>>,
    pub tenant_id: Option<String>,
    pub precomputed: Option<Vec<f32>>,
}

fn is_truth_src(src: &str, trust: &str) -> bool {
    src.starts_with("vault:")
        || src.starts_with("notes/")
        || src.starts_with("gateway/")
        || src.contains("/opt/gateway/")
        || trust == "canon"
}

fn query_tokens(query: &str) -> Vec<String> {
    let stop = ["的", "了", "和", "是", "在", "与", "或", "什么", "怎么", "如何", "一下", "这个", "那个", "一个", "能否", "能不能", "the", "and", "for", "how", "what"];
    let re = regex::Regex::new(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}").unwrap();
    let mut out = vec![];
    for t in re.find_iter(query) {
        let t = t.as_str();
        let tl = t.to_lowercase();
        if regex::Regex::new(r"^[\u4e00-\u9fff]+$").unwrap().is_match(t) && t.chars().count() > 2 {
            let ch: Vec<char> = t.chars().collect();
            for i in 0..ch.len().saturating_sub(1) {
                let bg: String = ch[i..i + 2].iter().collect();
                if !stop.contains(&bg.as_str()) && !out.contains(&bg) {
                    out.push(bg);
                }
            }
        } else if !stop.contains(&tl.as_str()) && !out.contains(&t.to_string()) {
            out.push(t.to_string());
        }
    }
    out
}

fn overlap_hits(query: &str, blob: &str) -> (i32, i32) {
    let toks = query_tokens(query);
    let b = blob.to_lowercase();
    let n = toks.iter().filter(|t| b.contains(&t.to_lowercase())).count() as i32;
    (n, toks.len() as i32)
}

pub fn compose_answer(query: &str, packed: &[Hit]) -> Value {
    if packed.is_empty() {
        return json!({
            "answer": "未命中可靠记忆。请扩大 query，或回读 L1 权威原文。",
            "confidence": 0.0, "executable": false, "evidence": [], "warnings": ["empty_results"]
        });
    }
    let mut evidence = vec![];
    let mut warnings = vec![];
    let mut has_canon = false;
    let mut has_super = false;
    for r in packed {
        let trust = r.trust.as_deref().unwrap_or("synthesis");
        if trust == "canon" {
            has_canon = true;
        }
        if trust == "superseded" {
            has_super = true;
            continue;
        }
        evidence.push(json!({
            "id": r.id, "trust": trust, "score": r.score, "rerank_score": r.rerank_score,
            "src": r.src.as_deref().or(r.source_file.as_deref()),
            "readback": r.readback, "snippet": r.content.chars().take(220).collect::<String>(),
        }));
    }
    let mut pick = evidence.first().cloned().unwrap_or(json!({}));
    let src = pick.get("src").and_then(|x| x.as_str()).unwrap_or("");
    let tr = pick.get("trust").and_then(|x| x.as_str()).unwrap_or("");
    if !is_truth_src(src, tr) {
        let mut truth = vec![];
        for ev in &evidence {
            let s = ev.get("src").and_then(|x| x.as_str()).unwrap_or("");
            let t = ev.get("trust").and_then(|x| x.as_str()).unwrap_or("");
            if !is_truth_src(s, t) {
                continue;
            }
            let blob = format!("{} {s}", ev.get("snippet").and_then(|x| x.as_str()).unwrap_or(""));
            let (hits, ntok) = overlap_hits(query, &blob);
            let need = if ntok >= 5 { 3 } else if ntok >= 3 { 2 } else { 1 };
            if ntok == 0 || hits >= need {
                let mut e = ev.clone();
                e["_hits"] = json!(hits);
                truth.push(e);
            }
        }
        if !truth.is_empty() {
            truth.sort_by(|a, b| {
                let ka = (
                    a.get("rerank_score").and_then(|x| x.as_f64()).unwrap_or(0.0),
                    a.get("_hits").and_then(|x| x.as_i64()).unwrap_or(0) as f64,
                    a.get("score").and_then(|x| x.as_f64()).unwrap_or(0.0),
                );
                let kb = (
                    b.get("rerank_score").and_then(|x| x.as_f64()).unwrap_or(0.0),
                    b.get("_hits").and_then(|x| x.as_i64()).unwrap_or(0) as f64,
                    b.get("score").and_then(|x| x.as_f64()).unwrap_or(0.0),
                );
                kb.partial_cmp(&ka).unwrap_or(std::cmp::Ordering::Equal)
            });
            pick = truth[0].clone();
        }
    }
    let top_snip = pick
        .get("snippet")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .replace('\n', " ");
    let answer: String = top_snip.trim().chars().take(600).collect();
    let top_trust = pick.get("trust").and_then(|x| x.as_str()).unwrap_or("synthesis");
    let (mut conf, mut executable) = if top_trust == "canon" {
        (0.88, true)
    } else if top_trust == "source" {
        warnings.push("top_is_source");
        (0.65, true)
    } else if packed.first().and_then(|r| r.rerank_score).is_some() {
        warnings.push("top_synthesis_reranked");
        (0.55, true)
    } else {
        warnings.push("top_low_trust");
        (0.40, false)
    };
    let _ = (&mut conf, &mut executable);
    if has_canon && top_trust != "canon" {
        warnings.push("canon_present_not_top");
    }
    if has_super {
        warnings.push("superseded_present_filtered");
    }
    let readbacks: Vec<Value> = evidence.iter().filter_map(|e| e.get("readback").cloned()).take(3).collect();
    json!({
        "answer": if answer.is_empty() { "（有命中但正文为空）".into() } else { answer },
        "confidence": if top_trust == "canon" { 0.88 } else if top_trust == "source" { 0.65 } else if packed.first().and_then(|r| r.rerank_score).is_some() { 0.55 } else { 0.40 },
        "executable": top_trust == "canon" || top_trust == "source" || packed.first().and_then(|r| r.rerank_score).is_some(),
        "evidence": evidence.into_iter().take(6).collect::<Vec<_>>(),
        "readbacks": readbacks,
        "warnings": warnings,
        "query": query,
    })
}

fn needs_llm_deep(packed: &[Hit], reflect_meta: &Value) -> bool {
    if packed.is_empty() {
        return true;
    }
    let top = packed[0].score;
    let trusts: Vec<_> = packed.iter().filter_map(|r| r.trust.as_deref()).collect();
    if (trusts.contains(&"canon") || trusts.contains(&"source")) && top >= 0.01 {
        return false;
    }
    if top < 0.008 {
        return true;
    }
    if reflect_meta.get("gaps").and_then(|g| g.as_array()).map(|a| !a.is_empty()).unwrap_or(false)
        && !trusts.contains(&"canon")
        && top < 0.015
    {
        return true;
    }
    false
}

pub async fn ultimate_ask(eng: &Engine, query: &str, mut opts: ReflectOpts, llm_deep: &str, llm_answer: bool, rerank: bool, reader: bool) -> Result<Value> {
    let t0 = std::time::Instant::now();
    let rerank = if !crate::rerank::enabled() { false } else { rerank };
    let wide_k = if rerank { (opts.top_k + 2).max(6) } else { opts.top_k };
    let orig_k = opts.top_k;
    opts.top_k = wide_k;
    opts.max_synthesis = if rerank { 3 } else { 1 };
    let base = reflect_ask(eng, query, opts.clone())?;
    let mut packed: Vec<Hit> = serde_json::from_value(base["results"].clone()).unwrap_or_default();
    let mut stages: Vec<Value> = base["stages"].as_array().cloned().unwrap_or_default();
    let mut deep_used = false;
    let mut deep_queries = vec![];
    let do_deep = llm_deep == "on" || (llm_deep == "auto" && needs_llm_deep(&packed, &base));
    if do_deep {
        deep_queries = crate::llm::rewrite_queries(&format!("{query} 权威 现行"), 2).await;
        let mut all: HashMap<i64, Hit> = packed.iter().cloned().map(|h| (h.id, h)).collect();
        for q in &deep_queries {
            let mut raw = eng
                .bm25(
                    q,
                    &SearchOpts {
                        top_k: (orig_k * 2).max(8),
                        enable_time_decay: true,
                        temporal_intent: opts.temporal_intent.clone(),
                        as_of: opts.as_of,
                        category: opts.category.clone(),
                        tenant_id: opts.tenant_id.clone(),
                        ..Default::default()
                    },
                )
                .unwrap_or_default();
            for r in raw.iter_mut() {
                if r.score == 0.0 {
                    r.score = r.bm25_score.unwrap_or(0.0);
                }
            }
            raw = apply_authority_boost(eng, raw, false).unwrap_or_default();
            for mut r in raw {
                r.score *= 0.9;
                r.hop = Some(json!("llm_deep"));
                let e = all.entry(r.id).or_insert_with(|| r.clone());
                if r.score > e.score {
                    *e = r;
                }
            }
        }
        let mut merged: Vec<_> = all.into_values().collect();
        merged.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap());
        packed = pack_results(
            &merged,
            query,
            &PackOpts {
                top_k: wide_k,
                max_chars: opts.max_chars,
                per_source: 1,
                drop_superseded: true,
                drop_hearsay_if_canon: true,
                compact: true,
                max_synthesis: if rerank { 3 } else { 1 },
                prefer_layers: opts.prefer_layers.clone(),
            },
        );
        deep_used = true;
        stages.push(json!({"stage":"llm_deep","queries": deep_queries, "n": merged.len()}));
    }
    let mut rerank_used = false;
    if rerank {
        if packed.len() > 1 && !seed_already_answers(query, &packed[0]) {
            let n = packed.len();
            apply_rerank(query, &mut packed, n).await;
            rerank_used = packed.iter().any(|r| r.rerank_score.is_some());
            packed.truncate(orig_k);
            if rerank_used {
                stages.push(json!({"stage":"rerank","backend":"qwen3-rerank","model": std::env::var("NEBULA_RERANK_MODEL").unwrap_or_else(|_| "qwen3-rerank".into()), "n": packed.len()}));
            }
        } else {
            if let Some(r) = packed.first_mut() {
                r.rerank_skip = Some("seed_answers".into());
            }
            packed.truncate(orig_k);
            if packed.first().and_then(|r| r.rerank_skip.as_ref()).is_some() {
                stages.push(json!({"stage":"rerank_skip","reason":"seed_answers"}));
            }
        }
    } else {
        packed.truncate(orig_k);
    }
    let before: Vec<i64> = packed.iter().take(3).map(|r| r.id).collect();
    packed = pin_exact_matches(query, packed);
    let after: Vec<i64> = packed.iter().take(3).map(|r| r.id).collect();
    if before != after {
        stages.push(json!({"stage":"pin_exact","keys": true, "moved": true}));
    }
    let pack_text = format_ask_pack(query, &packed, opts.max_total);
    let mut stats = results_token_stats(&packed);
    stats["pack_chars"] = json!(pack_text.chars().count());
    stats["pack_est_tokens"] = json!((pack_text.chars().count() as f64 / 2.2).max(1.0) as i64);
    let mut composed = compose_answer(query, &packed);
    if let Some(ti) = &opts.temporal_intent {
        composed["temporal_intent"] = json!(ti);
    }
    if let Some(a) = opts.as_of {
        composed["as_of"] = json!(a);
    }
    let mut polished: Option<String> = None;
    if reader {
        let mut events = vec![];
        for r in packed.iter().take(6) {
            if let Some(Value::Array(df)) = &r.dated_facts {
                events.extend(df.iter().cloned());
            }
        }
        polished = solve_temporal(query, &events);
        if polished.is_none() {
            let ev: Vec<String> = packed
                .iter()
                .take(6)
                .map(|r| r.full.as_deref().unwrap_or(&r.content).chars().take(3500).collect())
                .collect();
            let fact: String = events
                .iter()
                .take(16)
                .map(|e| format!("- {} | {}", e.get("date").and_then(|x| x.as_str()).unwrap_or(""), e.get("sent").and_then(|x| x.as_str()).unwrap_or("")))
                .collect::<Vec<_>>()
                .join("\n");
            let system = "You answer using ONLY Dated facts and Evidence. For which-first / which-earlier: pick the event with the earlier date. For how-many-days: subtract the two dates and reply like '7 days'. Short phrase only. No preamble. If a dated fact is related, you must answer — do not say you don't know.";
            let user = format!("Question: {query}\n\nDated facts:\n{fact}\n\nEvidence:\n{}", ev.join("\n---\n"));
            let t = crate::llm::llm_chat(system, &user, 400, 0.0, 12.0).await;
            if !t.is_empty() {
                polished = Some(t);
            }
        }
        if polished.is_some() {
            composed["reader"] = json!(true);
        }
    } else if llm_answer
        && composed.get("executable").and_then(|x| x.as_bool()).unwrap_or(false)
        && composed.get("confidence").and_then(|x| x.as_f64()).unwrap_or(0.0) >= 0.6
    {
        let ev_lines: Vec<String> = composed["evidence"]
            .as_array()
            .unwrap_or(&vec![])
            .iter()
            .take(5)
            .map(|e| {
                format!(
                    "#{}[{}] {}",
                    e.get("id").unwrap_or(&json!(0)),
                    e.get("trust").and_then(|x| x.as_str()).unwrap_or(""),
                    e.get("snippet").and_then(|x| x.as_str()).unwrap_or("").chars().take(160).collect::<String>()
                )
            })
            .collect();
        let t = crate::llm::llm_chat(
            "你是记忆裁决助手。只根据证据写 2-4 句中文结论。禁止编造证据没有的内容。若证据冲突，标明以 canon/GATEWAY 为准。输出纯结论，不要开场白。",
            &format!("问题：{query}\n证据：\n{}", ev_lines.join("\n")),
            280,
            0.2,
            8.0,
        )
        .await;
        if !t.is_empty() {
            polished = Some(t);
        }
    }
    if let Some(p) = polished {
        composed["answer_llm"] = json!(p);
        composed["answer_final"] = json!(p);
        stages.push(json!({"stage":"llm_answer"}));
    } else {
        composed["answer_final"] = composed.get("answer").cloned().unwrap_or(json!(""));
    }
    let contract = format!(
        "【星枢v5裁决】q={} conf={:.2} exec={}\n结论：{}",
        query.chars().take(60).collect::<String>(),
        composed["confidence"].as_f64().unwrap_or(0.0),
        composed["executable"].as_bool().unwrap_or(false),
        composed["answer_final"].as_str().unwrap_or("")
    );
    let mut contract_lines = vec![contract];
    if let Some(arr) = composed["readbacks"].as_array() {
        if !arr.is_empty() {
            let s: Vec<&str> = arr.iter().filter_map(|x| x.as_str()).take(2).collect();
            contract_lines.push(format!("回读：{}", s.join(" | ")));
        }
    }
    if let Some(w) = composed["warnings"].as_array() {
        if !w.is_empty() {
            contract_lines.push(format!(
                "警告：{}",
                w.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(",")
            ));
        }
    }
    Ok(json!({
        "status": "ok",
        "query": query,
        "pack": pack_text,
        "contract": contract_lines.join("\n"),
        "answer": composed.get("answer_final"),
        "composed": composed,
        "results": packed,
        "count": packed.len(),
        "followups": base.get("followups"),
        "gaps": base.get("gaps"),
        "deep_queries": deep_queries,
        "llm_deep_used": deep_used,
        "rerank_used": rerank_used,
        "stages": stages,
        "raw_candidates": base.get("raw_candidates"),
        "token_stats": stats,
        "elapsed_ms": (t0.elapsed().as_secs_f64()*1000.0*10.0).round()/10.0,
        "timing": base.get("timing"),
        "engine": "ultimate_v5",
        "version": crate::types::RELEASE,
        "hint": "优先用 contract 或 pack；executable=false 时不要当事实执行；vault 必须 readback",
    }))
}

pub async fn bootstrap(eng: &Engine, focus: &str, budget: usize) -> Result<Value> {
    let t0 = std::time::Instant::now();
    let header = "【L0星枢】权威：GATEWAY_LOCK>虎虎笔记>星枢chunk；密钥走 Vaultwarden；网络默认冻结。检索用 /ask engine=ultimate。\n";
    let fq = if focus.trim().is_empty() {
        "虎虎现行架构 星枢用法 工作流"
    } else {
        focus.trim()
    };
    let ult = ultimate_ask(
        eng,
        fq,
        ReflectOpts {
            top_k: 4,
            max_chars: 220,
            max_total: (1600).min(budget.saturating_sub(header.chars().count() + 200)),
            use_hybrid: true,
            category: None,
            use_graph: false,
            hops: 1,
            drop_hearsay: true,
            max_synthesis: 1,
            temporal_intent: None,
            as_of: None,
            prefer_layers: None,
            tenant_id: None,
            precomputed: None,
        },
        "off",
        false,
        false,
        false,
    )
    .await?;
    let pack = ult.get("pack").and_then(|x| x.as_str()).unwrap_or("");
    let contract = ult.get("contract").and_then(|x| x.as_str()).unwrap_or("");
    let remain = budget.saturating_sub(header.chars().count()).max(400);
    let block: String = format!("{contract}\n{pack}").chars().take(remain).collect();
    let mut text = format!("{header}{block}");
    if text.chars().count() > budget {
        text = text.chars().take(budget.saturating_sub(1)).collect::<String>() + "…";
    }
    Ok(json!({
        "status": "ok",
        "bootstrap": text,
        "chars": text.chars().count(),
        "est_tokens": (text.chars().count() as f64 / 2.2).max(1.0) as i64,
        "meta": {
            "engine": "bootstrap_v5",
            "fast": true,
            "layer_hint": "pack prefer abstract",
            "focus": fq,
            "token_stats": ult.get("token_stats"),
            "confidence": ult.pointer("/composed/confidence"),
            "ult_elapsed_ms": ult.get("elapsed_ms"),
        },
        "elapsed_ms": (t0.elapsed().as_secs_f64()*1000.0*10.0).round()/10.0,
        "version": crate::types::RELEASE,
    }))
}
