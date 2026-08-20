//! 百炼 qwen3-rerank。失败保持原序。

use crate::types::Hit;
use parking_lot::Mutex;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::time::{Duration, Instant};

const DEFAULT_URL: &str = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank";
const DEFAULT_INSTRUCT: &str = "Given a web search query, retrieve relevant passages that answer the query.";

pub fn enabled() -> bool {
    !matches!(
        std::env::var("NEBULA_RERANK").unwrap_or_else(|_| "1".into()).to_lowercase().as_str(),
        "0" | "false" | "off" | "no"
    )
}

fn model() -> String {
    std::env::var("NEBULA_RERANK_MODEL").unwrap_or_else(|_| "qwen3-rerank".into())
}
fn url() -> String {
    std::env::var("NEBULA_RERANK_URL").unwrap_or_else(|_| DEFAULT_URL.into())
}
fn api_key() -> String {
    std::env::var("BAILIAN_API_KEY")
        .ok()
        .or_else(|| std::env::var("DASHSCOPE_API_KEY").ok())
        .unwrap_or_default()
}

struct Cache {
    map: HashMap<(String, String), (Vec<f32>, Instant)>,
}
impl Cache {
    fn new() -> Self {
        Self { map: HashMap::new() }
    }
}

static CACHE: Mutex<Option<Cache>> = Mutex::new(None);

fn cache() -> parking_lot::MutexGuard<'static, Option<Cache>> {
    let mut g = CACHE.lock();
    if g.is_none() {
        *g = Some(Cache::new());
    }
    g
}

fn proper_nouns(query: &str) -> Vec<String> {
    let re = regex::Regex::new(r"[A-Za-z][A-Za-z0-9_-]{2,}").unwrap();
    let mut out = vec![];
    for cap in re.find_iter(query) {
        let tok = cap.as_str();
        if matches!(tok.to_lowercase().as_str(), "the" | "and" | "for" | "how" | "what" | "where") {
            continue;
        }
        if !out.iter().any(|x: &String| x == tok) {
            out.push(tok.to_string());
        }
        if out.len() >= 8 {
            break;
        }
    }
    out
}

fn query_window(text: &str, query: &str, max_chars: usize) -> String {
    let text: String = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= max_chars {
        return text;
    }
    let tokens: Vec<String> = regex::Regex::new(r"[\w\u4e00-\u9fff]{2,}")
        .unwrap()
        .find_iter(&query.to_lowercase())
        .map(|m| m.as_str().to_string())
        .filter(|t| !["的", "了", "和", "是", "在", "与", "或", "什么", "怎么", "如何", "一下", "这个", "那个", "一个"].contains(&t.as_str()))
        .collect();
    let mut best_i = 0usize;
    let mut best = -1i32;
    let step = (max_chars / 5).max(20);
    let limit = chars.len().saturating_sub(max_chars) + 1;
    let mut i = 0;
    while i < limit {
        let chunk: String = chars[i..chars.len().min(i + max_chars)].iter().collect::<String>().to_lowercase();
        let mut sc = 0i32;
        for tok in &tokens {
            if chunk.contains(tok) {
                sc += chunk.matches(tok).count() as i32 * if tok.chars().count() >= 6 { 4 } else { 1 };
            }
        }
        if sc > best {
            best = sc;
            best_i = i;
        }
        i += step;
    }
    chars[best_i..chars.len().min(best_i + max_chars)].iter().collect()
}

fn candidate_card(r: &Hit, query: &str) -> String {
    let src = r.source_file.as_deref().or(r.src.as_deref()).unwrap_or("");
    let src_s = src.rsplit('/').next().unwrap_or("-");
    let raw = if !r.content.is_empty() {
        r.content.as_str()
    } else {
        r.abstract_out.as_deref().or(r.content_preview.as_deref()).unwrap_or("")
    };
    let body = query_window(raw, query, 260);
    let stub = raw.get(..220.min(raw.len())).unwrap_or("").contains("nonce=")
        || raw.trim_start().starts_with("# 独立主题-");
    let blob = format!("{} {}", body, raw.chars().take(400).collect::<String>()).to_lowercase();
    let missing: Vec<_> = proper_nouns(query)
        .into_iter()
        .filter(|t| !blob.contains(&t.to_lowercase()))
        .collect();
    let mut tags = vec![];
    if stub {
        tags.push("[STUB]".to_string());
    }
    if !missing.is_empty() {
        tags.push(format!("[缺:{}]", missing.into_iter().take(3).collect::<Vec<_>>().join(",")));
    }
    let tag = if tags.is_empty() {
        String::new()
    } else {
        format!("{} ", tags.join(" "))
    };
    format!("{tag}file={src_s} | {body}")
}

