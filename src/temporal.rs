//! 时间意图、日期抽取、衰减。对齐 vector_memory.py。

use chrono::{Datelike, NaiveDate, Utc};
use regex::Regex;
use serde_json::{json, Value};
use std::collections::HashSet;
use std::sync::OnceLock;

const PRESENT: &[&str] = &[
    "现在", "当前", "最新", "最近", "此刻", "now", "currently", "latest", "right now",
];
const PAST: &[&str] = &[
    "去年", "之前", "以前", "当时", "曾经", "过去", "去年的", "last year", "previously",
    "used to", "in the past", "before",
];
const FUTURE: &[&str] = &[
    "下周", "计划", "即将", "未来", "下次", "next week", "upcoming", "will ", "going to",
    "plan to",
];

pub fn classify_temporal_intent(query: &str) -> String {
    let q = query;
    let ql = query.to_lowercase();
    if PAST.iter().any(|k| q.contains(k) || ql.contains(k)) {
        return "past".into();
    }
    if FUTURE.iter().any(|k| q.contains(k) || ql.contains(k)) {
        return "future".into();
    }
    if PRESENT.iter().any(|k| q.contains(k) || ql.contains(k)) {
        return "present".into();
    }
    "neutral".into()
}

pub fn parse_as_of(val: Option<&Value>) -> Option<f64> {
    let v = val?;
    match v {
        Value::Null => None,
        Value::Number(n) => n.as_f64(),
        Value::String(s) if s.is_empty() => None,
        Value::String(s) => {
            if let Ok(f) = s.parse::<f64>() {
                return Some(f);
            }
            for (fmt, n) in [("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)] {
                let slice = if s.len() >= n { &s[..n] } else { s };
                if let Ok(ndt) = chrono::NaiveDateTime::parse_from_str(
                    if fmt.contains("%H") {
                        slice
                    } else {
                        return NaiveDate::parse_from_str(slice, "%Y-%m-%d")
                            .ok()
                            .and_then(|d| d.and_hms_opt(0, 0, 0))
                            .map(|dt| dt.and_utc().timestamp() as f64);
                    },
                    "%Y-%m-%d %H:%M:%S",
                ) {
                    return Some(ndt.and_utc().timestamp() as f64);
                }
                let _ = fmt;
            }
            NaiveDate::parse_from_str(&s[..s.len().min(10)], "%Y-%m-%d")
                .ok()
                .and_then(|d| d.and_hms_opt(0, 0, 0))
                .map(|dt| dt.and_utc().timestamp() as f64)
        }
        _ => None,
    }
}

pub fn temporal_weight(
    created_at: Option<f64>,
    intent: &str,
    as_of: Option<f64>,
    enable: bool,
    lambda_neutral: f64,
) -> f64 {
    if !enable {
        return 1.0;
    }
    let Some(ca) = created_at else { return 1.0 };
    let now = as_of.unwrap_or_else(|| now_ts());
    let days = ((now - ca) / 86400.0).max(0.0);
    match intent {
        "past" => 1.0 + 0.15 * (1.0 + days).ln(),
        "future" => 1.0,
        "present" => (-0.08 * days).exp(),
        _ => (-lambda_neutral * days).exp(),
    }
}

pub fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn date_span() -> &'static Regex {
    static RX: OnceLock<Regex> = OnceLock::new();
    RX.get_or_init(|| {
        Regex::new(
            r"(?i)(?:\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2}(?:/\d{2,4})?|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?)",
        )
        .unwrap()
    })
}

