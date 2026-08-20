//! Obsidian 笔记 → 星枢 原生增量同步（对齐 scripts/vault_to_nebula_sync.py v1.2）。
//! 全部行为可配置：根目录 / 白名单 / 排除 / 体积帽 / 分类规则 / 定时器。

use crate::engine::Engine;
use crate::site::format_readback;
use crate::temporal::now_ts;
use anyhow::{anyhow, Result};
use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

static SYNC_RUNNING: AtomicBool = AtomicBool::new(false);

fn env_str(key: &str, default: &str) -> String {
    std::env::var(key).ok().filter(|s| !s.trim().is_empty()).unwrap_or_else(|| default.into())
}
fn env_num<T: std::str::FromStr>(key: &str, default: T) -> T {
    std::env::var(key).ok().and_then(|s| s.parse().ok()).unwrap_or(default)
}
fn env_csv(key: &str, default: &str) -> Vec<String> {
    env_str(key, default)
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// 笔记根目录：NEBULA_VAULT_ROOT → 旧现网路径（存在才用）→ ./vault/notes。
pub fn vault_root() -> PathBuf {
    if let Ok(p) = std::env::var("NEBULA_VAULT_ROOT") {
        if !p.trim().is_empty() {
            return PathBuf::from(p);
        }
    }
    let legacy = PathBuf::from("/home/huhu/obsidian-vault/notes");
    if legacy.is_dir() {
        return legacy;
    }
    PathBuf::from("./vault/notes")
}

const DEFAULT_INCLUDE: &str = "\
团子学习/_WORKBOOK-40.md,团子学习/_DASHBOARD.md,团子学习/_METRICS.md,团子学习/_LIBRARY-MAP.md,\
团子学习/_PLAYABLE-摘录.md,团子学习/_CANON-DELTA-*.md,团子学习/记忆系统/_merged-*.md,\
团子学习/记忆系统/Agent-记忆工作手册*.md,团子学习/记忆系统/*Memory*.md,团子学习/记忆系统/*memory*.md,\
团子学习/记忆系统/*Mem*.md,团子学习/记忆系统/*RAG*.md,团子学习/记忆系统/*rag*.md,\
团子学习/编码方法论/Ponytail*.md,团子学习/本地沉淀Skill/*.md,团子学习/趋势洞察/*趋势汇总*.md,\
00-元信息/*.md,01-用户画像/*.md,02-密码与安全/*.md,03-API与模型/*.md,04-项目笔记/*.md,\
05-临时记录/*.md,06-Agent会话提炼/*.md,HOME.md,PROJECT.md";

pub struct VaultCfg {
    pub root: PathBuf,
    pub state_file: PathBuf,
    pub key_prefix: String,
    pub wrap_label: String,
    pub max_bytes: u64,
    pub chunk_size: usize,
    pub overlap: usize,
    pub include: Vec<String>,
    pub exclude_dirs: Vec<String>,
    pub exclude_names: Vec<String>,
    pub rules: Vec<Rule>,
}

pub struct Rule {
    pub contains: Vec<String>,
    pub category: Option<String>,
    pub importance: Option<f64>,
}

fn default_rules() -> Vec<Rule> {
    let mk = |c: &[&str], cat: Option<&str>, imp: Option<f64>| Rule {
        contains: c.iter().map(|s| s.to_string()).collect(),
        category: cat.map(|s| s.to_string()),
        importance: imp,
    };
    vec![
        mk(&["GATEWAY_LOCK", "NETWORK-FROZEN", "phantun", "网关"], Some("infrastructure"), Some(0.95)),
        mk(&["HOME.md", "PROJECT.md"], Some("infrastructure"), Some(0.95)),
        mk(&["_WORKBOOK-40", "Agent-记忆工作手册"], Some("lesson"), Some(0.93)),
        mk(&["01-用户画像"], Some("identity"), Some(0.9)),
        mk(&["USER.md", "MEMORY.md", "AGENT操作手册"], None, Some(0.9)),
        mk(&["02-密码", "Vaultwarden"], Some("security"), None),
        mk(&["_merged-", "记忆系统"], Some("lesson"), Some(0.88)),
        mk(&["03-API"], Some("infrastructure"), None),
        mk(&["04-项目笔记"], Some("project"), Some(0.85)),
        mk(&["05-临时"], Some("fact"), Some(0.55)),
        mk(&["00-元信息"], None, Some(0.7)),
        mk(&["团子学习", "WORKBOOK", "CANON"], Some("lesson"), None),
        mk(&["infra/", "gateway/"], Some("infrastructure"), None),
    ]
}

fn parse_rules(raw: &str) -> Vec<Rule> {
    let Ok(Value::Array(items)) = serde_json::from_str::<Value>(raw) else {
        tracing::warn!("NEBULA_VAULT_RULES 不是 JSON 数组，忽略");
        return vec![];
    };
    items
        .iter()
        .filter_map(|it| {
            let contains: Vec<String> = match it.get("contains") {
                Some(Value::String(s)) => vec![s.clone()],
                Some(Value::Array(a)) => a.iter().filter_map(|x| x.as_str().map(String::from)).collect(),
                _ => return None,
            };
            if contains.is_empty() {
                return None;
            }
            Some(Rule {
                contains,
                category: it.get("category").and_then(|x| x.as_str()).map(String::from),
                importance: it.get("importance").and_then(|x| x.as_f64()),
            })
        })
        .collect()
}

impl VaultCfg {
    pub fn load(eng: &Engine) -> Self {
        let root = vault_root();
        let state_default = eng
            .db_path
            .parent()
            .unwrap_or(Path::new("."))
            .join("vault-sync-state.json");
        let mut rules = vec![];
        if let Ok(raw) = std::env::var("NEBULA_VAULT_RULES") {
            if !raw.trim().is_empty() {
                rules = parse_rules(&raw);
            }
        }
        rules.extend(default_rules()); // 用户规则在前，默认规则兜底
        Self {
            root,
            state_file: PathBuf::from(env_str(
                "NEBULA_VAULT_STATE",
                &state_default.to_string_lossy(),
            )),
            key_prefix: env_str("NEBULA_VAULT_KEY_PREFIX", "notes/"),
            wrap_label: env_str("NEBULA_VAULT_WRAP_LABEL", "[笔记]"),
            max_bytes: env_num("NEBULA_VAULT_MAX_BYTES", 120_000),
            chunk_size: env_num("NEBULA_VAULT_CHUNK", 900),
            overlap: env_num("NEBULA_VAULT_OVERLAP", 100),
            include: env_csv("NEBULA_VAULT_INCLUDE", DEFAULT_INCLUDE),
            exclude_dirs: env_csv("NEBULA_VAULT_EXCLUDE_DIRS", "团子学习/,_归档/,.obsidian/,.trash/"),
            exclude_names: env_csv("NEBULA_VAULT_EXCLUDE_NAMES", "gmail-最新邮件,主密码哈希"),
            rules,
        }
    }

    fn source_key(&self, rel: &str) -> String {
        format!("vault:{}{}", self.key_prefix, rel)
    }

    fn category_for(&self, hay: &str) -> String {
        self.rules
            .iter()
            .find(|r| r.category.is_some() && r.contains.iter().any(|c| hay.contains(c.as_str())))
            .and_then(|r| r.category.clone())
            .unwrap_or_else(|| "fact".into())
    }

    fn importance_for(&self, hay: &str) -> f64 {
        self.rules
            .iter()
            .find(|r| r.importance.is_some() && r.contains.iter().any(|c| hay.contains(c.as_str())))
            .and_then(|r| r.importance)
            .unwrap_or(0.75)
    }
}

/// fnmatch 语义的简易 glob（* 跨 /，? 单字符）。
pub fn glob_match(rel: &str, pattern: &str) -> bool {
    let mut rx = String::from("^");
    for ch in pattern.chars() {
        match ch {
            '*' => rx.push_str(".*"),
            '?' => rx.push('.'),
            c => rx.push_str(&regex::escape(&c.to_string())),
        }
    }
    rx.push('$');
    Regex::new(&rx).map(|r| r.is_match(rel)).unwrap_or(false)
}

fn in_excluded_dir(rel: &str, dirs: &[String]) -> bool {
    let hay = format!("/{rel}");
    dirs.iter().any(|d| {
        let t = d.trim_matches('/');
        !t.is_empty() && hay.contains(&format!("/{t}/"))
    })
}

fn collect_files(cfg: &VaultCfg) -> Vec<(PathBuf, String)> {
    let mut out = vec![];
    fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
        let Ok(rd) = std::fs::read_dir(dir) else { return };
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                walk(&p, out);
            } else if p.extension().and_then(|x| x.to_str()).map(|x| x.eq_ignore_ascii_case("md")) == Some(true) {
                out.push(p);
            }
        }
    }
    let mut files = vec![];
    walk(&cfg.root, &mut files);
    for p in files {
        let Ok(rel) = p.strip_prefix(&cfg.root) else { continue };
        let rel = rel.to_string_lossy().replace('\\', "/");
        let name = p.file_name().unwrap_or_default().to_string_lossy().to_string();
        if cfg.exclude_names.iter().any(|s| name.contains(s.as_str())) {
            continue;
        }
        let size = p.metadata().map(|m| m.len()).unwrap_or(0);
        if size == 0 || size > cfg.max_bytes {
            continue;
        }
        let whitelisted = cfg.include.iter().any(|g| glob_match(&rel, g));
        if !whitelisted && in_excluded_dir(&rel, &cfg.exclude_dirs) {
            continue;
        }
        out.push((p, rel));
    }
    out.sort_by(|a, b| a.1.cmp(&b.1));
    out
}

