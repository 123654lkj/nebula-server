//! SQLite 原库 + 内存向量矩阵。不改表结构。

use crate::embed::{content_hash, l2_normalize, Embedder};
use crate::rank::{apply_authority_boost, infer_trust};
use crate::secrets::scan_secrets;
use crate::temporal::{now_ts, temporal_weight, unix_from_ymd};
use crate::types::{Hit, SearchOpts, EMBED_DIM, EMBED_MODEL};
use anyhow::{anyhow, Result};
use parking_lot::{Mutex, RwLock};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

#[derive(Default, Clone)]
pub struct AddExtra {
    pub level: Option<i64>,
    pub parent_id: Option<i64>,
    pub line_start: Option<i64>,
    pub line_end: Option<i64>,
    pub node_type: Option<String>,
    pub node_name: Option<String>,
    pub project_name: Option<String>,
    pub location: Option<String>,
    pub content_hash_override: Option<String>,
    pub skip_secret_scan: bool,
    pub modality_image: bool,
}

pub struct EmbMatrix {
    pub ids: Vec<i64>,
    pub data: Vec<f32>, // 行优先，已 L2 归一
    pub dim: usize,
    pub deleted: HashSet<i64>,
}

impl EmbMatrix {
    pub fn n(&self) -> usize {
        self.ids.len()
    }
    pub fn scores(&self, q: &[f32]) -> Vec<(usize, f32)> {
        let dim = self.dim;
        let n = self.n();
        let mut out = Vec::with_capacity(n);
        for i in 0..n {
            if self.deleted.contains(&self.ids[i]) {
                continue;
            }
            let row = &self.data[i * dim..(i + 1) * dim];
            let mut s = 0.0f32;
            let m = dim.min(q.len());
            for j in 0..m {
                s += row[j] * q[j];
            }
            out.push((i, s));
        }
        out
    }
}

pub struct Engine {
    pub db_path: PathBuf,
    pub docs_dir: PathBuf,
    pub image_dir: PathBuf,
    conn: Mutex<Connection>,
    pub emb: RwLock<EmbMatrix>,
    pub embedder: Embedder,
    pub readonly: bool,
    result_cache: Mutex<ResultCache>,
}

struct ResultCache {
    map: HashMap<String, (Value, std::time::Instant)>,
    ttl: std::time::Duration,
    cap: usize,
    hits: u64,
    misses: u64,
}
impl ResultCache {
    fn new() -> Self {
        Self {
            map: HashMap::new(),
            ttl: std::time::Duration::from_secs(300),
            cap: 64,
            hits: 0,
            misses: 0,
        }
    }
    fn get(&mut self, k: &str) -> Option<Value> {
        if let Some((v, t)) = self.map.get(k) {
            if t.elapsed() < self.ttl {
                self.hits += 1;
                return Some(v.clone());
            }
        }
        self.misses += 1;
        None
    }
    fn put(&mut self, k: String, v: Value) {
        if self.map.len() >= self.cap {
            if let Some(old) = self.map.keys().next().cloned() {
                self.map.remove(&old);
            }
        }
        self.map.insert(k, (v, std::time::Instant::now()));
    }
    fn stats(&self) -> Value {
        json!({"size": self.map.len(), "hits": self.hits, "misses": self.misses, "ttl_sec": 300})
    }
}

fn open_conn(path: &Path) -> Result<(Connection, bool)> {
    match Connection::open(path) {
        Ok(c) => Ok((c, false)),
        Err(_) => {
            let c = Connection::open_with_flags(
                path,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
            )?;
            Ok((c, true))
        }
    }
}

fn pragmas(c: &Connection) {
    let _ = c.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA busy_timeout=5000;
         PRAGMA synchronous=NORMAL;
         PRAGMA cache_size=-8192;
         PRAGMA mmap_size=33554432;
         PRAGMA temp_store=MEMORY;
         PRAGMA foreign_keys=ON;",
    );
}

