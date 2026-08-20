//! HTTP API，路径与 JSON 字段对齐现网 Python。

use crate::engine::{AddExtra, Engine};
use crate::extract::{layered_recall_plan, promote_draft, session_extract};
use crate::image;
use crate::rank::{
    bootstrap, format_ask_pack, pack_results, reflect_ask, results_token_stats, ultimate_ask, ReflectOpts,
};
use crate::secrets;
use crate::temporal::{classify_temporal_intent, parse_as_of};
use crate::types::{PackOpts, SearchOpts, PRODUCT, RELEASE};
use axum::body::Body;
use axum::extract::multipart::Multipart;
use axum::extract::{FromRequest, Path, Query, State};
use axum::http::{header, HeaderMap, Method, Request, StatusCode};
use http_body_util::BodyExt;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;
use tower_http::cors::CorsLayer;

#[derive(Clone)]
pub struct AppState {
    pub eng: Arc<Engine>,
}

fn parse_bool(v: Option<&Value>, default: bool) -> bool {
    match v {
        None => default,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => matches!(s.to_lowercase().as_str(), "1" | "true" | "yes" | "on"),
        Some(Value::Number(n)) => n.as_i64().unwrap_or(0) != 0,
        _ => default,
    }
}
fn qbool(q: &HashMap<String, String>, k: &str, default: bool) -> bool {
    q.get(k)
        .map(|s| matches!(s.to_lowercase().as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(default)
}
fn qint(q: &HashMap<String, String>, k: &str, default: i64) -> i64 {
    q.get(k).and_then(|s| s.parse().ok()).unwrap_or(default)
}

fn cache_key(kind: &str, payload: &Value) -> String {
    format!("{kind}:{}", payload)
}

async fn embed_query(st: &AppState, query: &str, image: Option<&str>) -> Result<Vec<f32>, String> {
    if let Some(img) = image.filter(|s| !s.is_empty()) {
        let resolved = image::resolve(img, Some(&st.eng.image_dir))
            .await
            .map_err(|e| e.to_string())?;
        let cap = if query.is_empty() || query == "[image-query]" {
            None
        } else {
            Some(query)
        };
        return st
            .eng
            .embedder
            .embed_image(&resolved.data_uri, cap)
            .await
            .map_err(|e| e.to_string());
    }
    st.eng.embedder.embed(query).await.map_err(|e| e.to_string())
}

fn merge_ok(v: Value) -> Value {
    let mut out = json!({"status": "ok"});
    if let Some(obj) = v.as_object() {
        for (k, val) in obj {
            out[k.clone()] = val.clone();
        }
    }
    out
}

pub fn router(st: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/help", get(help))
        .route("/docs", get(help))
        .route("/search", get(search).post(search))
        .route("/ask", get(ask).post(ask))
        .route("/search_pack", get(ask).post(ask))
        .route("/search_rerank", post(search_rerank))
        .route("/reranker/status", get(reranker_status))
        .route("/rewrite_search", post(rewrite_search))
        .route("/ingest", post(ingest))
        .route("/stats", get(stats))
        .route("/tags/cloud", get(tags_cloud))
        .route("/tags/by-category", get(tags_by_cat))
        .route("/tags/rename", post(tags_rename))
        .route("/tags/merge", post(tags_merge))
        .route("/tags/cleanup", post(tags_cleanup))
        .route("/memory/add", post(memory_add))
        .route("/memory/dupes", get(memory_dupes))
        .route("/memory/gc", post(memory_gc))
        .route("/memory/infer-layer", post(memory_infer_layer))
        .route("/memory/image/{id}", get(memory_image))
        .route("/memory/promote", post(memory_promote))
        .route("/memory/{id}", axum::routing::delete(memory_delete).put(memory_update))
        .route("/memory/reclassify", post(reclassify))
        .route("/compress", post(compress))
        .route("/ui", get(ui))
        .route("/secrets/status", get(secrets_status))
        .route("/secrets/list", get(secrets_list))
        .route("/secrets/get", post(secrets_get))
        .route("/secrets/store", post(secrets_store))
        .route("/secrets/register", post(secrets_register))
        .route("/secrets/catalog-sync", post(secrets_catalog))
        .route("/secrets/resolve", post(secrets_resolve))
        .route("/vault/status", get(vault_status))
        .route("/vault/sync", post(vault_sync))
        .route("/v5/session-extract", post(v5_extract))
        .route("/v5/layered-recall", get(v5_layered).post(v5_layered))
        .route("/v5/promote-draft", post(v5_promote))
        .route("/v5/bootstrap", get(v5_bootstrap).post(v5_bootstrap))
        .route("/v5/answer", post(v5_answer))
        .route("/v5/lifecycle", post(v5_lifecycle))
        .route("/v5/health", get(v5_health))
        .route("/memory/{id}/related", get(memory_related))
        .route("/memory/supersede", post(memory_supersede))
        .route("/v4/migrate", post(v4_migrate))
        .route("/v4/reflect", post(v4_reflect))
        .route("/mcp", post(mcp))
        .with_state(st)
        .layer(axum::extract::DefaultBodyLimit::max(6 * 1024 * 1024))
        .layer(CorsLayer::permissive())
}

async fn health(State(st): State<AppState>) -> Json<Value> {
    let mat = st.eng.emb.read();
    let rss = st.eng.rss_bytes();
    Json(json!({
        "status": "ok",
        "service": "nebula-memory",
        "product": PRODUCT,
        "version": RELEASE,
        "docs": "/help",
        "docs_hint": "GET /help (mini) | /help?level=short|full",
        "engine": "rust",
        "emb_rows": mat.n(),
        "emb_quant": "i16",
        "emb_bytes": mat.nbytes(),
        "rss_bytes": rss,
        "rss_mb": rss.map(|b| ((b as f64) / 1024.0 / 1024.0 * 10.0).round() / 10.0),
    }))
}

async fn help(Query(q): Query<HashMap<String, String>>, State(st): State<AppState>) -> Response {
    let level = q.get("level").or(q.get("v")).cloned().unwrap_or_else(|| "mini".into());
    let fmt = q.get("format").cloned().unwrap_or_else(|| "text".into());
    let (lvl, text) = st.eng.help_text(&level);
    let chars = text.chars().count();
    let est = ((chars as f64) / 2.2).max(1.0) as i64;
    if fmt == "json" {
        return Json(json!({
            "status":"ok","level": lvl, "chars": chars, "est_tokens": est, "text": text,
            "hint": "默认 level=mini 省 token；需要时再 short/full。勿把 full 写入每轮 system prompt。",
            "links": {"mini":"/help?level=mini","short":"/help?level=short","full":"/help?level=full"}
        }))
        .into_response();
    }
    let body = format!("{text}\n\n---\n# meta: level={lvl} chars={chars} est_tokens≈{est} | JSON: /help?level={lvl}&format=json\n");
    Response::builder()
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .body(Body::from(body))
        .unwrap()
}

async fn search(method: Method, Query(qs): Query<HashMap<String, String>>, State(st): State<AppState>, body: Option<Json<Value>>) -> impl IntoResponse {
    let t0 = Instant::now();
    let data = if method == Method::GET {
        json!({})
    } else {
        body.map(|j| j.0).unwrap_or(json!({}))
    };
    let mut query = if method == Method::GET {
        qs.get("q").cloned().unwrap_or_default()
    } else {
        data.get("query").or(data.get("q")).and_then(|x| x.as_str()).unwrap_or("").to_string()
    };
    let image_ref = if method == Method::GET {
        None
    } else {
        data.get("image").or(data.get("image_url")).and_then(|x| x.as_str()).map(|s| s.to_string())
    };
    if query.is_empty() && image_ref.is_none() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"query is required"})));
    }
    let top_k = if method == Method::GET { qint(&qs, "top_k", 5) as usize } else { data.get("top_k").and_then(|x| x.as_u64()).unwrap_or(5) as usize };
    let mut use_hybrid = if method == Method::GET { qbool(&qs, "use_hybrid", true) } else { parse_bool(data.get("use_hybrid"), true) };
    if query.is_empty() && image_ref.is_some() {
        query = "[image-query]".into();
        use_hybrid = false;
    }
    let rerank = if method == Method::GET { qbool(&qs, "rerank", false) } else { parse_bool(data.get("rerank"), false) };
    let pack_on = if method == Method::GET { qbool(&qs, "pack", true) } else { parse_bool(data.get("pack"), true) };
    let compact = if method == Method::GET {
        if qbool(&qs, "full", false) { false } else { qbool(&qs, "compact", true) }
    } else if parse_bool(data.get("full"), false) {
        false
    } else {
        parse_bool(data.get("compact"), true)
    };
    let category = if method == Method::GET {
        qs.get("category").cloned().or_else(|| qs.get("cat").cloned())
    } else {
        data.get("category").and_then(|x| x.as_str()).map(|s| s.to_string())
    };
    let ck = cache_key("search", &json!({"q": query, "top_k": top_k, "hybrid": use_hybrid, "pack": pack_on}));
    if let Some(hit) = st.eng.cache_get(&ck) {
        let mut out = hit;
        out["elapsed_ms"] = json!((t0.elapsed().as_secs_f64() * 1000.0 * 10.0).round() / 10.0);
        out["result_cache_hit"] = json!(true);
        return (StatusCode::OK, Json(out));
    }
    let fetch = if pack_on { top_k * 3 } else { top_k };
    let vec = match embed_query(&st, &query, image_ref.as_deref()).await {
        Ok(v) => v,
        Err(e) => return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": e.to_string()}))),
    };
    let mut so = SearchOpts {
        top_k: fetch,
        category,
        use_hybrid,
        rerank: false,
        enable_time_decay: true,
        precomputed: Some(vec),
        ..Default::default()
    };
    if method != Method::GET {
        so.explain = parse_bool(data.get("explain"), false);
    }
    let raw = match st.eng.search(&query, so) {
        Ok(v) => v,
        Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    };
    let mut packed = raw.clone();
    if pack_on {
        packed = pack_results(
            &raw,
            &query,
            &PackOpts {
                top_k,
                max_chars: 320,
                per_source: 1,
                drop_superseded: true,
                drop_hearsay_if_canon: true,
                compact,
                max_synthesis: 1,
                prefer_layers: None,
            },
        );
    } else {
        packed.truncate(top_k);
    }
    if rerank {
        let n = packed.len().max(2);
        crate::rerank::apply_rerank(&query, &mut packed, n).await;
    }
    let tok = results_token_stats(&packed);
    let resp = json!({
        "status":"ok","results": packed, "count": packed.len(),
        "elapsed_ms": (t0.elapsed().as_secs_f64()*1000.0*10.0).round()/10.0,
        "cache_hit": true,
        "cache_stats": st.eng.embedder.stats(),
        "use_hybrid": use_hybrid,
        "rewrite": false,
        "smart_rewrite_used": false,
        "pack": pack_on, "compact": compact,
        "token_stats": tok,
        "result_cache_hit": false,
        "result_cache": st.eng.cache_stats(),
        "engine": "rust",
    });
    st.eng.cache_put(ck, resp.clone());
    (StatusCode::OK, Json(resp))
}

