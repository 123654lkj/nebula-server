//! 星枢 Rust 常驻引擎。默认侧路 :26672，现网库只读打开。
//! 配置：CLI > 环境变量 > 配置文件(nebula.json) > 默认，见 --help。

use nebula_engine::engine::Engine;
use nebula_engine::http::{router, AppState};
use std::net::SocketAddr;
use std::sync::Arc;
use tracing_subscriber::EnvFilter;

fn main() {
    nebula_engine::config::init();
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("nebula_engine=info".parse().unwrap()))
        .with_target(false)
        .init();

    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .max_blocking_threads(8)
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

    let eng = Arc::new(eng);

    // 内置 vault 同步定时器（秒，0=关闭；替代 systemd timer + Python 脚本）
    let interval: u64 = std::env::var("NEBULA_VAULT_SYNC_INTERVAL")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    if interval > 0 && !eng.readonly {
        let e2 = eng.clone();
        tokio::spawn(async move {
            tracing::info!("vault 同步定时器启动: 每 {interval}s，根目录 {}", nebula_engine::vault::vault_root().display());
            loop {
                tokio::time::sleep(std::time::Duration::from_secs(interval)).await;
                match nebula_engine::vault::sync(&e2, nebula_engine::vault::SyncOpts::default()).await {
                    Ok(v) => tracing::info!("vault 定时同步: {}", v["stats"]),
                    Err(e) => tracing::warn!("vault 定时同步失败: {e}"),
                }
            }
        });
    } else if interval > 0 {
        tracing::warn!("库只读，vault 同步定时器未启动");
    }

    let app = router(AppState { eng });
    let addr: SocketAddr = format!("{bind}:{port}").parse().expect("addr");
    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind");
    tracing::info!("listening http://{addr}  docs=/help");
    axum::serve(listener, app).await.expect("serve");
}
