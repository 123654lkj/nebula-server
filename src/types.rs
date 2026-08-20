//! 检索命中与公共常量。

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const EMBED_DIM: usize = 2048;
pub const EMBED_MODEL: &str = "qwen2.5-vl-embedding";
pub const RERANK_MODEL: &str = "qwen3-rerank";
pub const PRODUCT: &str = "Nebula Memory";
pub const RELEASE: &str = "v5.1.1";
pub const VERSION: &str = "5.1.1";

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Hit {
    pub id: i64,
    #[serde(default)]
    pub content: String,
    pub score: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub category: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub importance: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_file: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_preview: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub abstract_text: Option<String>,
    #[serde(rename = "abstract", skip_serializing_if = "Option::is_none")]
    pub abstract_out: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memory_layer: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub modality: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub image_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub image_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub image_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trust: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub authority_mult: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub readback: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub src: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bm25_score: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub vector_score: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub time_decay: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub explain: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rerank_score: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rerank_raw: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rerank_skip: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pinned: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hop: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub level: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub node_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub node_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub project_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub location: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub full: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub full_chars: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_chars: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dated_facts: Option<Value>,
}

impl Hit {
    pub fn src_file(&self) -> &str {
        self.source_file.as_deref().unwrap_or("")
    }

    pub fn to_compact_value(&self) -> Value {
        serde_json::to_value(self).unwrap_or(Value::Null)
    }

    pub fn metadata_map(&self) -> Map<String, Value> {
        match &self.metadata {
            Some(Value::Object(m)) => m.clone(),
            _ => Map::new(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct SearchOpts {
    pub top_k: usize,
    pub category: Option<String>,
    pub use_hybrid: bool,
    pub rerank: bool,
    pub rerank_candidates: usize,
    pub enable_time_decay: bool,
    pub time_decay_lambda: f64,
    pub explain: bool,
    pub tenant_id: Option<String>,
    pub temporal_intent: Option<String>,
    pub as_of: Option<f64>,
    pub similarity_threshold: f64,
    pub project_name: Option<String>,
    pub location: Option<String>,
    pub source_file: Option<String>,
    pub node_type: Option<String>,
    pub node_name: Option<String>,
    pub level: Option<i64>,
    pub date_from: Option<String>,
    pub date_to: Option<String>,
    pub precomputed: Option<Vec<f32>>,
}

impl Default for SearchOpts {
    fn default() -> Self {
        Self {
            top_k: 10,
            category: None,
            use_hybrid: false,
            rerank: false,
            rerank_candidates: 8,
            enable_time_decay: true,
            time_decay_lambda: 0.05,
            explain: false,
            tenant_id: None,
            temporal_intent: None,
            as_of: None,
            similarity_threshold: 0.0,
            project_name: None,
            location: None,
            source_file: None,
            node_type: None,
            node_name: None,
            level: None,
            date_from: None,
            date_to: None,
            precomputed: None,
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct PackOpts {
    pub top_k: usize,
    pub max_chars: usize,
    pub per_source: usize,
    pub drop_superseded: bool,
    pub drop_hearsay_if_canon: bool,
    pub compact: bool,
    pub max_synthesis: usize,
    pub prefer_layers: Option<Vec<String>>,
}

impl PackOpts {
    pub fn ask_default(top_k: usize, max_chars: usize) -> Self {
        Self {
            top_k,
            max_chars,
            per_source: 1,
            drop_superseded: true,
            drop_hearsay_if_canon: true,
            compact: true,
            max_synthesis: 1,
            prefer_layers: None,
        }
    }
}
