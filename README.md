# 星枢 Nebula Engine（Rust）

> 分支：`rust` · 现网 SQLite **原库原表**，HTTP API 对齐 Python 5.1.x  
> 常驻目标：RSS 约 80MB（Python 版 350MB+）

## 它做什么

Agent 可信记忆检索：`POST /ask` → `contract` / `pack`。  
向量仍走百炼 `qwen2.5-vl-embedding`（2048 维），精排 `qwen3-rerank`，**不在进程内加载大模型**。

## 构建

```bash
cargo build --release
sudo install -m 755 target/release/nebula-engine /usr/local/bin/nebula-engine
```

环境变量（密钥只走 EnvironmentFile，勿提交）：

| 变量 | 含义 |
|------|------|
| `NEBULA_DB_PATH` | 现网 `memory_vectors.db` |
| `NEBULA_PORT` | 默认 `26670` |
| `NEBULA_BIND` | 默认 `0.0.0.0` |
| `BAILIAN_API_KEY` | embedding / rerank |
| `NEBULA_LLM_API_KEY` / `BAILIAN_CHAT_URL` | 抽取 / 压缩 |

```bash
export NEBULA_DB_PATH=./data/memory_vectors.db
export BAILIAN_API_KEY=...
./target/release/nebula-engine
```

systemd 样例：`deploy/nebula-memory.service`。

## 验收

```bash
curl -s http://127.0.0.1:26670/health
# "engine":"rust"

python3 scripts/nebula_regression.py --base http://127.0.0.1:26670 --extra
```

Python 版在 `main` 分支。本分支是常驻引擎的 Rust 实现，不改 SQLite schema。