fn strip_frontmatter(text: &str) -> (String, Map<String, Value>) {
    let mut meta = Map::new();
    if let Some(rest) = text.strip_prefix("---") {
        if let Some(end) = rest.find("\n---") {
            let fm = &rest[..end];
            let body = rest[end + 4..].trim_start_matches(['\r', '\n']).to_string();
            for line in fm.lines() {
                if let Some((k, v)) = line.split_once(':') {
                    meta.insert(
                        k.trim().to_string(),
                        json!(v.trim().trim_matches(|c| c == '"' || c == '\'')),
                    );
                }
            }
            return (body, meta);
        }
    }
    (text.to_string(), meta)
}

fn extract_title(rel: &str, body: &str, meta: &Map<String, Value>) -> String {
    if let Some(t) = meta.get("title").and_then(|x| x.as_str()) {
        if !t.is_empty() {
            return t.to_string();
        }
    }
    for line in body.lines() {
        if let Some(h) = line.trim().strip_prefix("# ") {
            return h.trim().to_string();
        }
    }
    Path::new(rel)
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| rel.to_string())
}

fn hard_split(text: &str, chunk_size: usize, overlap: usize) -> Vec<String> {
    let n_chars = text.chars().count();
    if n_chars <= chunk_size {
        return vec![text.to_string()];
    }
    // 句末字符后断句
    let mut sents: Vec<String> = vec![];
    let mut cur = String::new();
    for ch in text.chars() {
        cur.push(ch);
        if matches!(ch, '。' | '！' | '？' | '.' | '!' | '?' | '\n' | '；' | ';') {
            sents.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        sents.push(cur);
    }
    let mut out = vec![];
    let mut buf: Vec<String> = vec![];
    let mut n = 0usize;
    for s in sents {
        let sl = s.chars().count();
        if n + sl > chunk_size && !buf.is_empty() {
            out.push(buf.join("").trim().to_string());
            let mut keep: Vec<String> = vec![];
            let mut kn = 0;
            for x in buf.iter().rev() {
                let xl = x.chars().count();
                if kn + xl > overlap {
                    break;
                }
                keep.insert(0, x.clone());
                kn += xl;
            }
            n = keep.iter().map(|x| x.chars().count()).sum();
            buf = keep;
        }
        n += sl;
        buf.push(s);
    }
    if !buf.is_empty() {
        out.push(buf.join("").trim().to_string());
    }
    out.into_iter().filter(|c| !c.is_empty()).collect()
}

/// 标题感知分块：先按 1-3 级标题切段，段内聚合，超限硬切。
pub fn split_chunks(text: &str, chunk_size: usize, overlap: usize) -> Vec<String> {
    let re = Regex::new(r"(?m)^#{1,3} ").unwrap();
    let mut idx: Vec<usize> = re.find_iter(text).map(|m| m.start()).collect();
    if idx.first() != Some(&0) {
        idx.insert(0, 0);
    }
    idx.push(text.len());
    let mut chunks: Vec<String> = vec![];
    let mut buf = String::new();
    for w in idx.windows(2) {
        let part = text[w[0]..w[1]].trim();
        if part.is_empty() {
            continue;
        }
        let pl = part.chars().count();
        if buf.chars().count() + pl + 1 <= chunk_size {
            if buf.is_empty() {
                buf = part.to_string();
            } else {
                buf = format!("{buf}\n\n{part}");
            }
        } else {
            if !buf.is_empty() {
                chunks.extend(hard_split(&buf, chunk_size, overlap));
            }
            if pl <= chunk_size {
                buf = part.to_string();
            } else {
                chunks.extend(hard_split(part, chunk_size, overlap));
                buf = String::new();
            }
        }
    }
    if !buf.is_empty() {
        chunks.extend(hard_split(&buf, chunk_size, overlap));
    }
    chunks.into_iter().filter(|c| !c.trim().is_empty()).collect()
}

fn wrap_chunk(cfg: &VaultCfg, abs: &Path, key: &str, title: &str, idx: usize, total: usize, body: &str) -> String {
    let rel = key.strip_prefix("vault:").unwrap_or(key);
    let readback = format_readback(key)
        .filter(|s| s != key)
        .unwrap_or_else(|| format!("read \"{}\"", abs.to_string_lossy().replace('\\', "/")));
    format!(
        "{} path={rel} source={key} title={title} chunk={}/{total}\n回读命令: {readback}\n---\n{}",
        cfg.wrap_label,
        idx + 1,
        body.trim()
    )
}

fn load_state(cfg: &VaultCfg) -> Value {
    std::fs::read_to_string(&cfg.state_file)
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_else(|| json!({"files": {}}))
}

fn save_state(cfg: &VaultCfg, state: &Value) -> Result<()> {
    if let Some(dir) = cfg.state_file.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let tmp = cfg.state_file.with_extension("tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(state)?)?;
    std::fs::rename(&tmp, &cfg.state_file)?;
    Ok(())
}

fn fingerprint(p: &Path) -> Result<(String, u64, f64)> {
    let bytes = std::fs::read(p)?;
    let mut h = Sha256::new();
    h.update(&bytes);
    let meta = p.metadata()?;
    let mtime = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    Ok((hex::encode(h.finalize()), meta.len(), mtime))
}

pub struct SyncOpts {
    pub force: bool,
    pub dry_run: bool,
    pub only: Option<String>,
    pub prune: bool,
}

impl Default for SyncOpts {
    fn default() -> Self {
        Self { force: false, dry_run: false, only: None, prune: true }
    }
}

pub async fn sync(eng: &Engine, opts: SyncOpts) -> Result<Value> {
    if SYNC_RUNNING
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return Err(anyhow!("vault sync already running"));
    }
    let guard = scopeguard();
    let _ = &guard;

    let cfg = VaultCfg::load(eng);
    if !cfg.root.is_dir() {
        return Err(anyhow!("vault root 不存在: {}（用 --vault-root 或 NEBULA_VAULT_ROOT 配置）", cfg.root.display()));
    }
    let mut files = collect_files(&cfg);
    if let Some(only) = &opts.only {
        files.retain(|(_, rel)| rel.contains(only.as_str()));
    }
    if opts.dry_run {
        let list: Vec<Value> = files
            .iter()
            .map(|(_, rel)| {
                let hay = format!("{rel} {}", Path::new(rel).file_name().unwrap_or_default().to_string_lossy());
                json!({
                    "source": cfg.source_key(rel),
                    "category": cfg.category_for(&hay),
                    "importance": cfg.importance_for(&hay),
                })
            })
            .collect();
        return Ok(json!({"dry_run": true, "candidates": list.len(), "files": list}));
    }

    let mut state = load_state(&cfg);
    let mut stats = json!({"skip": 0, "synced": 0, "added": 0, "deleted": 0, "errors": 0});
    let bump = |stats: &mut Value, k: &str, n: i64| {
        stats[k] = json!(stats[k].as_i64().unwrap_or(0) + n);
    };
    let mut present: Vec<String> = vec![];

    for (abs, rel) in &files {
        let key = cfg.source_key(rel);
        present.push(key.clone());
        let (sha, size, mtime) = match fingerprint(abs) {
            Ok(v) => v,
            Err(e) => {
                bump(&mut stats, "errors", 1);
                tracing::warn!("fingerprint {rel}: {e}");
                continue;
            }
        };
        let prev = state["files"].get(&key).cloned();
        let unchanged = !opts.force
            && prev
                .as_ref()
                .map(|p| p["sha256"] == json!(sha) && p["size"] == json!(size))
                .unwrap_or(false);
        if unchanged {
            bump(&mut stats, "skip", 1);
            continue;
        }
        let text = match std::fs::read_to_string(abs) {
            Ok(t) => t,
            Err(e) => {
                bump(&mut stats, "errors", 1);
                tracing::warn!("read {rel}: {e}");
                continue;
            }
        };
        let (body, meta) = strip_frontmatter(&text);
        let title = extract_title(rel, &body, &meta);
        let hay = format!("{rel} {}", Path::new(rel).file_name().unwrap_or_default().to_string_lossy());
        let cat = cfg.category_for(&hay);
        let imp = cfg.importance_for(&hay);
        let chunks = {
            let c = split_chunks(&body, cfg.chunk_size, cfg.overlap);
            if c.is_empty() {
                vec![body.chars().take(cfg.chunk_size).collect::<String>()]
            } else {
                c
            }
        };
        let deleted = if prev.is_some() || opts.force {
            eng.delete_by_source(&key).unwrap_or(0)
        } else {
            0
        };
        bump(&mut stats, "deleted", deleted);
        let total = chunks.len();
        let mut ids: Vec<i64> = vec![];
        let mut added = 0i64;
        let mut file_errors = 0i64;
        for (i, ch) in chunks.iter().enumerate() {
            let content = wrap_chunk(&cfg, abs, &key, &title, i, total, ch);
            let vec = match eng.embedder.embed(&content).await {
                Ok(v) => v,
                Err(e) => {
                    file_errors += 1;
                    tracing::warn!("embed {key}#{i}: {e}");
                    continue;
                }
            };
            match eng.add(&content, &key, Some(&cat), None, imp, json!({}), true, None, vec) {
                Ok(r) => {
                    if r.get("is_duplicate") != Some(&json!(true)) {
                        added += 1;
                    }
                    if let Some(id) = r.get("id").and_then(|x| x.as_i64()) {
                        ids.push(id);
                    }
                }
                Err(e) => {
                    // content_hash UNIQUE 视为已存在
                    if !e.to_string().contains("UNIQUE") {
                        file_errors += 1;
                        tracing::warn!("add {key}#{i}: {e}");
                    }
                }
            }
        }
        bump(&mut stats, "added", added);
        bump(&mut stats, "errors", file_errors);
        if file_errors > 0 {
            // 有失败不落 state：下一轮整文件重试（重灌为 delete+add，安全）
            tracing::warn!("vault sync {key}: {file_errors} 个 chunk 失败，state 不更新待重试");
            continue;
        }
        bump(&mut stats, "synced", 1);
        state["files"][&key] = json!({
            "sha256": sha, "size": size, "mtime": mtime,
            "path": abs.to_string_lossy(), "title": title,
            "category": cat, "importance": imp,
            "chunks": total, "ids": ids.iter().rev().take(30).rev().collect::<Vec<_>>(),
            "synced_at": now_ts(),
        });
        let _ = save_state(&cfg, &state);
        tracing::info!("vault sync {title} +{added} -{deleted} chunks={total} {key}");
    }

    let mut pruned = 0i64;
    if opts.prune && opts.only.is_none() {
        let known: Vec<String> = state["files"]
            .as_object()
            .map(|m| m.keys().cloned().collect())
            .unwrap_or_default();
        for key in known {
            if !present.contains(&key) {
                let n = eng.delete_by_source(&key).unwrap_or(0);
                state["files"].as_object_mut().map(|m| m.remove(&key));
                pruned += 1;
                tracing::info!("vault pruned {key} chunks={n}");
            }
        }
    }
    state["last_run"] = json!({
        "ts": now_ts(), "stats": stats, "pruned": pruned, "files": files.len(),
    });
    save_state(&cfg, &state)?;
    Ok(json!({
        "stats": stats, "pruned": pruned, "files": files.len(),
        "root": cfg.root.to_string_lossy(), "state_file": cfg.state_file.to_string_lossy(),
    }))
}

pub fn status(eng: &Engine) -> Value {
    let cfg = VaultCfg::load(eng);
    let state = load_state(&cfg);
    let files_n = state["files"].as_object().map(|m| m.len()).unwrap_or(0);
    let vault_rows: i64 = eng
        .with_conn(|c| {
            c.query_row(
                "SELECT COUNT(*) FROM memories WHERE source_file LIKE 'vault:%' AND ifnull(is_compressed,0)=0",
                [],
                |r| r.get(0),
            )
        })
        .unwrap_or(0);
    json!({
        "status": "ok",
        "root": cfg.root.to_string_lossy(),
        "root_exists": cfg.root.is_dir(),
        "state_file": cfg.state_file.to_string_lossy(),
        "tracked_files": files_n,
        "vault_rows": vault_rows,
        "include_globs": cfg.include.len(),
        "exclude_dirs": cfg.exclude_dirs,
        "max_bytes": cfg.max_bytes,
        "chunk_size": cfg.chunk_size,
        "wrap_label": cfg.wrap_label,
        "sync_interval_secs": env_num::<u64>("NEBULA_VAULT_SYNC_INTERVAL", 0),
        "sync_running": SYNC_RUNNING.load(Ordering::SeqCst),
        "last_run": state.get("last_run").cloned().unwrap_or(Value::Null),
        "hint": "POST /vault/sync {force,dry_run,only,prune}；配置见 --help / nebula.example.json",
    })
}

struct SyncGuard;
impl Drop for SyncGuard {
    fn drop(&mut self) {
        SYNC_RUNNING.store(false, Ordering::SeqCst);
    }
}
fn scopeguard() -> SyncGuard {
    SyncGuard
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn glob_semantics() {
        assert!(glob_match("04-项目笔记/星枢.md", "04-项目笔记/*.md"));
        assert!(glob_match("团子学习/记忆系统/_merged-a.md", "团子学习/记忆系统/_merged-*.md"));
        assert!(glob_match("HOME.md", "HOME.md"));
        assert!(!glob_match("HOME.md.bak", "HOME.md"));
    }

    #[test]
    fn exclude_dir_match() {
        let dirs = vec!["团子学习/".to_string(), ".obsidian/".to_string()];
        assert!(in_excluded_dir("团子学习/x.md", &dirs));
        assert!(in_excluded_dir("a/.obsidian/cache.md", &dirs));
        assert!(!in_excluded_dir("04-项目笔记/团子学习笔记.md", &dirs));
    }

    #[test]
    fn frontmatter_and_title() {
        let (body, meta) = strip_frontmatter("---\ntitle: 测试\n---\n# 标题\n正文");
        assert_eq!(meta.get("title").and_then(|x| x.as_str()), Some("测试"));
        assert_eq!(extract_title("a/b.md", &body, &meta), "测试");
        let (body2, meta2) = strip_frontmatter("# 只有标题\n正文");
        assert_eq!(extract_title("a/b.md", &body2, &meta2), "只有标题");
    }

    #[test]
    fn chunking_respects_size() {
        let text = (0..40)
            .map(|i| format!("## 段{i}\n{}", "内容句。".repeat(30)))
            .collect::<Vec<_>>()
            .join("\n");
        let chunks = split_chunks(&text, 900, 100);
        assert!(chunks.len() > 1);
        for c in &chunks {
            assert!(c.chars().count() <= 1100, "chunk too big: {}", c.chars().count());
        }
    }

    #[test]
    fn rules_first_match_wins() {
        let mut rules = parse_rules(r#"[{"contains":["04-项目笔记"],"category":"custom","importance":0.5}]"#);
        rules.extend(default_rules());
        let cfg_rules = rules;
        let hay = "04-项目笔记/x.md x.md";
        let cat = cfg_rules
            .iter()
            .find(|r| r.category.is_some() && r.contains.iter().any(|c| hay.contains(c.as_str())))
            .and_then(|r| r.category.clone())
            .unwrap();
        assert_eq!(cat, "custom");
    }
}