pub fn extract_dated_events(text: &str, _as_of: Option<f64>, max_facts: usize) -> Vec<Value> {
    if text.is_empty() {
        return vec![];
    }
    let mut events = vec![];
    let mut seen = HashSet::new();
    for m in date_span().find_iter(text) {
        let date_s = m.as_str().split_whitespace().collect::<Vec<_>>().join(" ");
        let a = m.start();
        let b = m.end();
        let start_hint = text.get(..a).and_then(|h| h.rfind('.')).map(|i| (i + 1).max(a.saturating_sub(90))).unwrap_or(a.saturating_sub(90));
        let start = text.floor_char_boundary(start_hint.min(text.len()));
        let end_hint = text.get(b..).and_then(|t| t.find('.')).map(|i| (b + i).min(text.len())).unwrap_or((b + 140).min(text.len()));
        let end = text.ceil_char_boundary(end_hint.min(text.len())).max(start);
        let sent: String = text[start..end]
            .trim_matches(|c: char| c == ' ' || c == '\n' || c == '-' || c == '—')
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        if sent.len() < 8 {
            continue;
        }
        let sent_key: String = sent.chars().take(72).collect();
        let key = format!("{}|{}", date_s.to_lowercase(), sent_key);
        if !seen.insert(key) {
            continue;
        }
        let ts = parse_date_token(&date_s);
        events.push(json!({"date": date_s, "ts": ts, "sent": sent.chars().take(180).collect::<String>()}));
        if events.len() >= max_facts {
            break;
        }
    }
    events
}

fn parse_date_token(raw: &str) -> Option<f64> {
    let s = raw.replace("st", "").replace("nd", "").replace("rd", "").replace("th", "");
    let s = s.trim().trim_matches(|c| c == ',' || c == ' ');
    if let Ok(d) = NaiveDate::parse_from_str(s, "%Y-%m-%d") {
        return d.and_hms_opt(0, 0, 0).map(|dt| dt.and_utc().timestamp() as f64);
    }
    if let Ok(d) = NaiveDate::parse_from_str(s, "%Y/%m/%d") {
        return d.and_hms_opt(0, 0, 0).map(|dt| dt.and_utc().timestamp() as f64);
    }
    None
}

pub fn classify_temporal_op(query: &str) -> String {
    let q = query.to_lowercase();
    if ["how many days", "how many day", "多少天", "几天", "隔了", "隔几天"]
        .iter()
        .any(|k| q.contains(k))
    {
        return "days_between".into();
    }
    if ["which", "哪个", "哪次"].iter().any(|k| q.contains(k))
        && ["first", "earlier", "earliest", "先", "更早", "最先"]
            .iter()
            .any(|k| q.contains(k))
    {
        return "which_first".into();
    }
    if ["which", "哪个"].iter().any(|k| q.contains(k))
        && ["last", "later", "latest", "最晚", "最后"]
            .iter()
            .any(|k| q.contains(k))
    {
        return "which_last".into();
    }
    String::new()
}

pub fn solve_temporal(query: &str, events: &[Value]) -> Option<String> {
    let op = classify_temporal_op(query);
    if op.is_empty() {
        return None;
    }
    let dated: Vec<&Value> = events.iter().filter(|e| e.get("ts").and_then(|t| t.as_f64()).is_some()).collect();
    if dated.is_empty() {
        return None;
    }
    // 简化：first/last 按 ts
    if op == "which_first" {
        let mut v = dated.clone();
        v.sort_by(|a, b| {
            a["ts"].as_f64().partial_cmp(&b["ts"].as_f64()).unwrap_or(std::cmp::Ordering::Equal)
        });
        return v.first().and_then(|e| e.get("sent")).and_then(|s| s.as_str()).map(|s| s.chars().take(80).collect());
    }
    if op == "which_last" {
        let mut v = dated.clone();
        v.sort_by(|a, b| {
            a["ts"].as_f64().partial_cmp(&b["ts"].as_f64()).unwrap_or(std::cmp::Ordering::Equal)
        });
        return v.last().and_then(|e| e.get("sent")).and_then(|s| s.as_str()).map(|s| s.chars().take(80).collect());
    }
    if op == "days_between" && dated.len() >= 2 {
        let mut ts: Vec<f64> = dated.iter().filter_map(|e| e["ts"].as_f64()).collect();
        ts.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let days = ((ts[ts.len() - 1] - ts[0]) / 86400.0).abs() as i64;
        return Some(format!("{days} days"));
    }
    None
}

pub fn unix_from_ymd(s: &str) -> Option<f64> {
    NaiveDate::parse_from_str(s, "%Y-%m-%d")
        .ok()
        .and_then(|d| d.and_hms_opt(0, 0, 0))
        .map(|dt| dt.and_utc().timestamp() as f64)
}

// 压 unused warning
#[allow(dead_code)]
fn _utc_now_year() -> i32 {
    Utc::now().year()
}