fn slot_bonus(query: &str, text: &str, source: &str) -> f64 {
    let src = source.rsplit('/').next().unwrap_or("").to_lowercase();
    let mut b = 0.0;
    if ["目录", "路径", "哪改", "哪个目录"].iter().any(|w| query.contains(w)) {
        if regex::Regex::new(r"/(?:home|opt|etc|usr)/\S+").unwrap().is_match(text) {
            b += 3.0;
        }
        if regex::Regex::new(r"(?i)systemd|\.service").unwrap().is_match(text)
            && !regex::Regex::new(r"/(?:home|opt)/").unwrap().is_match(text)
        {
            b -= 2.0;
        }
    }
    if ["能不能", "能否", "改代理", "冻结"].iter().any(|w| query.contains(w)) {
        if text.contains("禁止") || text.contains("默认禁止") {
            b += 2.5;
        } else if text.contains("GATEWAY_LOCK") && !text.contains("禁止") {
            b -= 1.0;
        }
        if src.contains("gateway_lock.md") {
            b += 3.0;
        }
    }
    if query.contains("模型") || query.contains("重排") {
        if regex::Regex::new(r"qwen3-rerank|MiniMax-M3|bge-reranker|改为 LLM|LLM 重排")
            .unwrap()
            .is_match(text)
        {
            b += 2.5;
        }
    }
    for tok in proper_nouns(query) {
        if tok.len() >= 4 && src.contains(&tok.to_lowercase()) {
            b += 3.0;
            break;
        }
    }
    b
}

pub fn seed_already_answers(query: &str, r: &Hit) -> bool {
    let text = if !r.content.is_empty() {
        r.content.as_str()
    } else {
        r.abstract_out.as_deref().unwrap_or("")
    };
    let src = r.source_file.as_deref().or(r.src.as_deref()).unwrap_or("");
    if text.is_empty() {
        return false;
    }
    let blob = format!("{text} {src}").to_lowercase();
    let nouns = proper_nouns(query);
    if !nouns.is_empty() && nouns.iter().any(|n| !blob.contains(&n.to_lowercase())) {
        return false;
    }
    if ["目录", "路径", "哪个目录"].iter().any(|w| query.contains(w)) {
        return regex::Regex::new(r"/(?:home|opt|etc)/").unwrap().is_match(text);
    }
    if ["能不能", "能否", "改代理", "冻结"].iter().any(|w| query.contains(w)) {
        return text.contains("禁止") || text.contains("默认禁止");
    }
    if query.contains("模型") || query.contains("重排") {
        return regex::Regex::new(r"qwen3-rerank|MiniMax-M3|bge-reranker|改为 LLM")
            .unwrap()
            .is_match(text);
    }
    if query.to_lowercase().contains("vaultwarden") || query.contains("密码本") {
        let tl = text.to_lowercase();
        return tl.contains("vaultwarden")
            && ["bw-", "vault.lan", "密码本", "密码库"].iter().any(|x| tl.contains(x));
    }
    false
}

