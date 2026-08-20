//! 配置装载：CLI > 环境变量 > 配置文件(JSON) > 内置默认。
//! 文件与 CLI 的键统一物化为环境变量，其余模块照常从 env 读取——路径不再写死。

use serde_json::Value;
use std::path::PathBuf;

const USAGE: &str = "\
nebula-engine —— 星枢 Rust 常驻引擎

用法: nebula-engine [选项]

选项:
  --config <file>             JSON 配置文件；默认查找 $NEBULA_CONFIG → ./nebula.json → /etc/nebula/nebula.json
  --db <path>                 SQLite 库路径                (NEBULA_DB_PATH)
  --bind <addr>               监听地址                     (NEBULA_BIND，默认 0.0.0.0)
  --port <n>                  端口                         (NEBULA_PORT，默认 26672)
  --docs-dir <dir>            /help 文档目录               (NEBULA_DOCS_DIR)
  --vault-root <dir>          Obsidian 笔记根目录          (NEBULA_VAULT_ROOT)
  --vault-sync-interval <s>   内置 vault 同步间隔秒，0=关  (NEBULA_VAULT_SYNC_INTERVAL)
  --readback-fmt <fmt>        回读命令模板，{path} 占位    (NEBULA_READBACK_FMT)
  --set KEY=VALUE             覆盖任意环境变量，可重复
  --print-config              打印生效配置后退出
  -V, --version               打印版本
  -h, --help                  本帮助

优先级: CLI > 环境变量 > 配置文件 > 默认值。
配置文件为扁平 JSON：小写键自动映射 NEBULA_*（如 \"port\" → NEBULA_PORT），
全大写键原样透传（如 BAILIAN_API_KEY）；字符串数组按逗号连接，对象/对象数组序列化为 JSON 字符串。
";

/// 打印 --print-config 时展示的键（密钥类会打码）。
const SHOW_KEYS: &[&str] = &[
    "NEBULA_DB_PATH",
    "NEBULA_BIND",
    "NEBULA_PORT",
    "NEBULA_DOCS_DIR",
    "NEBULA_VAULT_ROOT",
    "NEBULA_VAULT_STATE",
    "NEBULA_VAULT_KEY_PREFIX",
    "NEBULA_VAULT_INCLUDE",
    "NEBULA_VAULT_EXCLUDE_DIRS",
    "NEBULA_VAULT_EXCLUDE_NAMES",
    "NEBULA_VAULT_MAX_BYTES",
    "NEBULA_VAULT_CHUNK",
    "NEBULA_VAULT_OVERLAP",
    "NEBULA_VAULT_RULES",
    "NEBULA_VAULT_WRAP_LABEL",
    "NEBULA_VAULT_SYNC_INTERVAL",
    "NEBULA_PROMOTE_SUBDIR",
    "NEBULA_READBACK_FMT",
    "NEBULA_PATH_MAP",
    "NEBULA_PIN_REGEX",
    "NEBULA_CANON_SUBSTR",
    "NEBULA_DEMOTE_SUBSTR",
    "NEBULA_HEARSAY_SUBSTR",
    "NEBULA_SCRATCH_SUBSTR",
    "NEBULA_REG_LATEST",
    "NEBULA_LLM_MODEL",
    "BAILIAN_API_KEY",
    "NEBULA_LLM_API_KEY",
    "NEBULA_API_TOKEN",
];

fn norm_key(k: &str) -> String {
    let env_style = k
        .chars()
        .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_');
    if env_style {
        return k.to_string();
    }
    let up = k.to_ascii_uppercase().replace(['-', '.'], "_");
    if up.starts_with("NEBULA_") {
        up
    } else {
        format!("NEBULA_{up}")
    }
}

fn val_to_env(v: &Value) -> Option<String> {
    match v {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        Value::Bool(b) => Some(b.to_string()),
        Value::Null => None,
        Value::Array(a) if a.iter().all(|x| x.is_string()) => Some(
            a.iter()
                .filter_map(|x| x.as_str())
                .collect::<Vec<_>>()
                .join(","),
        ),
        other => Some(other.to_string()),
    }
}

