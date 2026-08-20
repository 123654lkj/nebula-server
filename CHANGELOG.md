# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [5.2.0] - 2026-08-20

### Added

- **配置系统**：CLI（`--db`/`--port`/`--vault-root`/`--set KEY=VALUE`/`--print-config` 等，见 `--help`）> 环境变量 > JSON 配置文件（`nebula.json`，模板 `nebula.example.json`）> 默认值
- **原生 Obsidian 同步**（Rust 移植 `vault_to_nebula_sync.py`）：`POST /vault/sync`（force/dry_run/only/prune）、`GET /vault/status`、内置定时器 `NEBULA_VAULT_SYNC_INTERVAL`（可替代 systemd timer + Python）；state 文件与 Python 版兼容，不会全量重灌
- vault 同步全量可配置：根目录、白名单 glob、排除目录/文件名、体积帽、分块大小、分类/重要度规则（`NEBULA_VAULT_RULES` JSON）
- `GET /health` `/stats` `/v5/health` 报告 `rss_bytes` / `emb_bytes`
- `GET /memory/dupes` 列出重复 `content_hash`
- `POST /memory/gc` compact 矩阵并清空结果缓存
- `POST /memory/infer-layer` 按来源补 `memory_layer`（vault→semantic，会话→episodic，skill/code→procedural）
- `Engine::delete_by_source`：按来源批量删除并懒清理矩阵

### Changed

- 内存向量矩阵由 f32 改为 i16 量化（L2 后再量化），常驻约减半
- 检索只保留 top-k 点积，不再为全库分配分数数组
- SQLite `mmap_size` 32MB→16MB，`cache_size` 8MB→4MB，`temp_store=FILE`
- 启动时查询向量缓存预热 256→64；tokio worker 4→2
- systemd 增加 `MALLOC_ARENA_MAX=2`；删除累计 50 条后自动 compact 矩阵
- `/memory/promote` 目标目录、回读命令、包裹标签不再写死（`NEBULA_VAULT_ROOT`/`NEBULA_PROMOTE_SUBDIR`/`NEBULA_READBACK_FMT`/`NEBULA_VAULT_WRAP_LABEL`）
- maturity 回归结果路径可配置（`NEBULA_REG_LATEST`），保留旧路径兜底

### Fixed

- 启动预热查询缓存对同一 key 写入两次

## [5.1.1] - 2026-08-19

### Fixed

- Embedder `requests.Session` 复用死连接会卡满 12s；改为短超时并丢弃失效 Session
- hybrid 搜索线程共用主 SQLite 连接；`MemoryManager.conn` 改为连接池 property
- `/ask` compose 会拿第一篇无关 vault 顶掉精排（GATEWAY 曾被 HOME.md 顶掉）
- 查询向量磁盘缓存写进生产库并在每次命中 `UPDATE`；已拆到独立 `*.embcache.db`
- `/search` POST 布尔参数未走 `_parse_bool`

### Changed

- SQLite `mmap_size` 256MB → 32MB，`cache_size` 64MB → 8MB，降低常驻 RSS
- systemd 增加 `MALLOC_ARENA_MAX=2`、`MemoryHigh`/`MemoryMax` 资源上限示例
- 查询向量内存 LRU 默认 2000 → 512

### Security

- 发布物不含生产库、备份、`.env`、密钥

## [5.1.0] - 2026-08-18

### Added

- 产品面：`VERSION` + `nebula_meta.py` 单一版本源
- 图片 RAG（qwen2.5-vl-embedding 2048 同空间）
- `POST /memory/promote` 写回 Obsidian `06-Agent会话提炼/`
- `nebula_site.py` 权威/钉死/回读可配置

### Changed

- 精排改为百炼 `qwen3-rerank`（不再用 MiniMax 占排序位）
- `/search` 默认关闭 rerank / smart_rewrite
- `/v5/health` 对 `bw status` 做 stale-while-revalidate

## [5.0.0] - 2026-08-11

### Added

- 首个可部署包：HTTP API + Obsidian 同步 + systemd/Docker + Agent hooks

[5.2.0]: https://github.com/123654lkj/nebula-server/compare/v5.1.1...v5.2.0
[5.1.1]: https://github.com/123654lkj/nebula-server/compare/v5.1.0...v5.1.1
[5.1.0]: https://github.com/123654lkj/nebula-server/compare/v5.0.0...v5.1.0
[5.0.0]: https://github.com/123654lkj/nebula-server/releases/tag/v5.0.0
