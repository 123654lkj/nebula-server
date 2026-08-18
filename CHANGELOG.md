# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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

[5.1.1]: https://github.com/123654lkj/nebula-server/compare/v5.1.0...v5.1.1
[5.1.0]: https://github.com/123654lkj/nebula-server/compare/v5.0.0...v5.1.0
[5.0.0]: https://github.com/123654lkj/nebula-server/releases/tag/v5.0.0