async fn ask(method: Method, Query(qs): Query<HashMap<String, String>>, headers: HeaderMap, State(st): State<AppState>, body: Option<Json<Value>>) -> impl IntoResponse {
    let t0 = Instant::now();
    let data = if method == Method::GET { json!({}) } else { body.map(|j| j.0).unwrap_or(json!({})) };
    let mut query = if method == Method::GET {
        qs.get("q").or(qs.get("query")).cloned().unwrap_or_default()
    } else {
        data.get("query").or(data.get("q")).and_then(|x| x.as_str()).unwrap_or("").to_string()
    };
    let image_ref = if method == Method::GET {
        None
    } else {
        data.get("image").or(data.get("image_url")).and_then(|x| x.as_str()).map(|s| s.to_string())
    };
    if query.is_empty() && image_ref.is_none() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"query is required"})));
    }
    let top_k = if method == Method::GET { qint(&qs, "top_k", 5) as usize } else { data.get("top_k").and_then(|x| x.as_u64()).unwrap_or(5) as usize };
    let max_chars = if method == Method::GET { qint(&qs, "max_chars", 280) as usize } else { data.get("max_chars").and_then(|x| x.as_u64()).unwrap_or(280) as usize };
    let max_total = if method == Method::GET { qint(&qs, "max_total_chars", 1800) as usize } else { data.get("max_total_chars").and_then(|x| x.as_u64()).unwrap_or(1800) as usize };
    let mut use_hybrid = if method == Method::GET { qbool(&qs, "use_hybrid", true) } else { parse_bool(data.get("use_hybrid"), true) };
    if query.is_empty() && image_ref.is_some() {
        query = "[image-query]".into();
        use_hybrid = false;
    }
    let hops = if method == Method::GET { qint(&qs, "hops", 2) as usize } else { data.get("hops").and_then(|x| x.as_u64()).unwrap_or(2) as usize };
    let engine = if method == Method::GET {
        qs.get("engine").cloned().unwrap_or_else(|| "ultimate".into())
    } else {
        data.get("engine").and_then(|x| x.as_str()).unwrap_or("ultimate").to_string()
    };
    let use_graph = if method == Method::GET { qbool(&qs, "use_graph", true) } else { parse_bool(data.get("use_graph"), true) };
    let no_cache = if method == Method::GET { qbool(&qs, "no_cache", false) } else { parse_bool(data.get("no_cache"), false) };
    let llm_deep = if method == Method::GET {
        qs.get("llm_deep").cloned().unwrap_or_else(|| "auto".into())
    } else {
        data.get("llm_deep").and_then(|x| x.as_str()).unwrap_or("auto").to_string()
    };
    let llm_answer = if method == Method::GET { qbool(&qs, "llm_answer", false) } else { parse_bool(data.get("llm_answer"), false) };
    let rerank = if method == Method::GET { qbool(&qs, "rerank", true) } else { parse_bool(data.get("rerank"), true) };
    let reader = if method == Method::GET { qbool(&qs, "reader", false) } else { parse_bool(data.get("reader"), false) };
    let temporal_intent = {
        let raw = if method == Method::GET { qs.get("temporal_intent").cloned() } else { data.get("temporal_intent").and_then(|x| x.as_str()).map(|s| s.to_string()) };
        raw.filter(|s| !s.is_empty()).unwrap_or_else(|| classify_temporal_intent(&query))
    };
    let as_of = if method == Method::GET {
        parse_as_of(qs.get("as_of").map(|s| json!(s)).as_ref())
    } else {
        parse_as_of(data.get("as_of"))
    };
    let tenant = crate::site::resolve_tenant(
        data.get("tenant_id")
            .and_then(|x| x.as_str())
            .or_else(|| headers.get("X-Nebula-Tenant").and_then(|v| v.to_str().ok())),
    );
    if crate::site::require_tenant() && tenant.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"tenant_id required"})));
    }
    let ck = cache_key(
        "ask_v4",
        &json!({"q": query, "top_k": top_k, "engine": engine, "hops": hops, "ti": temporal_intent, "llm_deep": llm_deep, "rerank": rerank}),
    );
    if !no_cache {
        if let Some(hit) = st.eng.cache_get(&ck) {
            let mut out = hit;
            out["elapsed_ms"] = json!((t0.elapsed().as_secs_f64() * 1000.0 * 10.0).round() / 10.0);
            out["result_cache_hit"] = json!(true);
            return (StatusCode::OK, Json(out));
        }
    }
    let _ = embed_query(&st, &query, image_ref.as_deref()).await; // 填 LRU，search 同步可用
    let ro = ReflectOpts {
        top_k,
        max_chars,
        max_total,
        use_hybrid,
        category: data.get("category").and_then(|x| x.as_str()).map(|s| s.to_string()),
        use_graph,
        hops,
        drop_hearsay: true,
        max_synthesis: 1,
        temporal_intent: Some(temporal_intent.clone()),
        as_of,
        prefer_layers: None,
        tenant_id: if tenant.is_empty() { None } else { Some(tenant) },
        precomputed: st.eng.embedder.mem_get(&query),
    };
    let mut resp = if engine == "reflect" {
        match reflect_ask(&st.eng, &query, ro) {
            Ok(v) => v,
            Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
        }
    } else if engine == "legacy" {
        let raw = st.eng.search(&query, SearchOpts { top_k: (top_k * 4).max(12), use_hybrid, precomputed: st.eng.embedder.mem_get(&query), ..Default::default() }).unwrap_or_default();
        let packed = pack_results(&raw, &query, &PackOpts::ask_default(top_k, max_chars));
        let pack = format_ask_pack(&query, &packed, max_total);
        json!({"status":"ok","query": query, "pack": pack, "results": packed, "count": packed.len(), "engine":"legacy"})
    } else {
        match ultimate_ask(&st.eng, &query, ro, &llm_deep, llm_answer, rerank, reader).await {
            Ok(v) => v,
            Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
        }
    };
    resp["elapsed_ms"] = json!((t0.elapsed().as_secs_f64() * 1000.0 * 10.0).round() / 10.0);
    resp["result_cache_hit"] = json!(false);
    resp["result_cache"] = st.eng.cache_stats();
    resp["version"] = json!("v4.0");
    if !no_cache {
        st.eng.cache_put(ck, resp.clone());
    }
    (StatusCode::OK, Json(resp))
}

