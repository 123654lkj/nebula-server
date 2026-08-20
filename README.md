# 星枢 Nebula Engine（Rust）

> 分支：`rust` · 现网 SQLite **原库原表**，HTTP API 对齐 Python 5.1.x  
> 常驻目标：RSS 约 50MB（现网 Rust 5.1.1 约 80MB；Python 版 350MB+）

## 它做什么

Agent 可信记忆检索：`POST /ask` → `contract` / `pack`。  
向量仍走百炼 `qwen2.5-vl-embedding`（2048 维），精排 `qwen3-rerank`，**不在进程内加载大模型**。

## 构建

```bash
cargo build --release
sudo install -m 755 target/release/nebula-engine /usr/local/bin/nebula-engine
```

## 配置（CLI > 环境变量 > 配置文件 > 默认）

```bash
nebula-engine --help                       # 全部 CLI 选项
nebula-engine --db ./data/memory_vectors.db --port 26670 \
  --vault-root ~/obsidian/notes --vault-sync-interval 900
nebula-engine --config /etc/nebula/nebula.json --print-config   # 查看生效配置
nebula-engine --set NEBULA_VAULT_MAX_BYTES=200000               # 覆盖任意变量
```

配置文件为扁平 JSON（模板 `nebula.example.json`；自动查找 `$NEBULA_CONFIG` → `./nebula.json` → `/etc/nebula/nebula.json`）。小写键映射 `NEBULA_*`，全大写键原样透传。密钥仍建议走 EnvironmentFile，勿提交。

常用变量：

| 变量 | 含义 |
|------|------|
| `NEBULA_DB_PATH` | 现网 `memory_vectors.db` |
| `NEBULA_PORT` / `NEBULA_BIND` | 默认 `26672` / `0.0.0.0` |
| `NEBULA_VAULT_ROOT` | Obsidian 笔记根目录 |
| `NEBULA_VAULT_SYNC_INTERVAL` | 内置笔记同步间隔秒（0=关） |
| `NEBULA_VAULT_INCLUDE` / `NEBULA_VAULT_RULES` | 白名单 glob / 分类规则 JSON |
| `NEBULA_READBACK_FMT` / `NEBULA_PATH_MAP` | 回读命令模板 / 路径映射 |
| `BAILIAN_API_KEY` | embedding / rerank |
| `NEBULA_LLM_API_KEY` / `BAILIAN_CHAT_URL` | 抽取 / 压缩 |

## Obsidian 联动

- `POST /vault/sync`（`{"force":false,"dry_run":false,"only":"HOME","prune":true}`）：增量同步笔记 → 星枢，白名单/排除/体积帽/分类规则全可配
- `GET /vault/status`：根目录、追踪文件数、上次运行统计
- `NEBULA_VAULT_SYNC_INTERVAL=900`：内置定时器，可替代 `vault-nebula-sync.timer` + Python 脚本（state 文件与 Python 版兼容）
- `POST /memory/promote`：写回笔记目录（`NEBULA_PROMOTE_SUBDIR`，默认 `06-Agent会话提炼`）并入索引

systemd 样例：`deploy/nebula-memory.service`。

## 验收

```bash
curl -s http://127.0.0.1:26670/health
# "engine":"rust"

python3 scripts/nebula_regression.py --base http://127.0.0.1:26670 --extra
```

Python 版在 `main` 分支。本分支是常驻引擎的 Rust 实现，不改 SQLite schema。
