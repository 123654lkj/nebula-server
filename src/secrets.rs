//! Vaultwarden 桥：明文只走 bw CLI，星枢只存指针。

use anyhow::{anyhow, Result};
use regex::Regex;
use serde_json::{json, Value};
use std::process::Command;

const PATTERNS: &[(&str, &str)] = &[
    (r"sk-[A-Za-z0-9]{20,}", "openai_like_sk"),
    (r"sk-ant-[A-Za-z0-9\-_]{20,}", "anthropic_key"),
    (r"ghp_[A-Za-z0-9]{20,}", "github_pat"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "github_pat"),
    (r"AKIA[0-9A-Z]{16}", "aws_access_key"),
    (r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----", "private_key_pem"),
    (r#"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['"][^'"]{12,}['"]"#, "kv_secret"),
    (r"(?i)Bearer\s+[A-Za-z0-9\-_\.]{20,}", "bearer_token"),
];

pub fn scan_secrets(text: &str) -> Vec<String> {
    if text.is_empty() {
        return vec![];
    }
    let mut hits = vec![];
    for (p, label) in PATTERNS {
        if let Ok(rx) = Regex::new(p) {
            if rx.is_match(text) {
                hits.push((*label).into());
            }
        }
    }
    hits
}

fn session_path() -> Option<String> {
    let cands = [
        std::env::var("BW_SESSION_FILE").unwrap_or_default(),
        "/tmp/bw-session-huhu".into(),
        format!("/tmp/bw-session-{}", std::env::var("USER").unwrap_or_else(|_| "root".into())),
    ];
    for p in cands {
        if p.is_empty() {
            continue;
        }
        if let Ok(m) = std::fs::metadata(&p) {
            if m.is_file() && m.len() > 10 {
                return Some(p);
            }
        }
    }
    None
}

fn bw_session() -> Option<String> {
    let p = session_path()?;
    std::fs::read_to_string(p).ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

fn bw_env() -> Vec<(String, String)> {
    let mut out = vec![];
    let app = std::env::var("BITWARDENCLI_APPDATA_DIR")
        .unwrap_or_else(|_| "/home/huhu/.config/Bitwarden CLI".into());
    if std::path::Path::new(&app).is_dir() {
        out.push(("BITWARDENCLI_APPDATA_DIR".into(), app));
    }
    if let Some(s) = bw_session() {
        out.push(("BW_SESSION".into(), s));
    }
    for ca in [
        "/etc/ssl/certs/zcode-network-ca.pem",
        "/home/huhu/.zcode/v2/certs/zcode-network-ca.pem",
    ] {
        if std::path::Path::new(ca).is_file() {
            out.push(("NODE_EXTRA_CA_CERTS".into(), ca.into()));
            break;
        }
    }
    out
}

fn bw(args: &[&str], timeout_secs: u64) -> Result<String> {
    let bin = std::env::var("BW_BIN").unwrap_or_else(|_| "bw".into());
    let mut cmd = Command::new(bin);
    cmd.args(["--nointeraction"]).args(args);
    for (k, v) in bw_env() {
        cmd.env(k, v);
    }
    if let Some(s) = bw_session() {
        if !args.iter().any(|a| *a == "--session") {
            cmd.args(["--session", &s]);
        }
    }
    // 简单超时：交给 OS；调用方短超时
    let _ = timeout_secs;
    let out = cmd.output()?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        return Err(anyhow!("bw failed: {}", err.chars().take(300).collect::<String>()));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

pub fn bw_status() -> Value {
    match bw(&["status"], 15) {
        Ok(out) if !out.is_empty() => {
            let mut data: Value = serde_json::from_str(&out).unwrap_or(json!({"raw": out}));
            if let Some(o) = data.as_object_mut() {
                o.insert("ok".into(), json!(o.get("status").and_then(|x| x.as_str()) == Some("unlocked")));
                o.insert("session_file".into(), json!(session_path()));
                o.insert("server".into(), o.get("serverUrl").cloned().unwrap_or(Value::Null));
                o.insert("policy".into(), json!("明文只存 Vaultwarden；星枢仅 vaultwarden 指针"));
            }
            data
        }
        Ok(_) => json!({"ok": false, "status": "error", "session_file": session_path()}),
        Err(e) => json!({"ok": false, "status": "error", "detail": e.to_string(), "session_file": session_path()}),
    }
}

pub fn list_items(limit: usize) -> Result<Vec<Value>> {
    let out = bw(&["list", "items"], 30)?;
    let items: Vec<Value> = serde_json::from_str(&out).unwrap_or_default();
    Ok(items
        .into_iter()
        .take(limit)
        .map(|it| {
            json!({
                "name": it.get("name").and_then(|x| x.as_str()).unwrap_or(""),
                "id": it.get("id").and_then(|x| x.as_str()).unwrap_or(""),
                "username": it.pointer("/login/username").and_then(|x| x.as_str()).unwrap_or(""),
                "folderId": it.get("folderId").and_then(|x| x.as_str()).unwrap_or(""),
            })
        })
        .collect())
}

pub fn get_password(name: &str) -> Result<String> {
    bw(&["get", "password", name], 20)
}
pub fn get_username(name: &str) -> Result<String> {
    bw(&["get", "username", name], 20)
}

pub fn create_login_item(name: &str, username: &str, password: &str, notes: &str) -> Result<Value> {
    for it in list_items(500)? {
        if it.get("name").and_then(|x| x.as_str()) == Some(name) {
            return Err(anyhow!("条目已存在: {name}（请换名或先 bw-ai rm）"));
        }
    }
    let payload = json!({
        "type": 1, "name": name, "notes": notes,
        "login": {"username": username, "password": password},
    });
    let bin = std::env::var("BW_BIN").unwrap_or_else(|_| "bw".into());
    let mut cmd = Command::new(bin);
    cmd.args(["--nointeraction", "create", "item"]);
    for (k, v) in bw_env() {
        cmd.env(&k, &v);
    }
    if let Some(s) = bw_session() {
        cmd.args(["--session", &s]);
    }
    cmd.stdin(std::process::Stdio::piped());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());
    let mut child = cmd.spawn()?;
    if let Some(mut stdin) = child.stdin.take() {
        use std::io::Write;
        stdin.write_all(payload.to_string().as_bytes())?;
    }
    let out = child.wait_with_output()?;
    if !out.status.success() {
        return Err(anyhow!(
            "{}",
            String::from_utf8_lossy(&out.stderr).chars().take(300).collect::<String>()
        ));
    }
    let data: Value = serde_json::from_slice(&out.stdout).unwrap_or(json!({}));
    Ok(json!({"name": name, "id": data.get("id"), "username": username}))
}

pub fn pointer_content(name: &str, purpose: &str, username: &str) -> String {
    let purpose = if purpose.is_empty() { "密钥/API" } else { purpose };
    format!(
        "[vaultwarden指针] item={name}\n用途: {purpose}\n用户名: {}\n取用: bw-ai get {name}  或  POST :26670/secrets/get {{\"name\":\"{name}\",\"reveal\":true}}\n禁止: 把明文密钥写入笔记/星枢/prompt\n更新: {}",
        if username.is_empty() { "(见密码本)" } else { username },
        chrono::Local::now().format("%Y-%m-%d")
    )
}

pub fn resolve_for_query(query: &str, items: &[Value]) -> Vec<Value> {
    let q_raw = query;
    let q = query.to_lowercase();
    let mut scored = vec![];
    for it in items {
        let name = it.get("name").and_then(|x| x.as_str()).unwrap_or("");
        let nl = name.to_lowercase();
        let mut score = 0i32;
        if !name.is_empty() && (q_raw.contains(name) || q.contains(&nl)) {
            score += 12;
        }
        let zh = regex::Regex::new(r"[\u4e00-\u9fff]{2,}").unwrap();
        for part in zh.find_iter(name) {
            if q_raw.contains(part.as_str()) {
                score += 8;
            }
        }
        for part in regex::Regex::new(r"[\s\-_/]+").unwrap().split(&nl) {
            if part.len() >= 3 && q.contains(part) {
                score += 3;
            }
        }
        if q.contains("api") && (nl.starts_with("api-") || nl.contains("api")) {
            score += 1;
        }
        if (q.contains("ssh") || q_raw.contains("SSH")) && nl.starts_with("ssh-") {
            score += 1;
            for kw in ["虎虎", "huhu", "团子", "tuanzi", "仙兔", "锐客", "绿云"] {
                if q_raw.contains(kw) && name.contains(kw) {
                    score += 10;
                }
                if q.contains(&kw.to_lowercase()) && nl.contains(&kw.to_lowercase()) {
                    score += 10;
                }
            }
        }
        if q.contains("github") && nl.contains("github") {
            score += 3;
        }
        if q_raw.contains("智谱") || q.contains("zhipu") || q.contains("glm") {
            if nl.contains("zhipu") || nl.contains("glm") {
                score += 6;
            }
        }
        if score > 0 {
            scored.push((score, it));
        }
    }
    scored.sort_by(|a, b| b.0.cmp(&a.0));
    scored
        .into_iter()
        .take(8)
        .map(|(_, it)| {
            let name = it.get("name").and_then(|x| x.as_str()).unwrap_or("");
            json!({
                "name": name,
                "username": it.get("username").and_then(|x| x.as_str()).unwrap_or(""),
                "how": format!("bw-ai get {name}"),
                "api": format!("POST /secrets/get {{\"name\":\"{name}\",\"reveal\":true}}"),
            })
        })
        .collect()
}