async fn search_rerank(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let query = data.get("query").or(data.get("q")).and_then(|x| x.as_str()).unwrap_or("").to_string();
    if query.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"query is required"})));
    }
    let top_k = data.get("top_k").and_then(|x| x.as_u64()).unwrap_or(10) as usize;
    let _ = st.eng.embedder.embed(&query).await;
    let mut res = st.eng.search(&query, SearchOpts { top_k, use_hybrid: true, precomputed: st.eng.embedder.mem_get(&query), ..Default::default() }).unwrap_or_default();
    crate::rerank::apply_rerank(&query, &mut res, data.get("rerank_candidates").and_then(|x| x.as_u64()).unwrap_or(5) as usize).await;
    (StatusCode::OK, Json(json!({"status":"ok","results": res, "rerank_backend":"qwen3-rerank"})))
}

async fn reranker_status() -> Json<Value> {
    Json(crate::rerank::status_json())
}

async fn rewrite_search(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let query = data.get("query").or(data.get("q")).and_then(|x| x.as_str()).unwrap_or("").to_string();
    if query.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"query is required"})));
    }
    let n = data.get("n_rewrites").and_then(|x| x.as_u64()).unwrap_or(2) as usize;
    let rewrites = crate::llm::rewrite_queries(&query, n).await;
    let mut all: HashMap<i64, crate::types::Hit> = HashMap::new();
    for rw in &rewrites {
        let _ = st.eng.embedder.embed(rw).await;
        if let Ok(rs) = st.eng.search(rw, SearchOpts { top_k: 10, use_hybrid: true, precomputed: st.eng.embedder.mem_get(rw), ..Default::default() }) {
            for r in rs {
                all.entry(r.id).and_modify(|e| { if r.score > e.score { *e = r.clone(); } }).or_insert(r);
            }
        }
    }
    let mut merged: Vec<_> = all.into_values().collect();
    merged.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap());
    (StatusCode::OK, Json(json!({"status":"ok","results": merged, "rewrites": rewrites})))
}

async fn ingest(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let Some(fp) = data.get("filepath").and_then(|x| x.as_str()) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"filepath is required"})));
    };
    let chunk = data.get("chunk_size").and_then(|x| x.as_u64()).unwrap_or(500) as usize;
    let overlap = data.get("overlap").and_then(|x| x.as_u64()).unwrap_or(80) as usize;
    let imp = data.get("importance").and_then(|x| x.as_f64()).unwrap_or(0.5);
    let cat = data.get("category").and_then(|x| x.as_str());
    let title = data.get("section_title").and_then(|x| x.as_str());
    if !std::path::Path::new(fp).exists() {
        return (StatusCode::OK, Json(json!({"status":"ok","added": 0})));
    }
    if image::is_image_path(fp) {
        let fallback = std::path::Path::new(fp)
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or(fp)
            .to_string();
        let cap = title.unwrap_or(fallback.as_str());
        match add_with_image(&st, cap, fp, cat.or(Some("image")), imp, json!({}), Some(fp), None).await {
            Ok(r) => {
                let n = if r.get("is_duplicate") == Some(&json!(true)) { 0 } else { 1 };
                return (StatusCode::OK, Json(json!({"status":"ok","added": n})));
            }
            Err(e) => return (StatusCode::BAD_REQUEST, Json(json!({"error": e}))),
        }
    }
    let text = match std::fs::read_to_string(fp) {
        Ok(t) => t,
        Err(e) => return (StatusCode::BAD_REQUEST, Json(json!({"error": e.to_string()}))),
    };
    let mut added = 0i64;
    for s in Engine::ingest_split(&text, chunk.max(80), overlap) {
        if s.trim().is_empty() { continue; }
        let Ok(vec) = st.eng.embedder.embed(&s).await else { continue; };
        if let Ok(r) = st.eng.add(&s, fp, cat, None, imp, json!({}), true, None, vec) {
            if r.get("is_duplicate") != Some(&json!(true)) {
                added += 1;
            }
        }
    }
    (StatusCode::OK, Json(json!({"status":"ok","added": added})))
}

async fn add_with_image(
    st: &AppState,
    content: &str,
    source: &str,
    category: Option<&str>,
    importance: f64,
    mut meta: Value,
    image_ref: Option<&str>,
    image_bytes: Option<(Vec<u8>, String)>,
) -> Result<Value, String> {
    let mut content = content.to_string();
    let dir = st.eng.image_dir.clone();
    let resolved = if let Some((bytes, name)) = image_bytes {
        image::resolve_bytes(bytes, &name, Some(&dir)).map_err(|e| e.to_string())?
    } else if let Some(r) = image_ref {
        image::resolve(r, Some(&dir)).await.map_err(|e| e.to_string())?
    } else {
        return Err("image 为空".into());
    };
    if content.trim().is_empty() {
        content = format!("[image] {}", resolved.name);
    }
    let vec = st
        .eng
        .embedder
        .embed_image(&resolved.data_uri, None)
        .await
        .map_err(|e| e.to_string())?;
    if !meta.is_object() {
        meta = json!({});
    }
    if let Some(o) = meta.as_object_mut() {
        if let serde_json::Value::Object(m) = image::meta_fields(&resolved) {
            o.extend(m);
        }
    }
    let h = crate::embed::content_hash(&format!("image:{}\n{content}", resolved.sha256));
    let extra = AddExtra {
        content_hash_override: Some(h),
        modality_image: true,
        skip_secret_scan: false,
        ..Default::default()
    };
    st.eng
        .add_ex(
            &content,
            source,
            category.or(Some("image")),
            None,
            importance,
            meta,
            false,
            None,
            vec,
            extra,
        )
        .map_err(|e| e.to_string())
}

async fn stats(State(st): State<AppState>) -> impl IntoResponse {
    match st.eng.stats() {
        Ok(v) => (StatusCode::OK, Json(v)),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    }
}

async fn memory_dupes(Query(q): Query<HashMap<String, String>>, State(st): State<AppState>) -> impl IntoResponse {
    let limit = qint(&q, "limit", 50);
    match st.eng.duplicate_hashes(limit) {
        Ok(v) => (StatusCode::OK, Json(v)),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    }
}

async fn memory_gc(State(st): State<AppState>) -> Json<Value> {
    Json(st.eng.gc())
}

async fn memory_infer_layer(State(st): State<AppState>, body: Option<Json<Value>>) -> impl IntoResponse {
    let data = body.map(|j| j.0).unwrap_or(json!({}));
    let limit = data.get("limit").and_then(|x| x.as_i64()).unwrap_or(2000);
    let dry_run = parse_bool(data.get("dry_run"), false);
    match st.eng.infer_layers(limit, dry_run) {
        Ok(v) => (StatusCode::OK, Json(merge_ok(v))),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    }
}