impl Engine {
    pub fn open(db_path: impl AsRef<Path>) -> Result<Self> {
        let db_path = db_path.as_ref().to_path_buf();
        let (conn, readonly) = open_conn(&db_path)?;
        pragmas(&conn);
        let docs_dir = PathBuf::from(
            std::env::var("NEBULA_DOCS_DIR").unwrap_or_else(|_| "/opt/nebula/docs".into()),
        );
        let image_dir = std::env::var("NEBULA_IMAGE_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| db_path.parent().unwrap_or(Path::new(".")).join("images"));
        let embedder = Embedder::new()?;
        let mut eng = Self {
            db_path,
            docs_dir,
            image_dir,
            conn: Mutex::new(conn),
            emb: RwLock::new(EmbMatrix {
                ids: vec![],
                data: vec![],
                dim: EMBED_DIM,
                deleted: HashSet::new(),
            }),
            embedder,
            readonly,
            result_cache: Mutex::new(ResultCache::new()),
        };
        eng.reload_matrix()?;
        // 磁盘查询缓存灌入 LRU（最多 256）
        eng.warmup_qcache();
        Ok(eng)
    }

    fn warmup_qcache(&self) {
        let conn = self.conn.lock();
        let mut stmt = match conn.prepare(
            "SELECT qkey, vector, dim FROM emb_query_cache ORDER BY access_count DESC LIMIT 256",
        ) {
            Ok(s) => s,
            Err(_) => return,
        };
        let rows = stmt.query_map([], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, Vec<u8>>(1)?, r.get::<_, i64>(2)?))
        });
        if let Ok(rows) = rows {
            for row in rows.flatten() {
                let (k, blob, dim) = row;
                if dim as usize != EMBED_DIM {
                    continue;
                }
                let mut v = blob_to_f32(&blob);
                if v.len() == EMBED_DIM {
                    self.embedder.mem_put(&k, v.split_off(0));
                    let _ = v;
                    self.embedder.mem_put(&k, blob_to_f32(&blob));
                }
            }
        }
    }

    pub fn reload_matrix(&self) -> Result<usize> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, embedding, embedding_dim FROM memories WHERE ifnull(is_compressed,0)=0 AND embedding IS NOT NULL",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1)?, r.get::<_, Option<i64>>(2)?))
        })?;
        let mut ids = vec![];
        let mut data = vec![];
        let mut n = 0usize;
        for row in rows.flatten() {
            let (id, blob, dim) = row;
            let d = dim.unwrap_or(EMBED_DIM as i64) as usize;
            let mut v = blob_to_f32(&blob);
            if v.len() != d && v.len() != EMBED_DIM {
                continue;
            }
            if v.len() > EMBED_DIM {
                v.truncate(EMBED_DIM);
            }
            while v.len() < EMBED_DIM {
                v.push(0.0);
            }
            l2_normalize(&mut v);
            ids.push(id);
            data.extend_from_slice(&v);
            n += 1;
        }
        *self.emb.write() = EmbMatrix {
            ids,
            data,
            dim: EMBED_DIM,
            deleted: HashSet::new(),
        };
        tracing::info!("嵌入矩阵加载: {n} 条");
        Ok(n)
    }

    pub fn cache_get(&self, key: &str) -> Option<Value> {
        self.result_cache.lock().get(key)
    }
    pub fn cache_put(&self, key: String, v: Value) {
        self.result_cache.lock().put(key, v);
    }
    pub fn cache_stats(&self) -> Value {
        self.result_cache.lock().stats()
    }

    pub fn with_conn<T>(&self, f: impl FnOnce(&Connection) -> rusqlite::Result<T>) -> Result<T> {
        let g = self.conn.lock();
        f(&g).map_err(|e| anyhow!(e))
    }

    pub fn search(&self, query: &str, opts: SearchOpts) -> Result<Vec<Hit>> {
        if opts.use_hybrid {
            let mut fused = self.hybrid(query, &opts)?;
            if opts.rerank {
                // 同步路径不打远程 rerank；由 /ask 异步调用
            }
            fused.truncate(opts.top_k);
            return Ok(fused);
        }
        let mut q = if let Some(pre) = opts.precomputed.clone() {
            pre
        } else {
            // 同步 search 只能用缓存；未命中则空（HTTP 层先 embed）
            self.embedder
                .mem_get(query)
                .ok_or_else(|| anyhow!("no_query_vector"))?
        };
        l2_normalize(&mut q);
        let scores = {
            let mat = self.emb.read();
            mat.scores(&q)
        };
        if scores.is_empty() {
            return Ok(vec![]);
        }
        let fetch_k = (opts.top_k * 3).min(scores.len()).max(1);
        let mut top = scores;
        if fetch_k < top.len() {
            let cut = top.len() - fetch_k;
            top.select_nth_unstable_by(cut, |a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
            top = top.split_off(cut);
        }
        top.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        let mat = self.emb.read();
        let mut ids = vec![];
        let mut scs = vec![];
        for (idx, s) in top {
            ids.push(mat.ids[idx]);
            scs.push(s as f64);
        }
        drop(mat);
        let mut hits = self.hydrate_ids(&ids, &scs, &opts)?;
        for h in hits.iter_mut() {
            let td = temporal_weight(
                h.created_at,
                opts.temporal_intent.as_deref().unwrap_or("neutral"),
                opts.as_of,
                opts.enable_time_decay,
                opts.time_decay_lambda,
            );
            h.score = ((h.score * td) * 1_000_000.0).round() / 1_000_000.0;
            if opts.explain {
                h.explain = Some(json!({"vector_score": h.vector_score, "time_decay": td, "final_score": h.score}));
            }
        }
        hits.retain(|h| h.score >= opts.similarity_threshold);
        hits.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap());
        hits = apply_authority_boost(self, hits, opts.explain)?;
        hits.truncate(opts.top_k);
        Ok(hits)
    }

    pub fn bm25(&self, query: &str, opts: &SearchOpts) -> Result<Vec<Hit>> {
        let re = regex::Regex::new(r"[\w\u4e00-\u9fff]+").unwrap();
        let tokens: Vec<String> = re
            .find_iter(&query.to_lowercase())
            .map(|m| m.as_str().to_string())
            .filter(|t| !t.is_empty())
            .collect();
        if tokens.is_empty() {
            return Ok(vec![]);
        }
        let fts: String = tokens.join(" OR ");
        let top = opts.top_k * 2;
        let conn = self.conn.lock();
        let mut sql = String::from(
            "SELECT m.id, m.content, bm25(memories_fts) as score, m.created_at
             FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid
             WHERE memories_fts MATCH ? AND ifnull(m.is_compressed,0)=0",
        );
        let mut args: Vec<Value> = vec![json!(fts)];
        if let Some(cat) = &opts.category {
            sql.push_str(" AND m.category = ?");
            args.push(json!(cat));
        }
        if let Some(tid) = &opts.tenant_id {
            sql.push_str(" AND json_extract(ifnull(m.metadata,'{}'), '$.tenant_id') = ?");
            args.push(json!(tid));
        }
        sql.push_str(" ORDER BY bm25(memories_fts) LIMIT ?");
        args.push(json!(top as i64));
        let mut stmt = conn.prepare(&sql)?;
        let bind: Vec<Box<dyn rusqlite::types::ToSql>> = args
            .iter()
            .map(|v| -> Box<dyn rusqlite::types::ToSql> {
                match v {
                    Value::String(s) => Box::new(s.clone()),
                    Value::Number(n) if n.is_i64() => Box::new(n.as_i64().unwrap()),
                    _ => Box::new(v.to_string()),
                }
            })
            .collect();
        let refs: Vec<&dyn rusqlite::types::ToSql> = bind.iter().map(|b| b.as_ref()).collect();
        let rows = stmt.query_map(refs.as_slice(), |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, f64>(2)?,
                r.get::<_, Option<f64>>(3)?,
            ))
        })?;
        let collected: Vec<_> = rows.flatten().collect();
        if collected.is_empty() {
            return Ok(vec![]);
        }
        let max_abs = collected.iter().map(|r| r.2.abs()).fold(1e-9, f64::max);
        let mut out = vec![];
        for (id, content, raw, created) in collected {
            let mut score = raw.abs() / max_abs;
            let td = temporal_weight(
                created,
                opts.temporal_intent.as_deref().unwrap_or("neutral"),
                opts.as_of,
                opts.enable_time_decay,
                opts.time_decay_lambda,
            );
            score *= td;
            out.push(Hit {
                id,
                content,
                score,
                bm25_score: Some(score),
                created_at: created,
                time_decay: if opts.enable_time_decay { Some(td) } else { None },
                ..Default::default()
            });
        }
        Ok(out)
    }

    fn hybrid(&self, query: &str, opts: &SearchOpts) -> Result<Vec<Hit>> {
        let mut vo = opts.clone();
        vo.use_hybrid = false;
        vo.rerank = false;
        vo.enable_time_decay = false;
        vo.top_k = opts.top_k * 2;
        let vec_res = match self.search(query, vo) {
            Ok(v) => v,
            Err(_) => vec![],
        };
        let mut bo = opts.clone();
        bo.enable_time_decay = false;
        bo.top_k = opts.top_k * 2;
        let bm = self.bm25(query, &bo).unwrap_or_default();

        let mut v_rank: HashMap<i64, usize> = HashMap::new();
        let mut vec_map: HashMap<i64, Hit> = HashMap::new();
        let mut order: Vec<(i64, f64)> = vec_res.iter().map(|h| (h.id, h.score)).collect();
        order.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
        for (i, (id, _)) in order.iter().enumerate() {
            v_rank.insert(*id, i + 1);
        }
        for h in vec_res {
            vec_map.insert(h.id, h);
        }
        let mut b_rank: HashMap<i64, usize> = HashMap::new();
        let mut bm_map: HashMap<i64, Hit> = HashMap::new();
        let mut border: Vec<(i64, f64)> = bm.iter().map(|h| (h.id, h.bm25_score.unwrap_or(h.score))).collect();
        border.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
        for (i, (id, _)) in border.iter().enumerate() {
            b_rank.insert(*id, i + 1);
        }
        for h in bm {
            bm_map.insert(h.id, h);
        }
        let mut all: HashSet<i64> = HashSet::new();
        all.extend(v_rank.keys());
        all.extend(b_rank.keys());
        let k = 60.0;
        let mut fused = vec![];
        for id in all {
            let v_rrf = 1.0 / (k + v_rank.get(&id).copied().unwrap_or(usize::MAX / 4) as f64);
            let b_rrf = 1.0 / (k + b_rank.get(&id).copied().unwrap_or(usize::MAX / 4) as f64);
            let rrf = v_rrf + b_rrf;
            let created = vec_map
                .get(&id)
                .and_then(|h| h.created_at)
                .or_else(|| bm_map.get(&id).and_then(|h| h.created_at));
            let td = temporal_weight(
                created,
                opts.temporal_intent.as_deref().unwrap_or("neutral"),
                opts.as_of,
                opts.enable_time_decay,
                opts.time_decay_lambda,
            );
            let content = vec_map
                .get(&id)
                .map(|h| h.content.clone())
                .or_else(|| bm_map.get(&id).map(|h| h.content.clone()))
                .unwrap_or_default();
            let mut h = vec_map.get(&id).cloned().or_else(|| bm_map.get(&id).cloned()).unwrap_or(Hit {
                id,
                content: content.clone(),
                ..Default::default()
            });
            h.content = content;
            h.score = ((rrf * td) * 1_000_000.0).round() / 1_000_000.0;
            h.vector_score = Some(vec_map.get(&id).map(|x| x.score).unwrap_or(0.0));
            h.bm25_score = Some(bm_map.get(&id).and_then(|x| x.bm25_score).unwrap_or(0.0));
            h.created_at = created;
            h.time_decay = if opts.enable_time_decay { Some(td) } else { None };
            if opts.explain {
                h.explain = Some(json!({
                    "vector_score": h.vector_score,
                    "bm25_score": h.bm25_score,
                    "rrf_score": rrf,
                    "time_decay": td,
                    "final_score": h.score,
                }));
            }
            fused.push(h);
        }
        let fused = apply_authority_boost(self, fused, opts.explain)?;
        Ok(fused)
    }

    fn hydrate_ids(&self, ids: &[i64], scores: &[f64], opts: &SearchOpts) -> Result<Vec<Hit>> {
        if ids.is_empty() {
            return Ok(vec![]);
        }
        let placeholders = vec!["?"; ids.len()].join(",");
        let mut sql = format!(
            "SELECT id, content, category, importance, created_at, source_file,
                    level, node_type, node_name, project_name, location, metadata, content_preview, trust
             FROM memories WHERE id IN ({placeholders}) AND ifnull(is_compressed,0)=0"
        );
        let mut extra: Vec<Box<dyn rusqlite::types::ToSql>> = vec![];
        for id in ids {
            extra.push(Box::new(*id));
        }
        if let Some(cat) = &opts.category {
            sql.push_str(" AND category = ?");
            extra.push(Box::new(cat.clone()));
        }
        if let Some(tid) = &opts.tenant_id {
            sql.push_str(" AND json_extract(ifnull(metadata,'{}'), '$.tenant_id') = ?");
            extra.push(Box::new(tid.clone()));
        }
        if let Some(lv) = opts.level {
            sql.push_str(" AND level = ?");
            extra.push(Box::new(lv));
        }
        if let Some(nt) = &opts.node_type {
            sql.push_str(" AND node_type = ?");
            extra.push(Box::new(nt.clone()));
        }
        if let Some(nn) = &opts.node_name {
            sql.push_str(" AND node_name = ?");
            extra.push(Box::new(nn.clone()));
        }
        if let Some(sf) = &opts.source_file {
            sql.push_str(" AND source_file = ?");
            extra.push(Box::new(sf.clone()));
        }
        if let Some(pn) = &opts.project_name {
            sql.push_str(" AND project_name = ?");
            extra.push(Box::new(pn.clone()));
        }
        if let Some(loc) = &opts.location {
            sql.push_str(" AND location = ?");
            extra.push(Box::new(loc.clone()));
        }
        if let Some(df) = &opts.date_from {
            if let Some(ts) = unix_from_ymd(df) {
                sql.push_str(" AND created_at >= ?");
                extra.push(Box::new(ts));
            }
        }
        if let Some(dt) = &opts.date_to {
            if let Some(ts) = unix_from_ymd(dt) {
                sql.push_str(" AND created_at < ?");
                extra.push(Box::new(ts + 86400.0));
            }
        }
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(&sql)?;
        let refs: Vec<&dyn rusqlite::types::ToSql> = extra.iter().map(|b| b.as_ref()).collect();
        let mut db_map: HashMap<i64, Hit> = HashMap::new();
        let rows = stmt.query_map(refs.as_slice(), |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, Option<f64>>(3)?,
                r.get::<_, Option<f64>>(4)?,
                r.get::<_, Option<String>>(5)?,
                r.get::<_, Option<i64>>(6)?,
                r.get::<_, Option<String>>(7)?,
                r.get::<_, Option<String>>(8)?,
                r.get::<_, Option<String>>(9)?,
                r.get::<_, Option<String>>(10)?,
                r.get::<_, Option<String>>(11)?,
                r.get::<_, Option<String>>(12)?,
                r.get::<_, Option<String>>(13)?,
            ))
        })?;
        for row in rows.flatten() {
            let (id, content, cat, imp, created, src, lvl, ntype, nname, proj, loc, meta_json, preview, trust) =
                row;
            let mut h = Hit {
                id,
                content,
                score: 0.0,
                category: cat,
                importance: imp,
                created_at: created,
                source_file: src,
                level: lvl,
                node_type: ntype,
                node_name: nname,
                project_name: proj,
                location: loc,
                content_preview: preview,
                trust,
                ..Default::default()
            };
            if let Some(mj) = meta_json {
                if let Ok(md) = serde_json::from_str::<Value>(&mj) {
                    if let Value::Object(m) = &md {
                        if let Some(a) = m.get("abstract").and_then(|x| x.as_str()) {
                            h.abstract_text = Some(a.to_string());
                            h.abstract_out = Some(a.to_string());
                        }
                        if let Some(l) = m.get("memory_layer").and_then(|x| x.as_str()) {
                            h.memory_layer = Some(l.to_string());
                        }
                        if m.get("modality").and_then(|x| x.as_str()) == Some("image") {
                            h.modality = Some("image".into());
                            h.image_url = Some(format!("/memory/image/{id}"));
                            if let Some(p) = m.get("image_path").and_then(|x| x.as_str()) {
                                h.image_path = Some(p.into());
                            }
                        }
                    }
                    h.metadata = Some(md);
                }
            }
            db_map.insert(id, h);
        }
        let mut out = vec![];
        for (id, sc) in ids.iter().zip(scores.iter()) {
            if let Some(mut h) = db_map.remove(id) {
                h.score = *sc;
                h.vector_score = Some(*sc);
                out.push(h);
            }
        }
        Ok(out)
    }

    pub fn related(&self, id: i64, limit: i64) -> Result<Value> {
        self.with_conn(|c| {
            let mut stmt = c.prepare(
                "SELECT l.dst_id, l.rel, l.weight, m.content, m.trust, m.importance, m.source_file, m.category
                 FROM memory_links l JOIN memories m ON m.id = l.dst_id
                 WHERE l.src_id = ? AND ifnull(m.is_compressed,0)=0
                 ORDER BY l.weight DESC, m.importance DESC LIMIT ?",
            )?;
            let rows = stmt.query_map(params![id, limit], |r| {
                Ok(json!({
                    "id": r.get::<_, i64>(0)?,
                    "rel": r.get::<_, String>(1)?,
                    "weight": r.get::<_, f64>(2)?,
                    "content": r.get::<_, Option<String>>(3)?.unwrap_or_default().chars().take(280).collect::<String>(),
                    "trust": r.get::<_, Option<String>>(4)?,
                    "importance": r.get::<_, Option<f64>>(5)?,
                    "source_file": r.get::<_, Option<String>>(6)?,
                    "category": r.get::<_, Option<String>>(7)?,
                }))
            })?;
            Ok(rows.flatten().collect::<Vec<_>>())
        })
        .map(|v| json!(v))
    }

    pub fn stats(&self) -> Result<Value> {
        self.with_conn(|c| {
            let total: i64 = c.query_row("SELECT COUNT(*) FROM memories", [], |r| r.get(0))?;
            let mut stmt = c.prepare("SELECT DISTINCT category FROM memories WHERE category IS NOT NULL")?;
            let cats: Vec<String> = stmt.query_map([], |r| r.get(0))?.flatten().collect();
            let db_size = std::fs::metadata(&self.db_path).map(|m| m.len()).unwrap_or(0);
            let db_s = if db_size > 1024 * 1024 {
                format!("{:.1}MB", db_size as f64 / 1024.0 / 1024.0)
            } else if db_size > 1024 {
                format!("{}KB", db_size / 1024)
            } else {
                format!("{db_size}B")
            };
            Ok(json!({
                "total_memories": total,
                "categories": cats,
                "db_size": db_s,
                "embedding_cache": self.embedder.stats(),
                "engine": "rust",
                "readonly": self.readonly,
                "emb_rows": self.emb.read().n(),
            }))
        })
    }

    pub fn health_report(&self) -> Result<Value> {
        self.with_conn(|c| {
            let mut trust = serde_json::Map::new();
            let mut stmt = c.prepare("SELECT trust, COUNT(*) FROM memories GROUP BY trust")?;
            for row in stmt.query_map([], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, i64>(1)?)))?.flatten() {
                trust.insert(row.0.unwrap_or_default(), json!(row.1));
            }
            let mut links = serde_json::Map::new();
            if let Ok(mut st) = c.prepare("SELECT rel, COUNT(*) FROM memory_links GROUP BY rel") {
                for row in st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))?.flatten() {
                    links.insert(row.0, json!(row.1));
                }
            }
            let total: i64 = c.query_row("SELECT COUNT(*) FROM memories WHERE ifnull(is_compressed,0)=0", [], |r| r.get(0))?;
            let vault_n: i64 = c.query_row(
                "SELECT COUNT(*) FROM memories WHERE source_file LIKE 'vault:%' AND ifnull(is_compressed,0)=0",
                [],
                |r| r.get(0),
            )?;
            let mut layers = json!({"semantic":0,"episodic":0,"procedural":0,"unknown":0});
            if let Ok(mut st) = c.prepare(
                "SELECT COALESCE(json_extract(metadata,'$.memory_layer'),'unknown'), COUNT(*) FROM memories WHERE ifnull(is_compressed,0)=0 GROUP BY 1",
            ) {
                for row in st.query_map([], |r| Ok((r.get::<_, Option<String>>(0)?, r.get::<_, i64>(1)?)))?.flatten() {
                    let k = row.0.unwrap_or_else(|| "unknown".into()).to_lowercase();
                    layers[k] = json!(row.1);
                }
            }
            let extract_n: i64 = c
                .query_row(
                    "SELECT COUNT(*) FROM memories WHERE ifnull(is_compressed,0)=0 AND source_file='session-extract'",
                    [],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            let abstract_n: i64 = c
                .query_row(
                    "SELECT COUNT(*) FROM memories WHERE ifnull(is_compressed,0)=0 AND json_extract(metadata,'$.abstract') IS NOT NULL AND length(json_extract(metadata,'$.abstract'))>0",
                    [],
                    |r| r.get(0),
                )
                .unwrap_or(0);
            let maturity = maturity_score(&trust, &links, total);
            Ok(json!({
                "version": crate::types::RELEASE,
                "total_active": total,
                "vault_chunks": vault_n,
                "trust": trust,
                "links": links,
                "memory_layers": layers,
                "session_extract_count": extract_n,
                "abstract_count": abstract_n,
                "maturity": maturity,
                "cached": false,
                "engine": "rust",
                "emb_rows": self.emb.read().n(),
                "rss_hint": "nebula-engine",
            }))
        })
    }

    pub fn add(
        &self,
        content: &str,
        source: &str,
        category: Option<&str>,
        tags: Option<&[String]>,
        importance: f64,
        metadata: Value,
        semantic_dedup: bool,
        created_at: Option<f64>,
        vec: Vec<f32>,
    ) -> Result<Value> {
        self.add_ex(
            content, source, category, tags, importance, metadata, semantic_dedup, created_at, vec,
            AddExtra::default(),
        )
    }

    pub fn add_ex(
        &self,
        content: &str,
        source: &str,
        category: Option<&str>,
        tags: Option<&[String]>,
        importance: f64,
        metadata: Value,
        semantic_dedup: bool,
        created_at: Option<f64>,
        vec: Vec<f32>,
        extra: AddExtra,
    ) -> Result<Value> {
        if self.readonly {
            return Err(anyhow!("database readonly"));
        }
        let hits = if extra.skip_secret_scan {
            vec![]
        } else {
            scan_secrets(content)
        };
        if !hits.is_empty() {
            return Err(anyhow!(
                "SECRET_REJECTED: 检测到疑似密钥明文({}).请 POST /secrets/store 写入 Vaultwarden，星枢只存 vaultwarden 指针。",
                hits.join(",")
            ));
        }
        let h = extra
            .content_hash_override
            .clone()
            .unwrap_or_else(|| content_hash(content));
        {
            let conn = self.conn.lock();
            if let Some(id) = conn
                .query_row(
                    "SELECT id FROM memories WHERE content_hash=? AND ifnull(is_compressed,0)=0",
                    params![h],
                    |r| r.get::<_, i64>(0),
                )
                .optional()?
            {
                return Ok(json!({"id": id, "is_duplicate": true, "duplicate_of": id, "similarity_score": 1.0}));
            }
        }
        let mut similarity_score = 0.0;
        if semantic_dedup && !content.trim().is_empty() {
            if let Ok(sim) = self.search(
                content,
                SearchOpts {
                    top_k: 1,
                    category: category.map(|s| s.to_string()),
                    use_hybrid: false,
                    precomputed: Some(vec.clone()),
                    enable_time_decay: false,
                    ..Default::default()
                },
            ) {
                if let Some(top) = sim.first() {
                    if top.score >= 0.95 {
                        return Ok(json!({
                            "id": top.id,
                            "is_duplicate": true,
                            "duplicate_of": top.id,
                            "similarity_score": top.score
                        }));
                    }
                    similarity_score = top.score;
                }
            }
        }
        let cat = category
            .map(|s| s.to_string())
            .unwrap_or_else(|| rule_classify(content));
        let mut meta = metadata;
        if !meta.is_object() {
            meta = json!({});
        }
        let obj = meta.as_object_mut().unwrap();
        let preview = obj
            .get("abstract")
            .and_then(|x| x.as_str())
            .map(|s| s.chars().take(200).collect::<String>())
            .unwrap_or_else(|| content.chars().take(200).collect());
        let trust = infer_trust(content, source, importance, obj.get("trust").and_then(|x| x.as_str()));
        obj.insert("trust".into(), json!(trust));
        let meta_json = meta.to_string();
        let now = now_ts();
        let created = created_at.unwrap_or(now);
        let blob = f32_to_blob(&vec);
        let dim = vec.len() as i64;
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO memories (
                source_file, section_title, content_hash, content, content_preview,
                embedding, embedding_dim, embedding_model, category, importance,
                created_at, updated_at, metadata, trust,
                level, parent_id, line_start, line_end, node_type, node_name, project_name, location
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params![
                source, h, content, preview, blob, dim, EMBED_MODEL, cat, importance, created, now, meta_json, trust,
                extra.level, extra.parent_id, extra.line_start, extra.line_end,
                extra.node_type, extra.node_name, extra.project_name, extra.location
            ],
        )?;
        let mid = conn.last_insert_rowid();
        if let Some(tags) = tags {
            for t in tags {
                let name = t.trim();
                if name.is_empty() {
                    continue;
                }
                conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", params![name])?;
                let tid: i64 = conn.query_row("SELECT id FROM tags WHERE name=? COLLATE NOCASE", params![name], |r| r.get(0))?;
                let _ = conn.execute(
                    "INSERT OR IGNORE INTO memory_tags(memory_id, tag_id) VALUES(?,?)",
                    params![mid, tid],
                );
            }
        }
        drop(conn);
        {
            let mut mat = self.emb.write();
            let mut v = vec.clone();
            if v.len() != EMBED_DIM {
                v.resize(EMBED_DIM, 0.0);
            }
            l2_normalize(&mut v);
            mat.ids.push(mid);
            mat.data.extend_from_slice(&v);
        }
        let mut out = json!({"id": mid, "is_duplicate": false, "duplicate_of": null, "similarity_score": similarity_score, "trust": trust});
        if extra.modality_image {
            out["modality"] = json!("image");
            out["image_url"] = json!(format!("/memory/image/{mid}"));
        }
        Ok(out)
    }

    pub fn delete(&self, id: i64) -> Result<bool> {
        if self.readonly {
            return Err(anyhow!("database readonly"));
        }
        let n = {
            let conn = self.conn.lock();
            conn.execute("DELETE FROM memories WHERE id=?", params![id])?
        };
        if n > 0 {
            self.emb.write().deleted.insert(id);
            Ok(true)
        } else {
            Ok(false)
        }
    }

    pub fn update_fields(
        &self,
        id: i64,
        content: Option<&str>,
        category: Option<&str>,
        importance: Option<f64>,
        tags: Option<&[String]>,
        new_vec: Option<Vec<f32>>,
    ) -> Result<Value> {
        if self.readonly {
            return Err(anyhow!("database readonly"));
        }
        let conn = self.conn.lock();
        let row = conn
            .query_row(
                "SELECT content, category, importance FROM memories WHERE id=?",
                params![id],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, Option<String>>(1)?,
                        r.get::<_, Option<f64>>(2)?,
                    ))
                },
            )
            .optional()?
            .ok_or_else(|| anyhow!("not_found"))?;
        let new_content = content.unwrap_or(&row.0);
        let new_cat = category.map(|s| s.to_string()).or(row.1);
        let new_imp = importance.or(row.2);
        let now = now_ts();
        let content_changed = content.map(|c| c != row.0).unwrap_or(false);
        if content_changed {
            let h = content_hash(new_content);
            conn.execute(
                "UPDATE memories SET content=?, category=?, importance=?, content_hash=?, updated_at=? WHERE id=?",
                params![new_content, new_cat, new_imp, h, now, id],
            )?;
        } else {
            conn.execute(
                "UPDATE memories SET category=?, importance=?, updated_at=? WHERE id=?",
                params![new_cat, new_imp, now, id],
            )?;
        }
        if let Some(tags) = tags {
            conn.execute("DELETE FROM memory_tags WHERE memory_id=?", params![id])?;
            for t in tags {
                let name = t.trim();
                if name.is_empty() {
                    continue;
                }
                conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", params![name])?;
                let tid: i64 = conn.query_row("SELECT id FROM tags WHERE name=? COLLATE NOCASE", params![name], |r| r.get(0))?;
                let _ = conn.execute("INSERT OR IGNORE INTO memory_tags(memory_id, tag_id) VALUES(?,?)", params![id, tid]);
            }
        }
        if let Some(vec) = new_vec {
            let blob = f32_to_blob(&vec);
            conn.execute(
                "UPDATE memories SET embedding=?, embedding_dim=?, updated_at=? WHERE id=?",
                params![blob, vec.len() as i64, now, id],
            )?;
        }
        drop(conn);
        if content_changed {
            let _ = self.reload_matrix();
        }
        Ok(json!({"status":"ok","id": id}))
    }

    pub fn supersede(&self, old_id: i64, new_id: Option<i64>, note: &str) -> Result<Value> {
        if self.readonly {
            return Err(anyhow!("database readonly"));
        }
        let conn = self.conn.lock();
        let row = conn
            .query_row(
                "SELECT id, content, importance FROM memories WHERE id=?",
                params![old_id],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<f64>>(2)?)),
            )
            .optional()?;
        let Some((_, mut content, imp)) = row else {
            return Ok(json!({"ok": false, "error": "not_found"}));
        };
        if !content.starts_with("【已过时") && !content.chars().take(80).collect::<String>().contains("SUPERSEDED") {
            let prefix = format!(
                "【已过时 SUPERSEDED {}】",
                chrono::Local::now().format("%Y-%m-%d")
            );
            let extra = if note.is_empty() {
                String::new()
            } else {
                format!("{note}\n")
            };
            content = format!("{prefix}{extra}{content}");
        }
        let new_imp = imp.unwrap_or(0.5).min(0.35);
        conn.execute(
            "UPDATE memories SET trust=?, importance=?, content=?, updated_at=? WHERE id=?",
            params!["superseded", new_imp, content, now_ts(), old_id],
        )?;
        if let Some(nid) = new_id {
            let _ = conn.execute(
                "INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at) VALUES(?,?,?,?,?)",
                params![old_id, nid, "superseded_by", 1.0, now_ts()],
            );
            let _ = conn.execute(
                "INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at) VALUES(?,?,?,?,?)",
                params![nid, old_id, "supersedes", 1.0, now_ts()],
            );
        }
        Ok(json!({"ok": true, "old_id": old_id, "new_id": new_id}))
    }

    pub fn tags_cloud(&self, limit: i64) -> Result<Value> {
        self.with_conn(|c| {
            let mut stmt = c.prepare("SELECT name, usage_count FROM tags ORDER BY usage_count DESC LIMIT ?")?;
            let rows: Vec<Value> = stmt
                .query_map(params![limit], |r| {
                    Ok(json!({"name": r.get::<_, String>(0)?, "count": r.get::<_, Option<i64>>(1)?}))
                })?
                .flatten()
                .collect();
            Ok(json!(rows))
        })
    }

    pub fn help_text(&self, level: &str) -> (String, String) {
        let (lvl, name) = match level {
            "full" | "long" | "all" | "3" => ("full", "USAGE.md"),
            "short" | "quick" | "2" | "s" => ("short", "USAGE.short.md"),
            _ => ("mini", "USAGE.mini.txt"),
        };
        let p = self.docs_dir.join(name);
        let text = std::fs::read_to_string(&p).unwrap_or_else(|e| format!("help unavailable: {e}"));
        (lvl.into(), text)
    }

    pub fn fetch_one(&self, id: i64) -> Result<Option<(String, Option<String>, Option<String>)>> {
        self.with_conn(|c| {
            c.query_row(
                "SELECT content, source_file, category FROM memories WHERE id=?",
                params![id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
        })
    }

    pub fn memory_meta(&self, id: i64) -> Result<Option<Value>> {
        self.with_conn(|c| {
            c.query_row("SELECT metadata FROM memories WHERE id=?", params![id], |r| r.get::<_, Option<String>>(0))
                .optional()
        })
        .map(|o| {
            o.flatten()
                .and_then(|s| serde_json::from_str(&s).ok())
        })
    }

    pub fn lifecycle(&self, demote_days: i64, demote_max: i64) -> Result<Value> {
        if self.readonly {
            return Err(anyhow!("database readonly"));
        }
        let cutoff = now_ts() - demote_days as f64 * 86400.0;
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT id, importance, content FROM memories
             WHERE ifnull(is_compressed,0)=0
               AND (trust='synthesis' OR trust IS NULL OR trust='')
               AND (source_file IS NULL OR (source_file NOT LIKE 'vault:%' AND source_file NOT IN ('grok','manual')))
               AND importance > 0.35 AND created_at < ?
             ORDER BY importance DESC LIMIT ?",
        )?;
        let rows: Vec<(i64, f64, String)> = stmt
            .query_map(params![cutoff, demote_max], |r| {
                Ok((r.get(0)?, r.get::<_, Option<f64>>(1)?.unwrap_or(0.5), r.get::<_, String>(2)?))
            })?
            .flatten()
            .collect();
        drop(stmt);
        let mut demoted = 0i64;
        let now = now_ts();
        for (mid, imp, content) in rows {
            let head: String = content.chars().take(200).collect();
            if head.contains("现行权威") || head.contains("GATEWAY_LOCK") {
                continue;
            }
            let new_imp = (imp * 0.55).max(0.25);
            conn.execute(
                "UPDATE memories SET importance=?, updated_at=? WHERE id=?",
                params![new_imp, now, mid],
            )?;
            demoted += 1;
        }
        Ok(json!({"demoted": demoted, "demote_days": demote_days}))
    }

    pub fn batch_recategorize(&self, category_filter: Option<&str>, limit: i64, dry_run: bool) -> Result<Value> {
        let conn = self.conn.lock();
        let mut rows: Vec<(i64, String, Option<String>)> = vec![];
        if let Some(cf) = category_filter {
            let mut st = conn.prepare(
                "SELECT id, content, category FROM memories WHERE category=? AND ifnull(is_compressed,0)=0 ORDER BY id LIMIT ?",
            )?;
            rows = st
                .query_map(params![cf, limit], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
                .flatten()
                .collect();
        } else {
            let mut st = conn.prepare(
                "SELECT id, content, category FROM memories WHERE ifnull(is_compressed,0)=0 ORDER BY id LIMIT ?",
            )?;
            rows = st
                .query_map(params![limit], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
                .flatten()
                .collect();
        }
        let mut changed = 0i64;
        let mut unchanged = 0i64;
        for (id, content, old) in &rows {
            let new = rule_classify(content);
            if Some(new.as_str()) != old.as_deref() {
                if !dry_run {
                    conn.execute(
                        "UPDATE memories SET category=?, updated_at=? WHERE id=?",
                        params![new, now_ts(), id],
                    )?;
                }
                changed += 1;
            } else {
                unchanged += 1;
            }
        }
        Ok(json!({"total": rows.len(), "changed": changed, "unchanged": unchanged, "errors": 0}))
    }

    pub fn search_function(&self, func_name: &str, top_k: usize, pre: Option<Vec<f32>>) -> Result<Vec<Hit>> {
        self.search(
            func_name,
            SearchOpts {
                top_k,
                level: Some(1),
                node_type: Some("function".into()),
                node_name: Some(func_name.into()),
                precomputed: pre,
                ..Default::default()
            },
        )
    }

    pub fn search_class(&self, class_name: &str, top_k: usize, pre: Option<Vec<f32>>) -> Result<Vec<Hit>> {
        self.search(
            class_name,
            SearchOpts {
                top_k,
                level: Some(1),
                node_type: Some("class".into()),
                node_name: Some(class_name.into()),
                precomputed: pre,
                ..Default::default()
            },
        )
    }

    /// 幂等：不改已有数据含义，只补列/表/索引。
    pub fn migrate_schema(&self) -> Result<Value> {
        let conn = self.conn.lock();
        let mut done = vec![];
        let mut stmt = conn.prepare("PRAGMA table_info(memories)")?;
        let cols: HashSet<String> = stmt
            .query_map([], |r| r.get::<_, String>(1))?
            .flatten()
            .collect();
        if !cols.contains("trust") {
            conn.execute("ALTER TABLE memories ADD COLUMN trust TEXT DEFAULT 'synthesis'", [])?;
            done.push("add_trust_column");
        }
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src_id INTEGER NOT NULL,
                dst_id INTEGER NOT NULL,
                rel TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                created_at REAL,
                UNIQUE(src_id, dst_id, rel)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_links_src ON memory_links(src_id);
            CREATE INDEX IF NOT EXISTS idx_memory_links_dst ON memory_links(dst_id);
            CREATE INDEX IF NOT EXISTS idx_memories_trust ON memories(trust);
            CREATE TABLE IF NOT EXISTS compression_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_str TEXT,
                summary TEXT,
                source_count INTEGER,
                compressed_at REAL
            );",
        )?;
        done.push("memory_links_ok");
        Ok(json!({"migrated": done}))
    }

    pub fn backfill_trust(&self, limit: i64) -> Result<i64> {
        let conn = self.conn.lock();
        let sql = if limit > 0 {
            format!("SELECT id, content, source_file, importance, trust FROM memories WHERE ifnull(is_compressed,0)=0 LIMIT {limit}")
        } else {
            "SELECT id, content, source_file, importance, trust FROM memories WHERE ifnull(is_compressed,0)=0".into()
        };
        let mut stmt = conn.prepare(&sql)?;
        let rows: Vec<(i64, String, Option<String>, Option<f64>, Option<String>)> = stmt
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?
            .flatten()
            .collect();
        drop(stmt);
        let mut n = 0i64;
        for (id, content, src, imp, old) in rows {
            let t = infer_trust(&content, src.as_deref().unwrap_or(""), imp.unwrap_or(0.5), None);
            if old.as_deref() != Some(t.as_str()) {
                conn.execute("UPDATE memories SET trust=? WHERE id=?", params![t, id])?;
                n += 1;
            }
        }
        Ok(n)
    }

    pub fn rebuild_links(&self) -> Result<Value> {
        let conn = self.conn.lock();
        conn.execute("DELETE FROM memory_links WHERE rel IN ('same_source','co_source_prefix')", [])?;
        let mut stmt = conn.prepare(
            "SELECT id, source_file FROM memories WHERE ifnull(is_compressed,0)=0 AND source_file IS NOT NULL AND source_file != '' ORDER BY source_file, id",
        )?;
        let rows: Vec<(i64, String)> = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.flatten().collect();
        drop(stmt);
        let mut by_src: HashMap<String, Vec<i64>> = HashMap::new();
        for (id, src) in rows {
            by_src.entry(src).or_default().push(id);
        }
        let now = now_ts();
        let mut same = 0i64;
        for ids in by_src.values() {
            let ids: Vec<i64> = ids.iter().copied().take(40).collect();
            if ids.len() < 2 {
                continue;
            }
            for i in 0..ids.len() - 1 {
                let (a, b) = (ids[i], ids[i + 1]);
                conn.execute(
                    "INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at) VALUES(?,?,?,?,?)",
                    params![a, b, "same_source", 1.0, now],
                )?;
                conn.execute(
                    "INSERT OR IGNORE INTO memory_links(src_id,dst_id,rel,weight,created_at) VALUES(?,?,?,?,?)",
                    params![b, a, "same_source", 1.0, now],
                )?;
                same += 2;
            }
        }
        let total: i64 = conn.query_row("SELECT COUNT(*) FROM memory_links", [], |r| r.get(0))?;
        Ok(json!({"same_source_writes": same, "total_links": total}))
    }

    pub fn compress_date_rows(&self, date_str: &str) -> Result<(Vec<(i64, String, Option<String>)>, bool)> {
        let conn = self.conn.lock();
        let exists: Option<i64> = conn
            .query_row("SELECT id FROM compression_log WHERE date_str=?", params![date_str], |r| r.get(0))
            .optional()?;
        if exists.is_some() {
            return Ok((vec![], true));
        }
        let ts_start = unix_from_ymd(date_str).ok_or_else(|| anyhow!("日期格式错误"))?;
        let mut stmt = conn.prepare(
            "SELECT id, content, category FROM memories WHERE created_at>=? AND created_at<? AND ifnull(is_compressed,0)=0 ORDER BY created_at",
        )?;
        let rows: Vec<(i64, String, Option<String>)> = stmt
            .query_map(params![ts_start, ts_start + 86400.0], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
            .flatten()
            .collect();
        Ok((rows, false))
    }

    pub fn compress_date_commit(&self, date_str: &str, summary: &str, ids: &[i64]) -> Result<()> {
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO compression_log (date_str, summary, source_count, compressed_at) VALUES (?,?,?,?)",
            params![date_str, summary, ids.len() as i64, now_ts()],
        )?;
        if !ids.is_empty() {
            let ph = vec!["?"; ids.len()].join(",");
            let sql = format!("UPDATE memories SET is_compressed=1 WHERE id IN ({ph})");
            let bind: Vec<&dyn rusqlite::types::ToSql> = ids.iter().map(|id| id as &dyn rusqlite::types::ToSql).collect();
            conn.execute(&sql, bind.as_slice())?;
        }
        Ok(())
    }

    pub fn compress_dates_before(&self, days_ago: i64) -> Result<Vec<String>> {
        let cutoff = now_ts() - days_ago as f64 * 86400.0;
        self.with_conn(|c| {
            let mut st = c.prepare(
                "SELECT DISTINCT date(created_at, 'unixepoch', 'localtime') as d FROM memories WHERE created_at < ? AND ifnull(is_compressed,0)=0 ORDER BY d",
            )?;
            let rows: Vec<Option<String>> = st.query_map(params![cutoff], |r| r.get(0))?.flatten().collect();
            Ok(rows.into_iter().flatten().collect())
        })
    }

    pub fn ingest_split(text: &str, chunk_size: usize, overlap: usize) -> Vec<String> {
        let mut sentences = vec![];
        let mut cur = String::new();
        for ch in text.chars() {
            cur.push(ch);
            if "。！？.!；;\n".contains(ch) {
                let s = cur.trim().to_string();
                if !s.is_empty() {
                    sentences.push(s);
                }
                cur.clear();
            }
        }
        if !cur.trim().is_empty() {
            sentences.push(cur.trim().to_string());
        }
        let mut chunks = vec![];
        let mut current: Vec<String> = vec![];
        let mut current_len = 0usize;
        let keep = if overlap > 0 { overlap / 10 } else { 0 };
        for sentence in sentences {
            if current_len + sentence.chars().count() > chunk_size && !current.is_empty() {
                chunks.push(current.join(""));
                if keep > 0 && current.len() > keep {
                    current = current.split_off(current.len() - keep);
                } else if keep == 0 {
                    current.clear();
                }
                current_len = current.iter().map(|s| s.chars().count()).sum();
            }
            current_len += sentence.chars().count();
            current.push(sentence);
        }
        if !current.is_empty() {
            chunks.push(current.join(""));
        }
        chunks
    }
}

