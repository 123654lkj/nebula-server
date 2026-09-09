//! 站点规则：canon / hearsay / 回读。对齐 nebula_site.py，全走环境变量。

use regex::Regex;
use std::sync::OnceLock;

fn csv(name: &str, default: &str) -> Vec<String> {
    std::env::var(name)
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| default.to_string())
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

fn canon_substr() -> Vec<String> {
    csv(
        "NEBULA_CANON_SUBSTR",
        "vault:gateway/,vault:notes/01-,vault:notes/02-,notes/HOME.md,notes/PROJECT.md,团子学习/",
    )
}
fn demote_substr() -> Vec<String> {
    csv("NEBULA_DEMOTE_SUBSTR", "/04-,/05-,notes/04-,notes/05-")
}
fn hearsay_substr() -> Vec<String> {
    csv(
        "NEBULA_HEARSAY_SUBSTR",
        "SESSIONS/,session-extract,minimax-auto-sync,.zcode/obsidian/,openclaw/workspace/memory",
    )
}
fn scratch_substr() -> Vec<String> {
    csv(
        "NEBULA_SCRATCH_SUBSTR",
        "session-extract,minimax-auto-sync,.zcode/obsidian/,openclaw/workspace/memory,/home/xiantuer/.openclaw/",
    )
}

pub fn pin_regex() -> &'static Regex {
    static RX: OnceLock<Regex> = OnceLock::new();
    RX.get_or_init(|| {
        let pat = std::env::var("NEBULA_PIN_REGEX")
            .unwrap_or_else(|_| r"[A-Za-z0-9._-]{3,}\.md".into());
        Regex::new(&format!("(?i){pat}"))
            .unwrap_or_else(|_| Regex::new(r"(?i)[A-Za-z0-9._-]{3,}\.md").unwrap())
    })
}

pub fn is_canon_src(src: &str) -> bool {
    canon_substr().iter().any(|x| !x.is_empty() && src.contains(x))
}
pub fn is_demote_src(src: &str) -> bool {
    demote_substr().iter().any(|x| !x.is_empty() && src.contains(x))
}
pub fn is_hearsay_src(src: &str) -> bool {
    hearsay_substr().iter().any(|x| !x.is_empty() && src.contains(x))
}
pub fn is_scratch_src(src: &str) -> bool {
    if src.starts_with("vault:") {
        return false;
    }
    is_hearsay_src(src) || scratch_substr().iter().any(|x| !x.is_empty() && src.contains(x))
}
pub fn is_vault_src(src: &str) -> bool {
    src.starts_with("vault:") || src.starts_with("notes/")
}

pub fn format_readback(src: &str) -> Option<String> {
    if !src.starts_with("vault:") {
        return None;
    }
    let path = &src["vault:".len()..];
    let mut logical = path.to_string();
    let map = csv("NEBULA_PATH_MAP", "");
    for spec in map {
        if let Some((pre, dest)) = spec.split_once('=') {
            if path.starts_with(pre) {
                logical = if dest.contains("{suffix}") {
                    dest.replace("{suffix}", &path[pre.len()..])
                } else {
                    format!("{}/{}", dest.trim_end_matches('/'), path)
                };
                break;
            }
        }
    }
    let fmt = std::env::var("NEBULA_READBACK_FMT").unwrap_or_default();
    if fmt.trim().is_empty() {
        Some(format!("vault:{path}"))
    } else {
        Some(fmt.replace("{path}", &logical))
    }
}

pub fn require_tenant() -> bool {
    matches!(
        std::env::var("NEBULA_REQUIRE_TENANT")
            .unwrap_or_default()
            .to_lowercase()
            .as_str(),
        "1" | "true" | "yes"
    )
}
pub fn default_tenant() -> String {
    std::env::var("NEBULA_DEFAULT_TENANT").unwrap_or_default()
}
pub fn resolve_tenant(explicit: Option<&str>) -> String {
    let t = explicit.unwrap_or("").trim();
    if !t.is_empty() {
        t.to_string()
    } else {
        default_tenant()
    }
}

pub fn api_token() -> String {
    std::env::var("NEBULA_API_TOKEN").unwrap_or_default()
}