async fn tags_cloud(Query(q): Query<HashMap<String, String>>, State(st): State<AppState>) -> impl IntoResponse {
    let limit = qint(&q, "limit", 30);
    match st.eng.tags_cloud(limit) {
        Ok(v) => (StatusCode::OK, Json(v)),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    }
}
async fn tags_by_cat(State(st): State<AppState>) -> Json<Value> {
    let v = st.eng.with_conn(|c| {
        let mut stt = c.prepare("SELECT category, COUNT(*) FROM tags WHERE category IS NOT NULL GROUP BY category")?;
        let mut m = serde_json::Map::new();
        for row in stt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?.flatten() {
            m.insert(row.0, json!(row.1));
        }
        Ok(Value::Object(m))
    }).unwrap_or(json!({}));
    Json(v)
}
async fn tags_rename(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let old = data.get("old").and_then(|x| x.as_str()).unwrap_or("");
    let new = data.get("new").and_then(|x| x.as_str()).unwrap_or("");
    if old.is_empty() || new.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"old and new names required"})));
    }
    let n = st.eng.with_conn(|c| c.execute("UPDATE tags SET name=? WHERE name=?", rusqlite::params![new, old])).unwrap_or(0);
    (StatusCode::OK, Json(json!({"status":"ok","affected": n})))
}
async fn tags_merge(State(st): State<AppState>, Json(data): Json<Value>) -> Json<Value> {
    let from = data.get("from").and_then(|x| x.as_array()).cloned().unwrap_or_default();
    let to = data.get("to").and_then(|x| x.as_str()).unwrap_or("");
    let mut total = 0;
    for t in from {
        if let Some(name) = t.as_str() {
            total += st.eng.with_conn(|c| c.execute("UPDATE tags SET name=? WHERE name=?", rusqlite::params![to, name])).unwrap_or(0);
        }
    }
    Json(json!({"status":"ok","affected": total}))
}
async fn tags_cleanup(State(st): State<AppState>) -> Json<Value> {
    let n = st.eng.with_conn(|c| c.execute("DELETE FROM tags WHERE usage_count=0 OR usage_count IS NULL", [])).unwrap_or(0);
    Json(json!({"status":"ok","cleaned": n}))
}

async fn memory_add(headers: HeaderMap, State(st): State<AppState>, req: Request<Body>) -> Response {
    let ct = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    if ct.contains("multipart/") {
        match Multipart::from_request(req, &st).await {
            Ok(mp) => return memory_add_multipart(headers, st, mp).await.into_response(),
            Err(e) => {
                return (StatusCode::BAD_REQUEST, Json(json!({"error": e.to_string()}))).into_response()
            }
        }
    }
    let collected = match req.into_body().collect().await {
        Ok(c) => c.to_bytes(),
        Err(e) => return (StatusCode::BAD_REQUEST, Json(json!({"error": e.to_string()}))).into_response(),
    };
    let data: Value = if collected.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(&collected).unwrap_or(json!({}))
    };
    memory_add_json(headers, st, data).await.into_response()
}

fn add_err(e: String) -> (StatusCode, Json<Value>) {
    if e.starts_with("SECRET_REJECTED") || e.contains("secret_in_content") {
        (
            StatusCode::BAD_REQUEST,
            Json(json!({"status":"rejected","error":"secret_in_content","message": e, "hint":"POST /secrets/store 写入 Vaultwarden"})),
        )
    } else {
        (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e})))
    }
}

async fn memory_add_json(headers: HeaderMap, st: AppState, data: Value) -> impl IntoResponse {
    let content = data.get("content").and_then(|x| x.as_str()).unwrap_or("").to_string();
    let source = data.get("source").and_then(|x| x.as_str()).unwrap_or("api").to_string();
    let mut meta = data.get("metadata").cloned().unwrap_or(json!({}));
    let tenant = crate::site::resolve_tenant(
        data.get("tenant_id").and_then(|x| x.as_str()).or_else(|| headers.get("X-Nebula-Tenant").and_then(|v| v.to_str().ok())),
    );
    if crate::site::require_tenant() && tenant.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"tenant_id required"}))).into_response();
    }
    if !tenant.is_empty() {
        if let Some(o) = meta.as_object_mut() {
            o.insert("tenant_id".into(), json!(tenant));
        }
    }
    let image = data.get("image").or(data.get("image_url")).or(data.get("image_path")).and_then(|x| x.as_str());
    if content.is_empty() && image.is_none() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"content required"}))).into_response();
    }
    if let Some(img) = image {
        return match add_with_image(&st, &content, &source, data.get("category").and_then(|x| x.as_str()), data.get("importance").and_then(|x| x.as_f64()).unwrap_or(0.5), meta, Some(img), None).await {
            Ok(r) => (StatusCode::OK, Json(merge_ok(r))).into_response(),
            Err(e) => add_err(e).into_response(),
        };
    }
    let vec = match st.eng.embedder.embed(&content).await {
        Ok(v) => v,
        Err(e) => return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": e.to_string()}))).into_response(),
    };
    let tags: Option<Vec<String>> = data.get("tags").and_then(|x| x.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect());
    match st.eng.add(
        &content,
        &source,
        data.get("category").and_then(|x| x.as_str()),
        tags.as_deref(),
        data.get("importance").and_then(|x| x.as_f64()).unwrap_or(0.5),
        meta,
        parse_bool(data.get("semantic_dedup"), true),
        parse_as_of(data.get("created_at")),
        vec,
    ) {
        Ok(r) => (StatusCode::OK, Json(merge_ok(r))).into_response(),
        Err(e) => add_err(e.to_string()).into_response(),
    }
}

async fn memory_add_multipart(headers: HeaderMap, st: AppState, mut mp: Multipart) -> impl IntoResponse {
    let mut content = String::new();
    let mut source = "api".to_string();
    let mut category = None::<String>;
    let mut importance = 0.5f64;
    let mut image_bytes = None::<(Vec<u8>, String)>;
    while let Ok(Some(field)) = mp.next_field().await {
        let name = field.name().unwrap_or("").to_string();
        let fname = field.file_name().unwrap_or("").to_string();
        let bytes = field.bytes().await.unwrap_or_default();
        match name.as_str() {
            "content" => content = String::from_utf8_lossy(&bytes).into(),
            "source" => source = String::from_utf8_lossy(&bytes).into(),
            "category" => category = Some(String::from_utf8_lossy(&bytes).into()),
            "importance" => importance = String::from_utf8_lossy(&bytes).parse().unwrap_or(0.5),
            "image" => image_bytes = Some((bytes.to_vec(), fname)),
            _ => {}
        }
    }
    let tenant = crate::site::resolve_tenant(headers.get("X-Nebula-Tenant").and_then(|v| v.to_str().ok()));
    let mut meta = json!({});
    if !tenant.is_empty() {
        meta["tenant_id"] = json!(tenant);
    }
    if let Some(pair) = image_bytes {
        match add_with_image(&st, &content, &source, category.as_deref(), importance, meta, None, Some(pair)).await {
            Ok(r) => (StatusCode::OK, Json(merge_ok(r))).into_response(),
            Err(e) => add_err(e).into_response(),
        }
    } else {
        memory_add_json(headers, st, json!({"content": content, "source": source, "category": category, "importance": importance})).await.into_response()
    }
}

