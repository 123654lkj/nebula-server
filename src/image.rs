//! 图片 RAG：url / data URI / 路径 / 原始字节。对齐 vector_memory.resolve_image。

use anyhow::{anyhow, Result};
use base64::Engine as _;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

const MAX_BYTES: usize = 5 * 1024 * 1024;

pub struct ResolvedImage {
    pub bytes: Vec<u8>,
    pub mime: String,
    pub name: String,
    pub data_uri: String,
    pub src_url: String,
    pub path: String,
    pub sha256: String,
}

pub fn guess_mime(name: &str, data: &[u8]) -> String {
    let ext = Path::new(name.split('?').next().unwrap_or(name))
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_lowercase();
    match ext.as_str() {
        "jpg" | "jpeg" => return "image/jpeg".into(),
        "png" => return "image/png".into(),
        "webp" => return "image/webp".into(),
        "gif" => return "image/gif".into(),
        "bmp" => return "image/bmp".into(),
        "tif" | "tiff" => return "image/tiff".into(),
        _ => {}
    }
    if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        "image/png".into()
    } else if data.len() >= 3 && data[0] == 0xff && data[1] == 0xd8 && data[2] == 0xff {
        "image/jpeg".into()
    } else if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        "image/webp".into()
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        "image/gif".into()
    } else {
        "image/jpeg".into()
    }
}

pub async fn resolve(image: &str, persist_dir: Option<&Path>) -> Result<ResolvedImage> {
    let s = image.trim();
    if s.is_empty() {
        return Err(anyhow!("image 为空"));
    }
    let mut raw = vec![];
    let mut mime = "image/jpeg".to_string();
    let mut name = "image.jpg".to_string();
    let mut src_url = String::new();
    let mut data_uri = String::new();

    if s.starts_with("data:image/") {
        let (header, b64) = s.split_once(',').ok_or_else(|| anyhow!("坏 data URI"))?;
        mime = header[5..].split(';').next().unwrap_or("image/jpeg").to_string();
        raw = base64::engine::general_purpose::STANDARD.decode(b64)?;
        data_uri = s.to_string();
        name = format!("image.{}", mime.split('/').nth(1).unwrap_or("jpg"));
    } else if s.starts_with("http://") || s.starts_with("https://") {
        src_url = s.to_string();
        name = Path::new(s.split('?').next().unwrap_or(s))
            .file_name()
            .and_then(|x| x.to_str())
            .unwrap_or("image.jpg")
            .to_string();
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(15))
            .build()?;
        let bytes = client
            .get(s)
            .header("User-Agent", "nebula-image/1.0")
            .send()
            .await?
            .bytes()
            .await?;
        raw = bytes.to_vec();
        mime = guess_mime(&name, &raw);
        data_uri = s.to_string(); // 百炼可直接吃公网 URL
    } else {
        let p = std::fs::canonicalize(s).unwrap_or_else(|_| PathBuf::from(s));
        if !p.is_file() {
            return Err(anyhow!("图片文件不存在: {s}"));
        }
        raw = std::fs::read(&p)?;
        name = p.file_name().and_then(|x| x.to_str()).unwrap_or("image.jpg").into();
        mime = guess_mime(&name, &raw);
        data_uri = format!(
            "data:{mime};base64,{}",
            base64::engine::general_purpose::STANDARD.encode(&raw)
        );
    }
    if raw.is_empty() {
        return Err(anyhow!("读不到图片字节"));
    }
    if raw.len() > MAX_BYTES {
        return Err(anyhow!("图片超过 5MB（qwen2.5-vl-embedding 上限）"));
    }
    let sha = {
        let mut h = Sha256::new();
        h.update(&raw);
        hex::encode(h.finalize())
    };
    let mut stored = String::new();
    if let Some(dir) = persist_dir {
        std::fs::create_dir_all(dir)?;
        let ext_owned = Path::new(&name)
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_lowercase();
        let ext = match ext_owned.as_str() {
            "jpeg" | "jpg" => "jpg",
            "png" => "png",
            "webp" => "webp",
            "gif" => "gif",
            other if !other.is_empty() => other,
            _ => mime.split('/').nth(1).unwrap_or("jpg"),
        };
        let path = dir.join(format!("{}.{ext}", &sha[..16]));
        if !path.exists() {
            std::fs::write(&path, &raw)?;
        }
        stored = path.to_string_lossy().into();
    }
    if data_uri.is_empty() {
        data_uri = format!(
            "data:{mime};base64,{}",
            base64::engine::general_purpose::STANDARD.encode(&raw)
        );
    }
    Ok(ResolvedImage {
        bytes: raw,
        mime,
        name,
        data_uri,
        src_url,
        path: stored,
        sha256: sha,
    })
}

pub fn resolve_bytes(raw: Vec<u8>, filename: &str, persist_dir: Option<&Path>) -> Result<ResolvedImage> {
    if raw.is_empty() {
        return Err(anyhow!("image 为空"));
    }
    if raw.len() > MAX_BYTES {
        return Err(anyhow!("图片超过 5MB（qwen2.5-vl-embedding 上限）"));
    }
    let mime = guess_mime(filename, &raw);
    let name = if filename.is_empty() { "image.jpg".into() } else { filename.into() };
    let data_uri = format!(
        "data:{mime};base64,{}",
        base64::engine::general_purpose::STANDARD.encode(&raw)
    );
    let sha = {
        let mut h = Sha256::new();
        h.update(&raw);
        hex::encode(h.finalize())
    };
    let mut stored = String::new();
    if let Some(dir) = persist_dir {
        std::fs::create_dir_all(dir)?;
        let ext = mime.split('/').nth(1).unwrap_or("jpg");
        let ext = if ext == "jpeg" { "jpg" } else { ext };
        let path = dir.join(format!("{}.{ext}", &sha[..16]));
        if !path.exists() {
            std::fs::write(&path, &raw)?;
        }
        stored = path.to_string_lossy().into();
    }
    Ok(ResolvedImage {
        bytes: raw,
        mime,
        name,
        data_uri,
        src_url: String::new(),
        path: stored,
        sha256: sha,
    })
}

pub fn meta_fields(r: &ResolvedImage) -> serde_json::Value {
    let mut o = serde_json::json!({
        "modality": "image",
        "image_path": r.path,
        "image_mime": r.mime,
        "image_name": r.name,
    });
    if !r.src_url.is_empty() {
        o["image_src"] = serde_json::json!(r.src_url);
    }
    o
}

pub fn is_image_path(p: &str) -> bool {
    matches!(
        Path::new(p).extension().and_then(|s| s.to_str()).unwrap_or("").to_lowercase().as_str(),
        "jpg" | "jpeg" | "png" | "webp" | "gif" | "bmp" | "tif" | "tiff"
    )
}
