//! MiniMax / 百炼兼容 chat。

use serde_json::{json, Value};
use std::time::Duration;

pub async fn llm_chat(system: &str, user: &str, max_tokens: u32, temperature: f64, timeout_s: f64) -> String {
    let api_key = std::env::var("NEBULA_LLM_API_KEY")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| std::env::var("BAILIAN_API_KEY").ok())
        .or_else(|| std::env::var("DASHSCOPE_API_KEY").ok())
        .unwrap_or_default();
    if api_key.is_empty() {
        return String::new();
    }
    let mut base = std::env::var("BAILIAN_CHAT_URL").unwrap_or_default();
    if base.trim().is_empty() {
        base = "https://api.minimaxi.com/v1/chat/completions".into();
    }
    let model = std::env::var("NEBULA_LLM_MODEL").unwrap_or_else(|_| "MiniMax-M3".into());
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs_f64(timeout_s.max(1.0)))
        .build()
    {
        Ok(c) => c,
        Err(_) => return String::new(),
    };
    let body = json!({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    });
    let resp = match client
        .post(&base)
        .header("Authorization", format!("Bearer {api_key}"))
        .json(&body)
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!("llm fail: {e}");
            return String::new();
        }
    };
    if !resp.status().is_success() {
        tracing::warn!("llm http {}", resp.status());
        return String::new();
    }
    let val: Value = match resp.json().await {
        Ok(v) => v,
        Err(_) => return String::new(),
    };
    let msg = val.pointer("/choices/0/message").cloned().unwrap_or(Value::Null);
    let mut text = extract_text(&msg["content"]);
    if text.is_empty() {
        text = extract_text(&msg["reasoning_content"]);
    }
    strip_think(&text)
}

fn extract_text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Array(a) => a
            .iter()
            .map(|p| {
                p.get("text")
                    .or_else(|| p.get("content"))
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string()
            })
            .collect(),
        _ => String::new(),
    }
}

pub fn strip_think(s: &str) -> String {
    let re = regex::Regex::new(r"(?is)<think[^>]*>.*?</[^>]*think[^>]*>").unwrap();
    let t = re.replace_all(s, "");
    let t = if t.to_lowercase().contains("<think") {
        t.split("</think>").last().unwrap_or(&t).to_string()
    } else {
        t.to_string()
    };
    t.trim().to_string()
}

pub async fn rewrite_queries(query: &str, n: usize) -> Vec<String> {
    let sys = format!(
        "你是一个查询改写助手。用户会给一个搜索查询，你需要生成{n}个不同角度的同义查询，用于向量搜索。每个查询一行，不要编号，不要解释。保持简洁，中文为主。"
    );
    let text = llm_chat(&sys, query, 200, 0.7, 10.0).await;
    if text.is_empty() {
        return vec![query.to_string()];
    }
    let junk = ["抱歉", "我不", "建议", "没有", "无法", "不清楚", "不知道"];
    let mut rewrites: Vec<String> = text
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty() && l != query && !junk.iter().any(|p| l.contains(p)))
        .collect();
    rewrites.insert(0, query.to_string());
    rewrites.truncate(n.max(1));
    if rewrites.len() < 2 {
        vec![query.to_string()]
    } else {
        rewrites
    }
}