async fn memory_image(Path(id): Path<i64>, State(st): State<AppState>) -> Response {
    let meta = match st.eng.memory_meta(id) {
        Ok(Some(m)) => m,
        _ => return (StatusCode::NOT_FOUND, Json(json!({"error":"not found"}))).into_response(),
    };
    if meta.get("modality").and_then(|x| x.as_str()) != Some("image") {
        return (StatusCode::NOT_FOUND, Json(json!({"error":"not an image memory"}))).into_response();
    }
    let path = meta.get("image_path").and_then(|x| x.as_str()).unwrap_or("");
    if path.is_empty() {
        let src = meta.get("image_src").and_then(|x| x.as_str()).unwrap_or("");
        if src.starts_with("http") {
            return (StatusCode::FOUND, Json(json!({"status":"redirect","url": src}))).into_response();
        }
        return (StatusCode::NOT_FOUND, Json(json!({"error":"no image_path"}))).into_response();
    }
    let root = st.eng.image_dir.canonicalize().unwrap_or_else(|_| st.eng.image_dir.clone());
    let real = match std::fs::canonicalize(path) {
        Ok(p) => p,
        Err(_) => return (StatusCode::NOT_FOUND, Json(json!({"error":"file missing"}))).into_response(),
    };
    if !real.starts_with(&root) {
        return StatusCode::FORBIDDEN.into_response();
    }
    match std::fs::read(&real) {
        Ok(b) => {
            let mime = meta.get("image_mime").and_then(|x| x.as_str()).unwrap_or("application/octet-stream");
            Response::builder().header(header::CONTENT_TYPE, mime).body(Body::from(b)).unwrap()
        }
        Err(_) => (StatusCode::NOT_FOUND, Json(json!({"error":"file missing"}))).into_response(),
    }
}

async fn memory_promote(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let mut body = data.get("content").and_then(|x| x.as_str()).unwrap_or("").to_string();
    let mut title = data.get("title").and_then(|x| x.as_str()).unwrap_or("").to_string();
    let mut src = data.get("source").and_then(|x| x.as_str()).unwrap_or("promote").to_string();
    let mid = data.get("id").and_then(|x| x.as_i64());
    if let Some(id) = mid {
        if body.is_empty() {
            if let Ok(Some((c, s, _))) = st.eng.fetch_one(id) {
                body = c;
                src = s.unwrap_or(src);
            }
        }
    }
    if body.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"content or id required"})));
    }
    if title.is_empty() {
        title = body.lines().next().unwrap_or("untitled").chars().take(40).collect();
    }
    let slug: String = regex::Regex::new(r"[^\w\u4e00-\u9fff-]+")
        .unwrap()
        .replace_all(&title, "-")
        .trim_matches('-')
        .chars()
        .take(40)
        .collect();
    let slug = if slug.is_empty() { "note".into() } else { slug };
    let day = chrono::Local::now().format("%Y-%m-%d").to_string();
    // 目标目录与回读命令均可配置，不写死机器路径
    let subdir = std::env::var("NEBULA_PROMOTE_SUBDIR").unwrap_or_else(|_| "06-Agent会话提炼".into());
    let key_prefix = std::env::var("NEBULA_VAULT_KEY_PREFIX").unwrap_or_else(|_| "notes/".into());
    let wrap_label = std::env::var("NEBULA_VAULT_WRAP_LABEL").unwrap_or_else(|_| "[笔记]".into());
    let dest_dir = crate::vault::vault_root().join(&subdir);
    if std::fs::create_dir_all(&dest_dir).is_err() {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": format!("cannot create {}（配置 NEBULA_VAULT_ROOT / --vault-root）", dest_dir.display())})),
        );
    }
    let mut dest = dest_dir.join(format!("{day}-{slug}.md"));
    let mut n = 2;
    while dest.exists() {
        dest = dest_dir.join(format!("{day}-{slug}-{n}.md"));
        n += 1;
    }
    let text = format!(
        "---\ntitle: {}\ndate: {day}\nsource: {src}\nnebula_id: {}\ntrust: source\n---\n\n# {title}\n\n{body}\n\n> 由星枢 /memory/promote 写回。冲突以本笔记为准。\n",
        title.replace('\n', " "),
        mid.map(|x| x.to_string()).unwrap_or_default()
    );
    if std::fs::write(&dest, text).is_err() {
        return (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error":"write failed"})));
    }
    let rel = format!("{key_prefix}{subdir}/{}", dest.file_name().unwrap().to_string_lossy());
    let vault_key = format!("vault:{rel}");
    let readback = crate::site::format_readback(&vault_key)
        .filter(|s| s != &vault_key)
        .unwrap_or_else(|| format!("read \"{}\"", dest.to_string_lossy().replace('\\', "/")));
    let content = format!(
        "{wrap_label} path={rel} source={vault_key} title={title}\n回读命令: {readback}\n---\n{}",
        body.chars().take(4000).collect::<String>()
    );
    let vec = st.eng.embedder.embed(&content).await.unwrap_or_default();
    let added = st.eng.add(&content, &vault_key, Some("lesson"), None, 0.8, json!({}), false, None, vec);
    (StatusCode::OK, Json(json!({"status":"ok","path": dest, "vault_key": vault_key, "nebula": added.ok()})))
}

async fn vault_status(State(st): State<AppState>) -> Json<Value> {
    Json(crate::vault::status(&st.eng))
}

async fn vault_sync(State(st): State<AppState>, body: Option<Json<Value>>) -> impl IntoResponse {
    let data = body.map(|j| j.0).unwrap_or(json!({}));
    let opts = crate::vault::SyncOpts {
        force: parse_bool(data.get("force"), false),
        dry_run: parse_bool(data.get("dry_run"), false),
        only: data.get("only").and_then(|x| x.as_str()).filter(|s| !s.is_empty()).map(String::from),
        prune: parse_bool(data.get("prune"), true),
    };
    match crate::vault::sync(&st.eng, opts).await {
        Ok(v) => (StatusCode::OK, Json(merge_ok(v))),
        Err(e) => {
            let msg = e.to_string();
            let code = if msg.contains("already running") {
                StatusCode::CONFLICT
            } else {
                StatusCode::INTERNAL_SERVER_ERROR
            };
            (code, Json(json!({"status":"error","error": msg})))
        }
    }
}

async fn memory_delete(Path(id): Path<i64>, State(st): State<AppState>) -> Json<Value> {
    let _ = st.eng.delete(id);
    Json(json!({"status":"ok"}))
}
async fn memory_update(Path(id): Path<i64>, State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let content = data.get("content").and_then(|x| x.as_str());
    let mut new_vec = None;
    if let Some(c) = content {
        new_vec = st.eng.embedder.embed(c).await.ok();
    }
    let tags: Option<Vec<String>> = data.get("tags").and_then(|x| x.as_array()).map(|a| a.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect());
    match st.eng.update_fields(id, content, data.get("category").and_then(|x| x.as_str()), data.get("importance").and_then(|x| x.as_f64()), tags.as_deref(), new_vec) {
        Ok(v) => (StatusCode::OK, Json(v)),
        Err(e) if e.to_string() == "not_found" => (StatusCode::NOT_FOUND, Json(json!({"status":"error","error":"not_found","id": id}))),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    }
}

async fn reclassify(State(st): State<AppState>, Json(data): Json<Value>) -> Json<Value> {
    let r = st.eng.batch_recategorize(data.get("category").and_then(|x| x.as_str()), data.get("limit").and_then(|x| x.as_i64()).unwrap_or(100), parse_bool(data.get("dry_run"), false)).unwrap_or(json!({}));
    Json(json!({"status":"ok","result": r}))
}
async fn compress_one(st: &AppState, date: &str) -> Result<String, String> {
    let (rows, skipped) = st.eng.compress_date_rows(date).map_err(|e| e.to_string())?;
    if skipped || rows.is_empty() {
        return Ok(String::new());
    }
    let mut combined = String::new();
    for (_id, content, cat) in &rows {
        combined.push_str(&format!(
            "[{}] {}\n\n",
            cat.as_deref().unwrap_or(""),
            content.chars().take(500).collect::<String>()
        ));
    }
    let prompt = format!(
        "以下是 {date} 的 {} 条记忆，请提炼为 3-5 条要点：\n\n{}",
        rows.len(),
        combined.chars().take(12000).collect::<String>()
    );
    let mut summary = crate::llm::llm_chat(
        "你是一个记忆压缩助手。将输入的笔记提炼为3-5条简洁要点，中文输出。保留关键信息（项目名、技术决策、教训），去掉流水账。",
        &prompt,
        2000,
        0.3,
        12.0,
    )
    .await;
    if summary.is_empty() {
        summary = format!("📅 {date} 有 {} 条记忆（LLM 不可用，待压缩）", rows.len());
    }
    let ids: Vec<i64> = rows.iter().map(|r| r.0).collect();
    st.eng.compress_date_commit(date, &summary, &ids).map_err(|e| e.to_string())?;
    let body = format!("【{date} 摘要】{summary}");
    if let Ok(vec) = st.eng.embedder.embed(&body).await {
        let _ = st.eng.add(&body, "compressor", Some("summary"), None, 0.7, json!({}), true, None, vec);
    }
    let _ = st.eng.reload_matrix();
    Ok(summary)
}

