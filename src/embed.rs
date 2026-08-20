//! 百炼 qwen2.5-vl-embedding。查询向量 LRU + 读现网 emb_query_cache 表（不改表）。

use crate::types::EMBED_DIM;
use anyhow::{anyhow, Result};
use lru::LruCache;
use parking_lot::Mutex;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::num::NonZeroUsize;
use std::time::Duration;

const EMBED_URL: &str =
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding";

pub struct Embedder {
    client: reqwest::Client,
    api_key: String,
    mem: Mutex<LruCache<String, Vec<f32>>>,
}

impl Embedder {
    pub fn new() -> Result<Self> {
        let api_key = std::env::var("BAILIAN_API_KEY")
            .or_else(|_| std::env::var("DASHSCOPE_API_KEY"))
            .unwrap_or_default();
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(2))
            .timeout(Duration::from_secs(7))
            .pool_max_idle_per_host(2)
            .build()?;
        Ok(Self {
            client,
            api_key,
            mem: Mutex::new(LruCache::new(NonZeroUsize::new(256).unwrap())),
        })
    }

    pub fn has_key(&self) -> bool {
        !self.api_key.is_empty()
    }

    fn norm_key(text: &str) -> String {
        text.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase()
    }

    pub fn mem_get(&self, text: &str) -> Option<Vec<f32>> {
        self.mem.lock().get(&Self::norm_key(text)).cloned()
    }

    pub fn mem_put(&self, text: &str, v: Vec<f32>) {
        self.mem.lock().put(Self::norm_key(text), v);
    }

    pub fn stats(&self) -> Value {
        let g = self.mem.lock();
        json!({"size": g.len(), "cap": 256, "disk": true})
    }

    pub async fn embed(&self, text: &str) -> Result<Vec<f32>> {
        if let Some(v) = self.mem_get(text) {
            return Ok(v);
        }
        let v = self.call_api(json!([{"text": text}])).await?;
        self.mem_put(text, v.clone());
        Ok(v)
    }

    pub async fn embed_image(&self, data_uri: &str, caption: Option<&str>) -> Result<Vec<f32>> {
        let mut contents = vec![];
        if let Some(c) = caption {
            let t: String = c.chars().take(2000).collect();
            if !t.trim().is_empty() {
                contents.push(json!({"text": t}));
            }
        }
        contents.push(json!({"image": data_uri}));
        self.call_api(Value::Array(contents)).await
    }

    async fn call_api(&self, contents: Value) -> Result<Vec<f32>> {
        if self.api_key.is_empty() {
            return Err(anyhow!("BAILIAN_API_KEY 未设置"));
        }
        let body = json!({
            "model": "qwen2.5-vl-embedding",
            "input": {"contents": contents},
            "parameters": {"dimension": EMBED_DIM},
        });
        let resp = self
            .client
            .post(EMBED_URL)
            .header("Authorization", format!("Bearer {}", self.api_key))
            .json(&body)
            .send()
            .await?;
        let status = resp.status();
        let val: Value = resp.json().await?;
        if !status.is_success() {
            return Err(anyhow!("Embedding API HTTP {status}: {}", trunc(&val.to_string(), 300)));
        }
        if let Some(sc) = val.get("status_code").and_then(|x| x.as_i64()) {
            if sc != 200 {
                return Err(anyhow!("Embedding API 错误: {}", trunc(&val.to_string(), 200)));
            }
        }
        let embs = val
            .pointer("/output/embeddings")
            .and_then(|x| x.as_array())
            .cloned()
            .unwrap_or_default();
        for item in embs {
            if let Some(arr) = item.get("vector").or_else(|| item.get("embedding")).and_then(|x| x.as_array()) {
                let v: Vec<f32> = arr.iter().filter_map(|n| n.as_f64().map(|f| f as f32)).collect();
                if !v.is_empty() {
                    return Ok(v);
                }
            }
        }
        Err(anyhow!("Embedding API 返回空向量"))
    }
}

pub fn l2_normalize(v: &mut [f32]) {
    let n: f32 = v.iter().map(|x| *x * *x).sum::<f32>().sqrt();
    if n > 1e-10 {
        for x in v.iter_mut() {
            *x /= n;
        }
    }
}

pub fn content_hash(text: &str) -> String {
    let mut h = Sha256::new();
    h.update(text.as_bytes());
    hex::encode(h.finalize())
}

fn trunc(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