fn find_config(explicit: Option<&str>) -> Option<PathBuf> {
    if let Some(p) = explicit {
        return Some(PathBuf::from(p));
    }
    if let Ok(p) = std::env::var("NEBULA_CONFIG") {
        if !p.trim().is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    for cand in ["./nebula.json", "/etc/nebula/nebula.json"] {
        let p = PathBuf::from(cand);
        if p.exists() {
            return Some(p);
        }
    }
    None
}

fn apply_config_file(path: &PathBuf) -> Result<usize, String> {
    let txt = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let obj: Value =
        serde_json::from_str(&txt).map_err(|e| format!("{} 不是合法 JSON: {e}", path.display()))?;
    let Some(map) = obj.as_object() else {
        return Err(format!("{} 顶层必须是 JSON 对象", path.display()));
    };
    let mut applied = 0;
    for (k, v) in map {
        let key = norm_key(k);
        let Some(val) = val_to_env(v) else { continue };
        // 环境变量已有值时不覆盖：env > 配置文件
        if std::env::var_os(&key).is_none() {
            std::env::set_var(&key, val);
            applied += 1;
        }
    }
    Ok(applied)
}

fn mask(key: &str, val: &str) -> String {
    let sensitive = ["KEY", "TOKEN", "SECRET", "PASSWORD"];
    if sensitive.iter().any(|s| key.contains(s)) && !val.is_empty() {
        let head: String = val.chars().take(4).collect();
        return format!("{head}***({} chars)", val.chars().count());
    }
    val.to_string()
}

fn print_config(config_file: Option<&PathBuf>) {
    println!(
        "# nebula-engine {} 生效配置（CLI > env > 文件 > 默认）",
        crate::types::RELEASE
    );
    println!(
        "config_file = {}",
        config_file.map(|p| p.display().to_string()).unwrap_or_else(|| "(无)".into())
    );
    for k in SHOW_KEYS {
        match std::env::var(k) {
            Ok(v) => println!("{k} = {}", mask(k, &v)),
            Err(_) => println!("{k} = (默认)"),
        }
    }
}

/// 解析 CLI + 配置文件并物化到环境变量。--help/--version/--print-config 打印后退出。
pub fn init() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut config_path: Option<String> = None;
    let mut overrides: Vec<(String, String)> = vec![];
    let mut want_print = false;

    let mut i = 0;
    let mut take = |i: &mut usize, flag: &str| -> String {
        *i += 1;
        args.get(*i).cloned().unwrap_or_else(|| {
            eprintln!("缺少参数: {flag} <value>");
            std::process::exit(2);
        })
    };
    while i < args.len() {
        match args[i].as_str() {
            "-h" | "--help" => {
                print!("{USAGE}");
                std::process::exit(0);
            }
            "-V" | "--version" => {
                println!("nebula-engine {}", crate::types::RELEASE);
                std::process::exit(0);
            }
            "--print-config" => want_print = true,
            "--config" => config_path = Some(take(&mut i, "--config")),
            "--db" => overrides.push(("NEBULA_DB_PATH".into(), take(&mut i, "--db"))),
            "--bind" => overrides.push(("NEBULA_BIND".into(), take(&mut i, "--bind"))),
            "--port" => overrides.push(("NEBULA_PORT".into(), take(&mut i, "--port"))),
            "--docs-dir" => overrides.push(("NEBULA_DOCS_DIR".into(), take(&mut i, "--docs-dir"))),
            "--vault-root" => {
                overrides.push(("NEBULA_VAULT_ROOT".into(), take(&mut i, "--vault-root")))
            }
            "--vault-sync-interval" => overrides.push((
                "NEBULA_VAULT_SYNC_INTERVAL".into(),
                take(&mut i, "--vault-sync-interval"),
            )),
            "--readback-fmt" => {
                overrides.push(("NEBULA_READBACK_FMT".into(), take(&mut i, "--readback-fmt")))
            }
            "--set" => {
                let kv = take(&mut i, "--set");
                match kv.split_once('=') {
                    Some((k, v)) => overrides.push((norm_key(k), v.to_string())),
                    None => {
                        eprintln!("--set 需要 KEY=VALUE 格式: {kv}");
                        std::process::exit(2);
                    }
                }
            }
            other => {
                eprintln!("未知参数: {other}\n\n{USAGE}");
                std::process::exit(2);
            }
        }
        i += 1;
    }

    let file = find_config(config_path.as_deref());
    if let Some(p) = &file {
        match apply_config_file(p) {
            Ok(n) => eprintln!("配置文件 {} 应用 {n} 项", p.display()),
            Err(e) => {
                // 显式 --config 指定的文件必须可用；自动发现的坏文件只警告
                if config_path.is_some() {
                    eprintln!("配置文件错误: {e}");
                    std::process::exit(2);
                }
                eprintln!("忽略配置文件: {e}");
            }
        }
    }
    for (k, v) in &overrides {
        std::env::set_var(k, v);
    }
    if want_print {
        print_config(file.as_ref());
        std::process::exit(0);
    }
}

#[cfg(test)]
mod tests {
    use super::{norm_key, val_to_env};
    use serde_json::json;

    #[test]
    fn key_normalization() {
        assert_eq!(norm_key("port"), "NEBULA_PORT");
        assert_eq!(norm_key("vault-root"), "NEBULA_VAULT_ROOT");
        assert_eq!(norm_key("nebula_db_path"), "NEBULA_DB_PATH");
        assert_eq!(norm_key("BAILIAN_API_KEY"), "BAILIAN_API_KEY");
    }

    #[test]
    fn value_materialization() {
        assert_eq!(val_to_env(&json!(26670)).as_deref(), Some("26670"));
        assert_eq!(val_to_env(&json!(["a", "b"])).as_deref(), Some("a,b"));
        assert_eq!(
            val_to_env(&json!([{"contains":["x"]}])).as_deref(),
            Some(r#"[{"contains":["x"]}]"#)
        );
        assert_eq!(val_to_env(&json!(null)), None);
    }
}
