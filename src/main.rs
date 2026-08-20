//! 星枢 Rust 常驻引擎。默认侧路 :26672，现网库只读打开。

use nebula_engine::engine::Engine;
use nebula_engine::http::{router, AppState};
use std::net::SocketAddr;
use std::sync::Arc;
use tracing_subscriber::EnvFilter;

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("nebula_engine=info".parse().unwrap()))
        .with_target(false)
        .init();

    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(4)
        .max_blocking_threads(16)
        .enable_all()
        .build()
        .expect("tokio");
    rt.block_on(async_main());
}

async fn async_main() {
    let db = std::env::var("NEBULA_DB_PATH").unwrap_or_else(|_| "/opt/nebula/data/memory_vectors.db".into());
    let bind = std::env::var("NEBULA_BIND").unwrap_or_else(|_| "0.0.0.0".into());
    let port: u16 = std::env::var("NEBULA_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(26672);

    tracing::info!("打开现网库 {db}");
    let eng = match Engine::open(&db) {
        Ok(e) => e,
        Err(e) => {
            tracing::error!("打开数据库失败: {e}");
            std::process::exit(1);
        }
    };
    tracing::info!(
        "星枢 {}  engine=rust  bind={bind}:{port}  db={}  emb_rows={}  readonly={}",
        nebula_engine::RELEASE,
        eng.db_path.display(),
        eng.emb.read().n(),
        eng.readonly
    );

    let app = router(AppState { eng: Arc::new(eng) });
    let addr: SocketAddr = format!("{bind}:{port}").parse().expect("addr");
    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind");
    tracing::info!("listening http://{addr}  docs=/help");
    axum::serve(listener, app).await.expect("serve");
}