fn blob_to_f32(blob: &[u8]) -> Vec<f32> {
    blob.chunks_exact(4)
        .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect()
}
fn f32_to_blob(v: &[f32]) -> Vec<u8> {
    let mut b = Vec::with_capacity(v.len() * 4);
    for x in v {
        b.extend_from_slice(&x.to_le_bytes());
    }
    b
}

fn rule_classify(text: &str) -> String {
    let t = text.to_lowercase();
    let kws: &[(&str, &[&str])] = &[
        ("code", &["def ", "class ", "import ", "function ", "return ", "async ", "git ", "commit", "变量", "函数", "代码"]),
        ("network", &["ip ", "tcp", "udp", "proxy", "代理", "透明代理", "iptables", "nftables", "dns", "vpn", "hy2", "hysteria", "xray", "sing-box"]),
        ("hardware", &["树莓派", "gpu", "cpu", "内存", "usb", "硬件"]),
        ("homelab", &["docker", "容器", "compose", "虎虎", "nas"]),
        ("todo", &["todo", "计划", "要做", "下一步"]),
        ("note", &["学了", "学习", "笔记", "心得", "记录", "总结"]),
        ("config", &["配置", "设置", "config", ".json", ".yaml", ".toml", "环境变量"]),
        ("debug", &["bug", "错误", "报错", "fix", "修复", "排查", "问题"]),
        ("ai", &["模型", "embedding", "向量", "llm", "gpt", "minimax", "千问", "rerank"]),
    ];
    let mut best = "general";
    let mut best_s = 0i32;
    for (cat, k) in kws {
        let s = k.iter().filter(|w| t.contains(&w.to_lowercase())).count() as i32;
        if s > best_s {
            best_s = s;
            best = cat;
        }
    }
    best.into()
}