async fn compress(State(st): State<AppState>, body: Option<Json<Value>>) -> impl IntoResponse {
    let data = body.map(|j| j.0).unwrap_or(json!({}));
    if let Some(d) = data.get("date").and_then(|x| x.as_str()) {
        match compress_one(&st, d).await {
            Ok(summary) => (StatusCode::OK, Json(json!({"status":"ok","date": d, "summary": summary}))).into_response(),
            Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e}))).into_response(),
        }
    } else {
        let days = data.get("days_ago").and_then(|x| x.as_i64()).unwrap_or(30);
        let dates = st.eng.compress_dates_before(days).unwrap_or_default();
        let mut results = serde_json::Map::new();
        for d in dates {
            if let Ok(s) = compress_one(&st, &d).await {
                if !s.is_empty() {
                    results.insert(d, json!(s));
                }
            }
        }
        (
            StatusCode::OK,
            Json(json!({"status":"ok","compressed_dates": results.len(), "results": results})),
        )
            .into_response()
    }
}
async fn ui() -> Response {
    let p = "/opt/nebula/scripts/vm-search-ui.html";
    if let Ok(b) = std::fs::read(p) {
        return Response::builder()
            .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
            .body(Body::from(b))
            .unwrap();
    }
    let html = r#"<!doctype html><meta charset=utf-8><title>星枢</title>
<style>body{font:16px sans-serif;max-width:880px;margin:24px auto;padding:0 12px}textarea{width:100%;height:80px}pre{white-space:pre-wrap;background:#111;color:#eee;padding:12px}</style>
<h1>星枢 /ask</h1>
<textarea id=q placeholder=query></textarea>
<button onclick="go()">检索</button>
<pre id=o></pre>
<script>
async function go(){
  const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:document.getElementById('q').value,top_k:5,llm_deep:'off'})});
  document.getElementById('o').textContent=JSON.stringify(await r.json(),null,2);
}
</script>"#;
    Response::builder()
        .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
        .body(Body::from(html))
        .unwrap()
}

async fn secrets_status() -> Json<Value> { Json(secrets::bw_status()) }
async fn secrets_list(Query(q): Query<HashMap<String, String>>) -> impl IntoResponse {
    match secrets::list_items(qint(&q, "limit", 100) as usize) {
        Ok(rows) => (StatusCode::OK, Json(json!({"status":"ok","count": rows.len(), "items": rows}))),
        Err(e) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"status":"error","error": e.to_string()}))),
    }
}
async fn secrets_get(Json(data): Json<Value>) -> impl IntoResponse {
    let name = data.get("name").or(data.get("item")).and_then(|x| x.as_str()).unwrap_or("");
    if name.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"name required"})));
    }
    if !parse_bool(data.get("reveal"), false) {
        return (StatusCode::OK, Json(json!({"status":"ok","name": name, "revealed": false, "hint":"加 reveal:true 才返回明文","command": format!("bw-ai get {name}")})));
    }
    match secrets::get_password(name) {
        Ok(pw) => {
            let user = secrets::get_username(name).unwrap_or_default();
            (StatusCode::OK, Json(json!({"status":"ok","name": name, "username": user, "password": pw, "revealed": true, "warning":"禁止写入星枢/笔记"})))
        }
        Err(e) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"status":"error","error": e.to_string()}))),
    }
}
async fn secrets_store(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let name = data.get("name").and_then(|x| x.as_str()).unwrap_or("");
    let password = data.get("password").or(data.get("secret")).or(data.get("value")).and_then(|x| x.as_str()).unwrap_or("");
    if name.is_empty() || password.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"name and password/secret required"})));
    }
    let username = data.get("username").or(data.get("user")).and_then(|x| x.as_str()).unwrap_or("");
    let notes = data.get("notes").or(data.get("purpose")).and_then(|x| x.as_str()).unwrap_or("");
    match secrets::create_login_item(name, username, password, notes) {
        Ok(created) => {
            let mut mem = Value::Null;
            if parse_bool(data.get("register_memory"), true) {
                let c = secrets::pointer_content(name, notes, username);
                if let Ok(vec) = st.eng.embedder.embed(&c).await {
                    mem = st.eng.add(&c, "vaultwarden", Some("credential"), None, 0.85, json!({"trust":"canon","secret_ref": name}), true, None, vec).unwrap_or(Value::Null);
                }
            }
            (StatusCode::OK, Json(json!({"status":"ok","vaultwarden": created, "memory_pointer": mem})))
        }
        Err(e) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"status":"error","error": e.to_string()}))),
    }
}
async fn secrets_register(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let name = data.get("name").and_then(|x| x.as_str()).unwrap_or("");
    if name.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"name required"})));
    }
    let c = secrets::pointer_content(name, data.get("purpose").and_then(|x| x.as_str()).unwrap_or(""), data.get("username").and_then(|x| x.as_str()).unwrap_or(""));
    let vec = st.eng.embedder.embed(&c).await.unwrap_or_default();
    match st.eng.add(&c, "vaultwarden", Some("credential"), None, 0.85, json!({"trust":"canon","secret_ref": name}), true, None, vec) {
        Ok(r) => {
            let mut v = merge_ok(r);
            v["name"] = json!(name);
            v["pointer"] = json!(true);
            (StatusCode::OK, Json(v))
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    }
}
async fn secrets_catalog(State(st): State<AppState>) -> Json<Value> {
    let items = secrets::list_items(80).unwrap_or_default();
    let mut added = 0;
    let mut skipped = 0;
    for it in &items {
        let name = it.get("name").and_then(|x| x.as_str()).unwrap_or("");
        if name.is_empty() { continue; }
        let c = secrets::pointer_content(name, "密钥", it.get("username").and_then(|x| x.as_str()).unwrap_or(""));
        if let Ok(vec) = st.eng.embedder.embed(&c).await {
            if let Ok(r) = st.eng.add(&c, "vaultwarden", Some("credential"), None, 0.8, json!({"secret_ref": name}), true, None, vec) {
                if r.get("is_duplicate") == Some(&json!(true)) { skipped += 1; } else { added += 1; }
            }
        }
    }
    Json(json!({"status":"ok","added": added, "skipped": skipped, "listed": items.len()}))
}
async fn secrets_resolve(Json(data): Json<Value>) -> impl IntoResponse {
    let q = data.get("query").or(data.get("q")).and_then(|x| x.as_str()).unwrap_or("");
    match secrets::list_items(100) {
        Ok(items) => {
            let matches = secrets::resolve_for_query(q, &items);
            (StatusCode::OK, Json(json!({"status":"ok","query": q, "matches": matches, "count": matches.len()})))
        }
        Err(e) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"status":"error","error": e.to_string()}))),
    }
}