async fn rerank_scores(query: &str, docs: &[String]) -> Vec<f32> {
    let n = docs.len();
    if n == 0 {
        return vec![];
    }
    if n == 1 {
        return vec![1.0];
    }
    if !enabled() || api_key().is_empty() {
        return (0..n).map(|i| (n - i) as f32 / n as f32).collect();
    }
    let qh = {
        let mut h = Sha256::new();
        h.update(query.as_bytes());
        hex::encode(h.finalize())[..16].to_string()
    };
    let ch = {
        let mut h = Sha256::new();
        h.update(docs.join("\n").as_bytes());
        hex::encode(h.finalize())[..16].to_string()
    };
    {
        let mut g = cache();
        if let Some(c) = g.as_mut() {
            if let Some((s, ts)) = c.map.get(&(qh.clone(), ch.clone())) {
                if ts.elapsed() < Duration::from_secs(600) {
                    return s.clone();
                }
            }
        }
    }
    let mut payload = json!({
        "model": model(),
        "input": {"query": query, "documents": docs},
        "parameters": {"top_n": n, "return_documents": false},
    });
    if model().starts_with("qwen3") {
        payload["parameters"]["instruct"] = json!(
            std::env::var("NEBULA_RERANK_INSTRUCT").unwrap_or_else(|_| DEFAULT_INSTRUCT.into())
        );
    }
    let client = match reqwest::Client::builder().timeout(Duration::from_secs(8)).build() {
        Ok(c) => c,
        Err(_) => return (0..n).map(|i| (n - i) as f32 / n as f32).collect(),
    };
    let resp = client
        .post(url())
        .header("Authorization", format!("Bearer {}", api_key()))
        .json(&payload)
        .send()
        .await;
    let fallback: Vec<f32> = (0..n).map(|i| (n - i) as f32 / n as f32).collect();
    let Ok(resp) = resp else {
        return fallback;
    };
    let Ok(data): Result<Value, _> = resp.json().await else {
        return fallback;
    };
    let rows = data
        .get("results")
        .cloned()
        .or_else(|| data.pointer("/output/results").cloned())
        .unwrap_or(Value::Null);
    let mut scores = vec![0.0f32; n];
    if let Some(arr) = rows.as_array() {
        for row in arr {
            if let (Some(idx), Some(sc)) = (row.get("index").and_then(|x| x.as_i64()), row.get("relevance_score").and_then(|x| x.as_f64())) {
                if idx >= 0 && (idx as usize) < n {
                    scores[idx as usize] = sc as f32;
                }
            }
        }
    } else {
        return fallback;
    }
    {
        let mut g = cache();
        if let Some(c) = g.as_mut() {
            if c.map.len() > 128 {
                c.map.clear();
            }
            c.map.insert((qh, ch), (scores.clone(), Instant::now()));
        }
    }
    scores
}

pub async fn apply_rerank(query: &str, results: &mut Vec<Hit>, candidates: usize) {
    if results.len() <= 1 || !enabled() {
        return;
    }
    let actual_k = candidates.max(2).min(results.len());
    let head: Vec<Hit> = results[..actual_k].to_vec();
    let texts: Vec<String> = head.iter().map(|r| candidate_card(r, query)).collect();
    let scores = rerank_scores(query, &texts).await;
    let max_vec = head.iter().map(|r| r.score).fold(0.0f64, f64::max).max(1e-9);
    let mut head = head;
    for (i, r) in head.iter_mut().enumerate() {
        let s = scores.get(i).copied().unwrap_or(0.0) as f64;
        let mut penalty = if r.trust.as_deref() == Some("superseded") { 100.0 } else { 0.0 };
        let blob = if !r.content.is_empty() {
            r.content.clone()
        } else {
            r.abstract_out.clone().unwrap_or_default()
        };
        let head220: String = blob.chars().take(220).collect();
        if head220.contains("nonce=") || blob.trim_start().starts_with("# 独立主题-") {
            penalty += 5.0;
        }
        let miss = proper_nouns(query)
            .into_iter()
            .any(|t| !blob.to_lowercase().contains(&t.to_lowercase()));
        if miss && !proper_nouns(query).is_empty() {
            // 与 Python：缺专名 +4
            let missing = proper_nouns(query)
                .into_iter()
                .filter(|t| !blob.to_lowercase().contains(&t.to_lowercase()))
                .count();
            if missing > 0 {
                penalty += 4.0;
            }
        }
        let slot = slot_bonus(query, &blob, r.source_file.as_deref().or(r.src.as_deref()).unwrap_or(""));
        let vec = 2.5 * (r.score / max_vec);
        r.rerank_raw = Some((s * 10000.0).round() / 10000.0);
        r.rerank_score = Some(((s * 10.0 - penalty + slot + vec) * 10000.0).round() / 10000.0);
    }
    head.sort_by(|a, b| {
        b.rerank_score
            .unwrap_or(0.0)
            .partial_cmp(&a.rerank_score.unwrap_or(0.0))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    for (i, h) in head.into_iter().enumerate() {
        results[i] = h;
    }
}

pub fn status_json() -> Value {
    json!({
        "loaded": !api_key().is_empty(),
        "enabled": enabled(),
        "backend": "qwen3-rerank",
        "device": "bailian",
        "model": model(),
        "chat_url": std::env::var("NEBULA_RERANK_URL").unwrap_or_default(),
    })
}