fn maturity_score(trust: &serde_json::Map<String, Value>, links: &serde_json::Map<String, Value>, total: i64) -> Value {
    let canon = trust.get("canon").and_then(|x| x.as_i64()).unwrap_or(0);
    let synth = trust.get("synthesis").and_then(|x| x.as_i64()).unwrap_or(0);
    let link_n: i64 = links.values().filter_map(|v| v.as_i64()).sum();
    let mut score = 35;
    if canon >= 100 {
        score += 12;
    }
    if link_n >= 2000 {
        score += 12;
    }
    if total > 0 && synth < (total as f64 * 0.55) as i64 {
        score += 10;
    } else if total > 0 && synth < (total as f64 * 0.7) as i64 {
        score += 5;
    }
    if trust.get("superseded").and_then(|x| x.as_i64()).unwrap_or(0) >= 5 {
        score += 4;
    }
    score += 10;
    let mut reg = json!({});
    if let Ok(txt) = std::fs::read_to_string("/home/huhu/.local/state/nebula-regression/latest.json") {
        if let Ok(v) = serde_json::from_str::<Value>(&txt) {
            let pr = v.get("pass_rate").and_then(|x| x.as_f64()).unwrap_or(0.0);
            score += (pr * 25.0) as i64;
            reg = v;
        }
    }
    // secrets 解锁在 http 层补
    score = score.min(100);
    let pr = reg.get("pass_rate").and_then(|x| x.as_f64()).unwrap_or(0.0);
    let level = if score >= 92 && pr >= 0.95 {
        "true_nb"
    } else if score >= 85 {
        "ultimate"
    } else if score >= 70 {
        "advanced"
    } else {
        "intermediate"
    };
    json!({
        "score": score,
        "level": level,
        "regression_pass_rate": reg.get("pass_rate"),
        "regression_passed": reg.get("passed"),
        "regression_total": reg.get("total"),
    })
}