async fn v5_extract(State(st): State<AppState>, Json(data): Json<Value>) -> Json<Value> {
    let transcript = data.get("transcript").or(data.get("notes")).or(data.get("session")).and_then(|x| x.as_str()).unwrap_or("");
    let focus = data.get("focus").or(data.get("query")).and_then(|x| x.as_str()).unwrap_or("");
    let dry = parse_bool(data.get("dry_run"), false);
    let auto = if dry { false } else { parse_bool(data.get("auto_write"), true) };
    let max_items = data.get("max_items").and_then(|x| x.as_u64()).unwrap_or(8) as usize;
    Json(session_extract(&st.eng, transcript, focus, dry, max_items, auto).await.unwrap_or(json!({"status":"error"})))
}
async fn v5_layered(Query(qs): Query<HashMap<String, String>>, body: Option<Json<Value>>) -> Json<Value> {
    let focus = body
        .and_then(|j| j.0.get("focus").or(j.0.get("query")).or(j.0.get("q")).and_then(|x| x.as_str()).map(|s| s.to_string()))
        .or_else(|| qs.get("focus").or(qs.get("q")).cloned())
        .unwrap_or_default();
    Json(layered_recall_plan(&focus))
}
async fn v5_promote(State(st): State<AppState>, Json(data): Json<Value>) -> Json<Value> {
    let q = data.get("query").or(data.get("evidence_query")).and_then(|x| x.as_str()).unwrap_or("");
    let notes = data.get("notes").or(data.get("session_notes")).and_then(|x| x.as_str()).unwrap_or("");
    Json(merge_ok(promote_draft(&st.eng, q, notes).await.unwrap_or(json!({}))))
}
async fn v5_bootstrap(method: Method, Query(qs): Query<HashMap<String, String>>, State(st): State<AppState>, body: Option<Json<Value>>) -> Json<Value> {
    let t0 = Instant::now();
    let data = if method == Method::GET { json!({}) } else { body.map(|j| j.0).unwrap_or(json!({})) };
    let focus = if method == Method::GET {
        qs.get("focus").or(qs.get("q")).cloned().unwrap_or_default()
    } else {
        data.get("focus").or(data.get("query")).or(data.get("q")).and_then(|x| x.as_str()).unwrap_or("").to_string()
    };
    let budget = if method == Method::GET { qint(&qs, "budget_chars", 2400) as usize } else { data.get("budget_chars").and_then(|x| x.as_u64()).unwrap_or(2400) as usize };
    let no_cache = if method == Method::GET { qbool(&qs, "no_cache", false) } else { parse_bool(data.get("no_cache"), false) };
    let ck = cache_key("bootstrap_v5", &json!({"focus": focus, "budget": budget}));
    if !no_cache {
        if let Some(hit) = st.eng.cache_get(&ck) {
            let mut out = hit;
            out["elapsed_ms"] = json!((t0.elapsed().as_secs_f64()*1000.0*10.0).round()/10.0);
            out["result_cache_hit"] = json!(true);
            return Json(out);
        }
    }
    let _ = st.eng.embedder.embed(if focus.is_empty() { "虎虎现行架构 星枢用法 工作流" } else { &focus }).await;
    let mut resp = bootstrap(&st.eng, &focus, budget).await.unwrap_or(json!({"status":"error"}));
    resp["result_cache_hit"] = json!(false);
    resp["result_cache"] = st.eng.cache_stats();
    if !no_cache {
        st.eng.cache_put(ck, resp.clone());
    }
    Json(resp)
}
async fn v5_answer(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let query = data.get("query").and_then(|x| x.as_str()).unwrap_or("");
    if query.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"query required"})));
    }
    let _ = st.eng.embedder.embed(query).await;
    let r = ultimate_ask(
        &st.eng, query,
        ReflectOpts {
            top_k: data.get("top_k").and_then(|x| x.as_u64()).unwrap_or(5) as usize,
            max_chars: 280, max_total: 1800, use_hybrid: true, category: None,
            use_graph: parse_bool(data.get("use_graph"), true),
            hops: data.get("hops").and_then(|x| x.as_u64()).unwrap_or(2) as usize,
            drop_hearsay: true, max_synthesis: 1, temporal_intent: None, as_of: None,
            prefer_layers: None, tenant_id: None, precomputed: st.eng.embedder.mem_get(query),
        },
        data.get("llm_deep").and_then(|x| x.as_str()).unwrap_or("auto"),
        parse_bool(data.get("llm_answer"), false),
        true, false,
    ).await;
    match r {
        Ok(v) => (StatusCode::OK, Json(v)),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    }
}
async fn v5_lifecycle(State(st): State<AppState>, body: Option<Json<Value>>) -> Json<Value> {
    let data = body.map(|j| j.0).unwrap_or(json!({}));
    Json(merge_ok(st.eng.lifecycle(data.get("demote_days").and_then(|x| x.as_i64()).unwrap_or(21), data.get("demote_max").and_then(|x| x.as_i64()).unwrap_or(300)).unwrap_or(json!({}))))
}
async fn v5_health(State(st): State<AppState>) -> Json<Value> {
    let mut v = st.eng.health_report().unwrap_or(json!({"status":"error"}));
    // secrets 解锁加分
    let stt = secrets::bw_status();
    if stt.get("ok") == Some(&json!(true)) || stt.get("status").and_then(|x| x.as_str()) == Some("unlocked") {
        if let Some(m) = v.get_mut("maturity") {
            if let Some(s) = m.get("score").and_then(|x| x.as_i64()) {
                m["score"] = json!((s + 8).min(100));
            }
        }
    }
    Json(v)
}
async fn memory_related(Path(id): Path<i64>, Query(q): Query<HashMap<String, String>>, State(st): State<AppState>) -> Json<Value> {
    let limit = qint(&q, "limit", 8);
    Json(json!({"status":"ok","id": id, "related": st.eng.related(id, limit).unwrap_or(json!([]))}))
}
async fn memory_supersede(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let old = data.get("old_id").or(data.get("id")).and_then(|x| x.as_i64());
    let Some(old) = old else { return (StatusCode::BAD_REQUEST, Json(json!({"error":"old_id required"}))); };
    let new = data.get("new_id").and_then(|x| x.as_i64());
    let note = data.get("note").and_then(|x| x.as_str()).unwrap_or("");
    match st.eng.supersede(old, new, note) {
        Ok(r) => (StatusCode::OK, Json(merge_ok(r))),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    }
}
async fn v4_migrate(State(st): State<AppState>, body: Option<Json<Value>>) -> Json<Value> {
    let data = body.map(|j| j.0).unwrap_or(json!({}));
    let migrate = st.eng.migrate_schema().unwrap_or(json!({"error":"migrate fail"}));
    let n = st.eng.backfill_trust(data.get("limit").and_then(|x| x.as_i64()).unwrap_or(0)).unwrap_or(0);
    let links = if parse_bool(data.get("rebuild_links"), true) {
        st.eng.rebuild_links().unwrap_or(json!({}))
    } else {
        json!({})
    };
    Json(json!({"status":"ok","migrate": migrate, "trust_updated": n, "links": links}))
}
async fn v4_reflect(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let query = data.get("query").and_then(|x| x.as_str()).unwrap_or("");
    if query.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"query required"})));
    }
    let _ = st.eng.embedder.embed(query).await;
    match reflect_ask(&st.eng, query, ReflectOpts {
        top_k: data.get("top_k").and_then(|x| x.as_u64()).unwrap_or(5) as usize,
        max_chars: data.get("max_chars").and_then(|x| x.as_u64()).unwrap_or(280) as usize,
        max_total: 1800, use_hybrid: true,
        category: data.get("category").and_then(|x| x.as_str()).map(|s| s.to_string()),
        use_graph: parse_bool(data.get("use_graph"), true),
        hops: data.get("hops").and_then(|x| x.as_u64()).unwrap_or(2) as usize,
        drop_hearsay: true, max_synthesis: 1, temporal_intent: None, as_of: None,
        prefer_layers: None, tenant_id: None, precomputed: st.eng.embedder.mem_get(query),
    }) {
        Ok(mut r) => { r["version"] = json!("v4.0"); (StatusCode::OK, Json(r)) }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": e.to_string()}))),
    }
}

async fn mcp(State(st): State<AppState>, Json(data): Json<Value>) -> impl IntoResponse {
    let method = data.get("method").and_then(|x| x.as_str()).unwrap_or("");
    let id = data.get("id").cloned();
    match method {
        "initialize" => Json(json!({"jsonrpc":"2.0","id": id, "result":{
            "protocolVersion":"2024-11-05",
            "capabilities":{"tools":{"listChanged": false}},
            "serverInfo":{"name":"nebula-memory","version":"5.1.1"}
        }})).into_response(),
        "tools/list" => {
            let tools = [
                ("ask_memories", "省 token 终极问答（v5 ultimate）。Agent 优先调用。"),
                ("search_memories", "语义检索（pack 省 token）。优先 ask_memories。"),
                ("bootstrap_memories", "会话启动记忆注入包（L0）。"),
                ("add_memory_tool", "Add a new memory to the system."),
                ("get_memory_stats", "Get memory system statistics."),
                ("get_tags_cloud", "Get popular tags from the memory system."),
                ("session_extract_memories", "从会话文本抽取记忆并写入星枢"),
                ("layered_recall", "返回记忆分层调用步骤"),
                ("related_memories", "记忆关系邻居"),
                ("supersede_memory_tool", "标记旧记忆 superseded"),
                ("delete_memory_by_id", "Delete a memory by its ID."),
                ("search_function", "Search for a function by name in the memory system."),
                ("search_class", "Search for a class by name in the memory system."),
                ("compress_memories", "Compress old memories into summaries. Specify a date or days_ago."),
            ];
            let arr: Vec<Value> = tools.iter().map(|(n,d)| json!({"name": n, "description": d, "inputSchema":{"type":"object"}})).collect();
            Json(json!({"jsonrpc":"2.0","id": id, "result":{"tools": arr}})).into_response()
        }
        "tools/call" => {
            let name = data.pointer("/params/name").and_then(|x| x.as_str()).unwrap_or("");
            let args = data.pointer("/params/arguments").cloned().unwrap_or(json!({}));
            let result = mcp_call(&st, name, args).await;
            Json(json!({"jsonrpc":"2.0","id": id, "result":{"content":[{"type":"text","text": result.to_string()}]}})).into_response()
        }
        "notifications/initialized" => Json(json!({"jsonrpc":"2.0"})).into_response(),
        _ => Json(json!({"jsonrpc":"2.0","id": id, "error":{"code":-32601,"message": format!("Method not found: {method}")}})).into_response(),
    }
}

async fn mcp_call(st: &AppState, name: &str, args: Value) -> Value {
    match name {
        "ask_memories" => {
            let q = args.get("query").and_then(|x| x.as_str()).unwrap_or("");
            let _ = st.eng.embedder.embed(q).await;
            ultimate_ask(&st.eng, q, ReflectOpts {
                top_k: args.get("top_k").and_then(|x| x.as_u64()).unwrap_or(5) as usize,
                max_chars: 280, max_total: 1800, use_hybrid: true, category: None, use_graph: true, hops: 2,
                drop_hearsay: true, max_synthesis: 1, temporal_intent: None, as_of: None, prefer_layers: None,
                tenant_id: None, precomputed: st.eng.embedder.mem_get(q),
            }, "auto", false, true, false).await.unwrap_or(json!({"error":"ask fail"}))
        }
        "search_memories" => {
            let q = args.get("query").and_then(|x| x.as_str()).unwrap_or("");
            let top_k = args.get("top_k").and_then(|x| x.as_u64()).unwrap_or(5) as usize;
            let _ = st.eng.embedder.embed(q).await;
            let raw = st.eng.search(q, SearchOpts { top_k: top_k * 3, use_hybrid: true, precomputed: st.eng.embedder.mem_get(q), ..Default::default() }).unwrap_or_default();
            let packed = pack_results(&raw, q, &PackOpts::ask_default(top_k, 280));
            json!({"status":"ok","results": packed, "count": packed.len(), "compact": true})
        }
        "bootstrap_memories" => {
            let f = args.get("focus_query").and_then(|x| x.as_str()).unwrap_or("");
            let b = args.get("budget_chars").and_then(|x| x.as_u64()).unwrap_or(2400) as usize;
            let _ = st.eng.embedder.embed(if f.is_empty() {"虎虎现行架构 星枢用法 工作流"} else {f}).await;
            bootstrap(&st.eng, f, b).await.unwrap_or(json!({}))
        }
        "add_memory_tool" => {
            let c = args.get("content").and_then(|x| x.as_str()).unwrap_or("");
            let Ok(vec) = st.eng.embedder.embed(c).await else { return json!({"error":"embed"}); };
            st.eng.add(c, "mcp", args.get("category").and_then(|x| x.as_str()), None, args.get("importance").and_then(|x| x.as_f64()).unwrap_or(0.5), json!({}), true, None, vec).unwrap_or(json!({}))
        }
        "get_memory_stats" => st.eng.stats().unwrap_or(json!({})),
        "layered_recall" => layered_recall_plan(args.get("focus").and_then(|x| x.as_str()).unwrap_or("")),
        "related_memories" => {
            let id = args.get("memory_id").and_then(|x| x.as_i64()).unwrap_or(0);
            json!({"status":"ok","id": id, "related": st.eng.related(id, args.get("limit").and_then(|x| x.as_i64()).unwrap_or(8)).unwrap_or(json!([]))})
        }
        "delete_memory_by_id" => {
            let id = args.get("memory_id").and_then(|x| x.as_i64()).unwrap_or(0);
            json!({"status":"ok","deleted": st.eng.delete(id).unwrap_or(false), "memory_id": id})
        }
        "supersede_memory_tool" => {
            st.eng.supersede(args.get("old_id").and_then(|x| x.as_i64()).unwrap_or(0), args.get("new_id").and_then(|x| x.as_i64()), args.get("note").and_then(|x| x.as_str()).unwrap_or("")).unwrap_or(json!({}))
        }
        "session_extract_memories" => {
            session_extract(
                &st.eng,
                args.get("transcript").and_then(|x| x.as_str()).unwrap_or(""),
                args.get("focus").and_then(|x| x.as_str()).unwrap_or(""),
                parse_bool(args.get("dry_run"), false),
                8,
                !parse_bool(args.get("dry_run"), false),
            )
            .await
            .unwrap_or(json!({"error":"extract fail"}))
        }
        "get_tags_cloud" => st.eng.tags_cloud(args.get("limit").and_then(|x| x.as_i64()).unwrap_or(20)).unwrap_or(json!([])),
        "search_function" => {
            let name = args.get("func_name").and_then(|x| x.as_str()).unwrap_or("");
            let top_k = args.get("top_k").and_then(|x| x.as_u64()).unwrap_or(5) as usize;
            let _ = st.eng.embedder.embed(name).await;
            let results = st.eng.search_function(name, top_k, st.eng.embedder.mem_get(name)).unwrap_or_default();
            json!({"status":"ok","results": results, "count": results.len()})
        }
        "search_class" => {
            let name = args.get("class_name").and_then(|x| x.as_str()).unwrap_or("");
            let top_k = args.get("top_k").and_then(|x| x.as_u64()).unwrap_or(5) as usize;
            let _ = st.eng.embedder.embed(name).await;
            let results = st.eng.search_class(name, top_k, st.eng.embedder.mem_get(name)).unwrap_or_default();
            json!({"status":"ok","results": results, "count": results.len()})
        }
        "compress_memories" => {
            if let Some(d) = args.get("date").and_then(|x| x.as_str()) {
                match compress_one(st, d).await {
                    Ok(summary) => json!({"status":"ok","date": d, "summary": summary}),
                    Err(e) => json!({"error": e}),
                }
            } else {
                let days = args.get("days_ago").and_then(|x| x.as_i64()).unwrap_or(30);
                let dates = st.eng.compress_dates_before(days).unwrap_or_default();
                let mut results = serde_json::Map::new();
                for d in dates {
                    if let Ok(s) = compress_one(st, &d).await {
                        if !s.is_empty() {
                            results.insert(d, json!(s));
                        }
                    }
                }
                json!({"status":"ok","compressed_dates": results.len(), "results": results})
            }
        }
        _ => json!({"error": format!("Tool not found: {name}")}),
    }
}

// 占位：Request 仅 mcp 内部可能用到
#[allow(dead_code)]
fn _unused(_r: Request<Body>) {}
