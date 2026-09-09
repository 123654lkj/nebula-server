# 星枢完整使用与运维手册

> 适用实现：本仓库 **`rust` 分支，Nebula Engine 5.2.0**。  
> 核对基线：提交 `52276f8`，核对日期 **2026-09-09**。  
> 本文依据 Rust 路由和处理函数编写。接口版本字段、旧 Python 文档、部署脚本可能不同，差异在文中明确列出。示例中的写操作不会因阅读本文而自动执行。

本手册面向三类读者：日常查记忆的人、接入星枢的 Agent 开发者、维护服务的管理员。先完成第 3 章，再按目录查需要的部分。星枢是记忆索引与证据组织服务：笔记原文负责保存权威事实，星枢负责查找、排序、摘要、关联与回读线索。

## 目录

1. [版本、部署与文档如何对账](#s01)
2. [组成、数据流和基本术语](#s02)
3. [五分钟开始使用](#s03)
4. [请求与响应通用规则](#s04)
5. [问答、检索、精排与改写](#s05)
6. [如何理解结果与执行边界](#s06)
7. [新增、更新、删除与替代记忆](#s07)
8. [图片与文件导入](#s08)
9. [Obsidian 笔记同步和原文回读](#s09)
10. [会话召回、抽取和写回](#s10)
11. [Vaultwarden 密钥桥接](#s11)
12. [标签、关联、分层和生命周期](#s12)
13. [全部 HTTP 路由速查](#s13)
14. [MCP 接口及兼容范围](#s14)
15. [配置文件、环境变量与 CLI](#s15)
16. [Rust 安装与 systemd 部署](#s16)
17. [升级、灰度验证与回滚](#s17)
18. [备份、恢复与迁移](#s18)
19. [健康、回归和性能观测](#s19)
20. [常见故障逐项排查](#s20)
21. [已知限制与容易踩的坑](#s21)
22. [Agent 接入约定和操作清单](#s22)
23. [源码索引与文档维护](#s23)

<a id="s01"></a>
## 1. 版本、部署与文档如何对账

### 1.1 分清四种版本信息

| 信息 | 如何读取 | 能证明什么 |
|---|---|---|
| Git 源码提交 | `git rev-parse HEAD` | 检出的源码版本 |
| 磁盘二进制 | `nebula-engine --version` | 此路径上的二进制自报版本 |
| 运行进程 | `/health`、`/v5/health`、systemd MainPID | 当前 API 服务进程的自报版本 |
| 文档 | README、VERSION、`/help` | 文档或部署清单的标注，可能滞后 |

发布版本相同不意味着二进制完全相同。确认某个构建是否已部署，需要比较 SHA-256，并确认运行进程没有继续持有替换前的旧可执行文件。只运行磁盘上的 `--version` 不能证明服务已经重启加载它。

```bash
git branch --show-current
git rev-parse HEAD
git status --short
cat VERSION
/usr/local/bin/nebula-engine --version
systemctl show nebula-memory -p MainPID -p ExecStart -p ActiveState
sudo ss -lntp | grep ':26670'
curl -sS --max-time 10 http://127.0.0.1:26670/health
sha256sum /usr/local/bin/nebula-engine target/release/nebula-engine
```

### 1.2 端口的三个概念

| 场景 | 端口 |
|---|---|
| Rust 引擎未配置端口时 | **26672** |
| 本仓库示例配置文件 | **26670** |
| 虎虎当前主服务 | **26670** |

端口实际由 CLI、环境变量和配置文件共同决定。`26671` 上另有服务不代表它是星枢代理；需要分别检查 owner、命令行和响应。

普通用户运行 `ss -p` 看不到 owner，通常只能说明没有足够的进程信息读取权限。用 `sudo ss -lntp` 核对后再判断。`/proc/PID/net/tcp` 是进程所在网络命名空间的 TCP 表，不是该进程独占的 socket 清单；不能仅据此给进程认领端口。

### 1.3 Rust 与 Python 启动入口

本手册以 `src/main.rs`、`src/http.rs` 为准。仓库仍保留 Python 文件以兼容旧部署：

- `start.sh` 启动 `vector_memory_server.py`。
- 当前 `Dockerfile` 和 `docker-compose.yml` 也是 Python 部署入口。
- `deploy/nebula-engine.service` 是旧侧路实例示例，包含历史路径和资源限制，不能原样覆盖虎虎主服务。
- 想运行本文核对的 Rust 实现，构建并运行 `target/release/nebula-engine`。

<a id="s02"></a>
## 2. 组成、数据流和基本术语

```mermaid
flowchart TD
  A[人或 Agent] --> B[HTTP /ask 或 MCP]
  B --> C[Rust 检索与证据组织]
  C --> D[SQLite 记忆与关联]
  C --> E[百炼 embedding]
  C --> F[可选 rerank / LLM]
  G[Obsidian 原文] --> H[Vault 同步]
  H --> D
  C --> I[contract / pack / results]
  I --> J[按 readback 阅读原文]
  J --> G
  A --> K[Vaultwarden 密钥桥]
```

| 名称 | 含义 |
|---|---|
| memory / 记忆 | SQLite 内的一条记录，有 id、正文、来源和元数据 |
| source_file | 来源标识；可能是逻辑路径、会话来源或应用名称，不一定是可直接打开的路径 |
| `vault:notes/...` | 笔记逻辑身份；`notes/` 不意味着物理目录必须叫 notes |
| trust | 来源可信度分类，例如 canon、source、hearsay、synthesis、superseded |
| contract | 面向 Agent 的简洁结论、置信度、可执行标志及回读提示 |
| pack | 控制体积后的证据包 |
| results | 结构化命中列表，可用于界面、筛选和原文核验 |
| readback | 原文回读提示；应检查来源与目标路径，再用自己的工具读取 |
| bootstrap | 会话开始时的有限上下文包 |
| embedding | 内容对应的向量；默认模型 `qwen2.5-vl-embedding`，2048 维 |
| rerank | 用 `qwen3-rerank` 等配置模型重新排序候选 |
| memory_layer | semantic / episodic / procedural 等记忆类别 |
| supersede | 标记旧记忆已被替代，保留历史线索 |

Rust 内存矩阵使用 i16 量化表示；SQLite 仍保存数据与向量。没有在进程里加载完整的大语言模型。RSS 会随数据、缓存和操作变化，不能把某次 54 MB 当作永久上限。

<a id="s03"></a>
## 3. 五分钟开始使用

### 3.1 Linux / macOS

在星枢服务器本机执行，或将 BASE 换成自己能访问的地址：

```bash
BASE=http://127.0.0.1:26670
curl -sS --max-time 10 "$BASE/health"
curl -sS --max-time 10 "$BASE/help"
curl -sS --max-time 30 "$BASE/ask" \
  -H 'Content-Type: application/json' \
  -d '{"query":"星枢怎么用","top_k":5,"llm_deep":"off","no_cache":true}'
```

如果有 jq，可以只取需要的字段：

```bash
curl -sS --max-time 30 "$BASE/ask" \
  -H 'Content-Type: application/json' \
  -d '{"query":"星枢怎么用","llm_deep":"off"}' \
  | jq '{status,contract,pack,count,result_cache_hit}'
```

### 3.2 Windows PowerShell：中文不乱码

```powershell
$nebulaBase = 'http://127.0.0.1:26670'
$nebulaBody = @{query='星枢怎么用'; top_k=5; llm_deep='off'; no_cache=$true} |
    ConvertTo-Json -Compress
$nebulaBytes = [System.Text.Encoding]::UTF8.GetBytes($nebulaBody)
$nebulaReply = Invoke-RestMethod -Uri "$nebulaBase/ask" -Method Post `
    -ContentType 'application/json; charset=utf-8' -Body $nebulaBytes -TimeoutSec 30
$nebulaReply.contract
$nebulaReply.pack
```

使用 `curl.exe` 才明确调用 Windows 上的 curl 可执行文件；Windows PowerShell 某些环境中的 `curl` 是别名。把 JSON 保存到文件时用 UTF-8 无 BOM，复杂内容用序列化器，避免手拼引号。

### 3.3 Python 标准库客户端

```python
import json
import urllib.request
import urllib.error

def nebula_ask(base, query):
    payload = json.dumps({
        "query": query, "top_k": 5, "llm_deep": "off", "no_cache": True
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/ask", data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # 不将包含密钥或私人正文的完整响应写入公共日志。
        raise RuntimeError(f"Nebula HTTP {exc.code}") from exc

reply = nebula_ask("http://127.0.0.1:26670", "星枢怎么用")
print(reply.get("contract", ""))
```

### 3.4 不在服务器本机

优先通过已获授权的 SSH 连接建立本地转发；以下使用 SSH 配置中已有的 `huhu` 主机名：

```bash
ssh -N -L 127.0.0.1:27670:127.0.0.1:26670 huhu
```

保留此 SSH 会话，客户端连接 `http://127.0.0.1:27670`。左边 27670 是客户端端口，右边 26670 是虎虎端口。手机或另一台电脑上的 127.0.0.1 指它自己，不是虎虎。

### 3.5 使用帮助和界面

- `/help`：mini，适合 Agent 初次接入。
- `/help?level=short`：速查。
- `/help?level=full`：部署目录中的完整 API 文本，不等同于本 GitHub 手册。
- `/help?level=short&format=json`：JSON 包装，含 text、chars、est_tokens。
- `/docs`：与 `/help` 使用同一个处理函数。
- `/ui`：搜索界面；优先读取服务器 `/opt/nebula/scripts/vm-search-ui.html`，不存在则返回内置简易搜索页。

<a id="s04"></a>
## 4. 请求与响应通用规则

1. POST 示例默认使用 `Content-Type: application/json` 和 UTF-8。
2. 同时检查 HTTP 状态和 JSON 内容。部分兼容接口即使内部失败，也可能返回 HTTP 200、空对象或 `status:error`。
3. 不假设所有路由都接受相同参数。未知 JSON 字段往往被忽略，不会自动报错。
4. 普通查询可重试；写入、删除、压缩和写回不能在响应不明时盲目重试。
5. 时间戳、计数、RSS、检索分数都是运行时值，示例不保证与你的实例一致。
6. 路由层声明默认 6 MiB body 限制；部分路径手工读取 request body，不能把这个声明当成所有接口均已验证的上传边界。正式网关仍应限制请求大小。
7. 当前默认 CORS 是 permissive，不提供访问控制。安全边界见第 21 章。

常见状态：400 参数错误或密钥拒绝；404 记录或图片不存在；409 同步正在进行；500 内部错误；503 外部向量服务或密码本不可用。各接口对错误的具体映射不同。

<a id="s05"></a>
## 5. 问答、检索、精排与改写

### 5.1 `/ask`：日常推荐入口

支持 GET、POST；`/search_pack` 是同一处理函数的别名。GET 可用 `q` 或 `query`，POST 可用 `query` 或 `q`。

| 参数 | 默认 | 说明 |
|---|---|---|
| query / q | 空，必须提供查询或图片 | 问题正文 |
| top_k | 5 | 返回结果数量，客户端应自行设置合理上限 |
| max_chars | 280 | 单条内容预算 |
| max_total_chars | 1800 | 内容总预算，不是整个 JSON 响应的字节硬上限 |
| use_hybrid | true | 混合检索 |
| hops | 2 | 关联扩展深度 |
| use_graph | true | 是否使用图关联 |
| engine | ultimate | 支持 reflect、legacy；其他值进入 ultimate 分支，一般保留默认 |
| llm_deep | auto | 深搜策略；日常快速查询可设 off |
| llm_answer | false | 是否生成 LLM 回答 |
| rerank | true | 是否尝试精排 |
| reader | false | 传给问答处理流程的读取选项；不能代替客户端核验原文 |
| no_cache | false | 绕过本次结果缓存的读写 |
| temporal_intent | 自动分类 | 时间意图；常见 past / present |
| as_of | 无 | 时间参考，解析规则见 `src/temporal.rs` |
| tenant_id | 站点默认 | POST 可传，也可用 `X-Nebula-Tenant`；不构成完整租户隔离 |
| category | 无 | POST 的类别过滤；不要假设 GET 同样读取此参数 |
| image / image_url | 无 | POST 图片参考；纯图片时内部使用 image-query 标记 |

`llm_deep:off` 只关闭深搜，不等于关闭 embedding 或精排，也不保证查询完全离线。

诊断示例：

```json
{
  "query": "虎虎笔记和星枢谁优先",
  "top_k": 5,
  "llm_deep": "off",
  "llm_answer": false,
  "no_cache": true
}
```

### 5.2 `/search`：更直接的检索列表

支持 GET、POST。**GET 的查询字段是 `q`**，不要把 `/ask` 的 GET 别名支持机械套过来。

| 参数 | 默认 / 可用方式 |
|---|---|
| q（GET）；query 或 q（POST） | 必须有文本或图片 |
| top_k | 5 |
| use_hybrid | true |
| rerank | false，与 /ask 不同 |
| pack | true，按来源去重和裁剪 |
| compact | true |
| full | false；true 时关闭 compact |
| category | GET 也可用 cat |
| explain | POST 支持，默认 false |
| image / image_url | POST 支持 |

该接口在 query embedding 失败时直接返回 503。`/ask` 的实现可能继续使用缓存向量或其他检索路径，因此 `/ask` 成功不证明实时 embedding 一定可用。

缓存注意：当前 `/search` 结果缓存 key 没有包含全部筛选和格式参数；同一查询切换 category 等参数可能命中不合适的缓存。敏感筛选不能只信此结果，参阅第 21 章。响应 `cache_hit` 在此实现里有固定赋值，应看 `result_cache_hit` 判断结果缓存命中。

### 5.3 专用检索接口

| 接口 | JSON 参数 | 行为 |
|---|---|---|
| POST `/search_rerank` | query/q，top_k=10，rerank_candidates=5 | 获取候选后精排 |
| GET `/reranker/status` | 无 | 查看精排配置和统计 |
| POST `/rewrite_search` | query/q，n_rewrites=2 | LLM 生成改写后合并候选 |
| POST `/v4/reflect` | query，top_k=5，max_chars=280，category，use_graph=true，hops=2 | 兼容反思问答 |
| POST `/v5/answer` | query，top_k=5，use_graph=true，hops=2，llm_deep=auto，llm_answer=false | 直接调用 ultimate 问答 |

空结果先查来源是否存在、同步范围是否正确和外部模型连接情况。不要为了“有答案”而不断提高 top_k 或把会话碎片提升为权威。

<a id="s06"></a>
## 6. 如何理解结果与执行边界

读取顺序建议：HTTP → status/error → contract → composed → results → readback 原文。

| 字段 | 用法 |
|---|---|
| composed.confidence | 排序流程的置信指标，不是概率保证 |
| composed.executable | 系统判断；false 时不能把结果当现行操作依据 |
| composed.readbacks | 原文入口集合 |
| results[].id | 用于关联、替代或更新的记录 ID |
| results[].source_file / src | 核对来源身份 |
| results[].trust | 判断是权威、来源材料、会话线索还是过时记录 |
| results[].score | 相对排序分数，不宜跨不同 query 比较 |
| results[].content / full / abstract | 不同压缩层级的正文 |
| result_cache_hit | 结果缓存是否命中 |
| timing / elapsed_ms | 观察耗时；并非所有接口都提供完全相同字段 |

`canon` 由来源规则参与判定，不能自动证明文件内容正确或仍然最新。`source` 也不等于用户已授权执行。`hearsay` 用作线索，`synthesis` 要追溯证据，`superseded` 不能继续当现行。

回读命令可能包含空格、中文或外部来源文字。客户端应解析允许的主机和文件路径，调用文件工具；不要不加检查地把任意字符串交给 shell。命中 `vault:*` 后，应读取真实文件，核对日期和上下文。

若“服务器还有记录”但“原文路径不存在”，先检查映射和文件迁移。不能把数据库缓存正文冒充已经回读过的原文。

<a id="s07"></a>
## 7. 新增、更新、删除与替代记忆

本章操作会写数据库。先确认写入内容有明确用途、来源可追溯，并遵守调用者的授权规则。

### 7.1 新增文本：POST `/memory/add`

```json
{
  "content": "示例项目的构建入口是 make build；来源为已审核的项目 README。",
  "source": "example-project/README.md",
  "category": "project",
  "importance": 0.5,
  "tags": ["example-project", "build"],
  "metadata": {"reviewed": true},
  "semantic_dedup": true
}
```

| 字段 | 默认 / 说明 |
|---|---|
| content | 文本；无文本时需要 image |
| source | api |
| category | 可选；常用 fact、project、lesson、decision、infrastructure、security |
| importance | 0.5；通常使用 0–1 范围，客户端自行约束 |
| tags | 可选字符串数组 |
| metadata | 可选 JSON 对象 |
| semantic_dedup | true |
| created_at | 可选；使用时间解析器 |
| tenant_id | 可选；header 也可提供 |

成功后检查返回的 id、`is_duplicate` 和实际结果；重复写入不一定新增记录。密钥样式正文会在本 JSON 接口调用向量服务前返回 400、`error:secret_in_content`，错误不回显完整密钥。

这不是全系统防泄露承诺：更新、文件导入、MCP、写回笔记的检查顺序不完全一致，见第 21 章。

### 7.2 更新：PUT `/memory/{id}`

支持 content、category、importance、tags。修改 content 会尝试重新生成向量；不存在的 id 返回 404。

```json
{"category":"lesson","importance":0.7,"tags":["reviewed"]}
```

注意：当前实现中 embedding 失败可能仍更新正文，造成正文与向量不同步；并且 update 路径没有与 `/memory/add` 相同的密钥预检查。不要用该接口写密钥。更新后用 `no_cache:true` 查询核验内容。

### 7.3 替代：POST `/memory/supersede`

```json
{"old_id":123,"new_id":456,"note":"原结论已由审核后的新记录替代"}
```

old_id 必填，也接受 id 别名；new_id 和 note 可选。对历史纠正优先使用替代关系，而不是删掉全部证据。示例 ID 必须换成实际核对过的 ID。

### 7.4 删除：DELETE `/memory/{id}`

删除数据库记忆，原始笔记不因此删除；同步仍可能重新导入来源。当前路由忽略底层删除结果并返回 status:ok，因此不能只靠该响应判断删除成功。用数据库副本或独立验证查询确认，必要时处理结果缓存。

当前没有注册通用 GET `/memory/{id}`。同一路径只注册 PUT 和 DELETE；取原文用返回的回读路径或检索结果，不要凭旧版接口印象调用。

<a id="s08"></a>
## 8. 图片与文件导入

### 8.1 图片写入

POST `/memory/add` 的 JSON 支持 image、image_url、image_path；也接受 multipart 的 content、source、category、importance、image。

```bash
curl -sS --max-time 60 "$BASE/memory/add" \
  -F 'content=示例设备接线图' \
  -F 'source=example-project' \
  -F 'image=@./diagram.png'
```

此命令会上传客户端文件并新增索引。JSON 中本地文件路径指**服务器能读取的路径**，不是客户端磁盘。

返回图片记录后用 GET `/memory/image/{id}` 读取。实现会检查图片路径是否位于 image_dir 内；没有本地文件时，部分远程来源会返回重定向相关 JSON，应检查 HTTP 和响应，不假设浏览器一定能自动跳转。

图片向量会调用外部 embedding 服务；文字描述、图片和来源须符合调用者的数据使用约束。远程 image URL 必须来自可信输入，不能把未受控 API 当作任意 URL 抓取服务开放。

### 8.2 文件导入：POST `/ingest`

```json
{
  "filepath": "/srv/example/README.md",
  "chunk_size": 500,
  "overlap": 80,
  "importance": 0.5,
  "category": "project",
  "section_title": "示例项目说明"
}
```

非图片文件按 UTF-8 文本处理，不是通用 PDF、Word、音频解析器。接口会读取服务器文件；文件不存在时当前返回 200、added:0。embedding 失败的分块也可能被跳过，所以 added:0 不足以定位原因。

持续维护的 Markdown 笔记优先使用 Vault 同步，临时一次性文档才用 ingest；不要对同一来源同时建立多套重复导入流程。

<a id="s09"></a>
## 9. Obsidian 笔记同步和原文回读

### 9.1 三个路径必须分清

| 类型 | 虎虎示例 |
|---|---|
| 物理根目录 | `/home/huhu/obsidian-vault` |
| 数据库逻辑来源 | `vault:notes/HOME.md` |
| 原文实际路径 | `/home/huhu/obsidian-vault/HOME.md` |

正确的虎虎配置：

```ini
[Service]
Environment=NEBULA_VAULT_ROOT=/home/huhu/obsidian-vault
Environment="NEBULA_PATH_MAP=gateway/=/opt/,notes/=/home/huhu/obsidian-vault/{suffix}"
Environment="NEBULA_READBACK_FMT=rxt read --host huhu {path}"
```

`{suffix}` 是匹配前缀之后的剩余路径。对于 notes/HOME.md，前缀 notes/ 被替换为物理根目录。没有 `{suffix}` 时保持旧行为：目标目录与**完整逻辑路径**拼接。例如 gateway/=/opt/ 得到 /opt/gateway/…。

### 9.2 先看候选，再同步

```bash
curl -sS "$BASE/vault/status"
curl -sS "$BASE/vault/sync" -H 'Content-Type: application/json' \
  -d '{"dry_run":true,"only":"HOME.md","prune":false}'
```

确认 root 和候选文件正确后，实际同步：

```bash
curl -sS --max-time 120 "$BASE/vault/sync" \
  -H 'Content-Type: application/json' \
  -d '{"only":"HOME.md","prune":false}'
```

参数：force=false；dry_run=false；only 为相对路径的**子串筛选**，不是正则也不是文件列表；prune=true，但实现仅在未指定 only 的全量同步时执行来源清理。

观察 root、files、stats.synced、added、deleted、errors、secret_skipped、skip 和 pruned。added 为 0 可能是去重或无需更新；files 为 0 要查根目录、筛选和文件大小。

### 9.3 筛选规则的真实语义

- 递归寻找 Markdown 文件。
- 名称匹配 exclude_names 的文件排除。
- 空文件与超过 max_bytes 的文件跳过，默认上限 120000 字节。
- include 命中可允许文件穿过 exclude_dirs 目录过滤。
- **include 不是严格的全局白名单**：不在排除目录的 Markdown，即使未命中 include，仍可成为候选。
- 默认 exclude_dirs 含团子学习、归档和 Obsidian 内部目录，但有 include 例外。
- 避免在库内放形成循环的目录符号链接。

迁移根目录后先 dry_run；不要立即 force 全量同步。保留旧同步状态和数据库备份，用 `prune:false` 检查新范围，确认没有误删来源后再启用常规同步。

### 9.4 定时同步与并发

`NEBULA_VAULT_SYNC_INTERVAL` 以秒为单位，0 关闭。主服务可设 900。引擎内部防止同步重入，冲突返回 409。独立验证实例设 0，且使用数据库副本，避免与主服务竞争写入。

### 9.5 写回笔记：POST `/memory/promote`

支持 content 或已有 id，title 可选，source 默认 promote。目标目录为 vault_root 下的 `NEBULA_PROMOTE_SUBDIR`，默认 `06-Agent会话提炼`；重名会追加序号。

```json
{"title":"示例项目部署约定","content":"经过审核的部署说明。","source":"example-project"}
```

检查响应 path 是否存在、vault_key 是否正确、nebula 是否成功。当前先写文件再写索引，所以可能出现笔记成功而 nebula:null。这个接口没有事务性地同时提交文件和数据库，也没有覆盖所有敏感文本路径的预检查；不要提交密钥。

<a id="s10"></a>
## 10. 会话召回、抽取和写回

### 10.1 会话开始

POST `/v5/bootstrap`：focus（也接受 query/q）、budget_chars=2400、no_cache=false。GET 支持 focus/q。

```json
{"focus":"排查示例项目部署","budget_chars":2000,"no_cache":true}
```

只注入 bootstrap 与需要的证据，不把完整结果、所有历史记录和 full 手册一起塞入每轮提示词。

GET/POST `/v5/layered-recall` 返回分层调用计划。它不是执行所有检索后的完整原文结果；调用者仍须按计划取所需内容。

### 10.2 会话结束：先预览

POST `/v5/session-extract`：

```json
{
  "transcript":"本次会话的必要摘要，不包含密码或无关聊天。",
  "focus":"示例项目",
  "dry_run":true,
  "auto_write":false,
  "max_items":8
}
```

transcript 也接受 notes/session；focus 也接受 query。**默认 auto_write=true、dry_run=false**，所以只想预览时必须显式覆盖。dry_run=true 强制不自动写入。

输出可能包括抽取项、abstract、episode、written 和 vault_drafts，具体依据模型和内容。vault_drafts 是草稿，不是已写好的笔记；先审核内容与来源，再决定是否写回。

### 10.3 生成草稿

POST `/v5/promote-draft`：query 或 evidence_query，notes 或 session_notes。用于组织候选笔记草稿，不能将它与实际写文件的 `/memory/promote` 混淆。

<a id="s11"></a>
## 11. Vaultwarden 密钥桥接

密码和 API key 存 Vaultwarden。星枢只保存“哪个条目可取”的指针。密钥桥依赖服务器上的 Bitwarden CLI、相应账号状态和会话；向量查询正常不代表密码本已解锁。

| 接口 | 输入与行为 |
|---|---|
| GET `/secrets/status` | 查看 CLI 状态 |
| GET `/secrets/list?limit=100` | 列出条目摘要；名称本身也可能敏感 |
| POST `/secrets/resolve` | query/q，按用途找候选条目 |
| POST `/secrets/get` | name/item；默认 reveal=false，只给提示 |
| POST `/secrets/store` | name，password/secret/value；可选 username/user、notes/purpose；register_memory 默认 true |
| POST `/secrets/register` | name，purpose、username 可选；建立指针，不是保存新密码 |
| POST `/secrets/catalog-sync` | 列举并尝试同步指针，当前最多读取 80 项 |

安全的只读示例：

```json
{"query":"示例项目 API"}
```

```json
{"name":"example-api","reveal":false}
```

reveal=true 会返回明文 password，只有明确需要且已获授权的调用者才应使用；不要把响应回显到公共终端日志、GitHub、笔记或模型上下文。store 的真实 password 不要写进 curl 命令历史，使用受控程序或密码本客户端传递。

`notes` 和 `purpose` 可能参与生成指针文本，不能把秘密藏在这些字段里。store 成功也不代表 memory_pointer 成功；密码本与向量服务是两套独立依赖。

<a id="s12"></a>
## 12. 标签、关联、分层和生命周期

| 接口 | 参数 / 效果 |
|---|---|
| GET `/tags/cloud` | limit=30，热门标签 |
| GET `/tags/by-category` | 标签分类计数 |
| POST `/tags/rename` | old、new；修改标签名称 |
| POST `/tags/merge` | from 字符串数组、to；当前逐项改名，注意同名冲突，不保证完整事务合并 |
| POST `/tags/cleanup` | 清理 usage_count 为 0 或 NULL 的标签 |
| GET `/memory/{id}/related` | limit=8，关联记录 |
| GET `/memory/dupes` | limit=50，列举内容 hash 重复组，不自动删除 |
| POST `/memory/infer-layer` | limit=2000，dry_run=false；补记忆分层 |
| POST `/memory/reclassify` | category、limit=100、dry_run=false；批量重新分类 |
| POST `/memory/gc` | 压紧内存向量矩阵并清结果缓存，不等于 SQLite VACUUM，也不会清系统 Swap |
| POST `/v5/lifecycle` | demote_days=21、demote_max=300；降低部分旧合成记录的重要度 |
| POST `/compress` | date，或 days_ago=30；生成按日摘要并改变旧记录状态 |
| POST `/v4/migrate` | limit=0、rebuild_links=true；迁移结构、回填 trust、重建关联 |

批量操作先用数据库副本；支持 dry_run 的显式设 true。compress、migrate 没有同等的 dry_run 合同，不要在“健康检查”中顺手执行。压缩在 LLM 不可用时也可能提交占位摘要，必须先验证依赖并保留备份。

<a id="s13"></a>
## 13. 全部 HTTP 路由速查

下表覆盖核对基线中 `router()` 注册的全部路径。读查询可能更新内存缓存；这里的“写”主要指业务数据、笔记或维护状态的变更。

| 方法 | 路径 | 用途 / 章节 |
|---|---|---|
| GET | `/health` | 进程健康，19 |
| GET | `/help` | 文档，3 |
| GET | `/docs` | 文档别名，3 |
| GET, POST | `/search` | 检索，5 |
| GET, POST | `/ask` | 问答，5 |
| GET, POST | `/search_pack` | ask 别名，5 |
| POST | `/search_rerank` | 精排检索，5 |
| GET | `/reranker/status` | 精排状态，5 |
| POST | `/rewrite_search` | 改写检索，5 |
| POST | `/ingest` | 写：文件导入，8 |
| GET | `/stats` | 统计，19 |
| GET | `/tags/cloud` | 标签，12 |
| GET | `/tags/by-category` | 标签分类，12 |
| POST | `/tags/rename` | 写：标签改名，12 |
| POST | `/tags/merge` | 写：标签合并，12 |
| POST | `/tags/cleanup` | 写：清理，12 |
| POST | `/memory/add` | 写：新增，7、8 |
| GET | `/memory/dupes` | 重复组，12 |
| POST | `/memory/gc` | 内存与缓存维护，12 |
| POST | `/memory/infer-layer` | 写：分层，12 |
| GET | `/memory/image/{id}` | 取图片，8 |
| POST | `/memory/promote` | 写：笔记与索引，9 |
| PUT, DELETE | `/memory/{id}` | 写：更新 / 删除，7 |
| POST | `/memory/reclassify` | 写：分类，12 |
| POST | `/compress` | 写：压缩，12 |
| GET | `/ui` | 页面，3 |
| GET | `/secrets/status` | 密码本状态，11 |
| GET | `/secrets/list` | 条目摘要，11 |
| POST | `/secrets/get` | 可能返回秘密，11 |
| POST | `/secrets/store` | 写：密码本，11 |
| POST | `/secrets/register` | 写：指针，11 |
| POST | `/secrets/catalog-sync` | 写：指针同步，11 |
| POST | `/secrets/resolve` | 找条目，11 |
| GET | `/vault/status` | 笔记同步状态，9 |
| POST | `/vault/sync` | 写：笔记索引，9 |
| POST | `/v5/session-extract` | 默认写：抽取，10 |
| GET, POST | `/v5/layered-recall` | 调用计划，10 |
| POST | `/v5/promote-draft` | 草稿，10 |
| GET, POST | `/v5/bootstrap` | 启动包，10 |
| POST | `/v5/answer` | ultimate 问答，5 |
| POST | `/v5/lifecycle` | 写：生命周期，12 |
| GET | `/v5/health` | 综合报告，19 |
| GET | `/memory/{id}/related` | 关系，12 |
| POST | `/memory/supersede` | 写：替代，7 |
| POST | `/v4/migrate` | 写：迁移，12 |
| POST | `/v4/reflect` | 兼容问答，5 |
| POST | `/mcp` | JSON-RPC，14 |

<a id="s14"></a>
## 14. MCP 接口及兼容范围

`POST /mcp` 接收 JSON-RPC 风格请求，支持 initialize、tools/list、tools/call、notifications/initialized。不要由路径名称推断它实现了所有 Streamable HTTP、SSE 或 stdio 传输要求；应先用你的客户端进行握手测试。

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
```

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

```json
{
  "jsonrpc":"2.0","id":3,"method":"tools/call",
  "params":{"name":"ask_memories","arguments":{"query":"星枢怎么用","top_k":5}}
}
```

返回的 `result.content[].text` 是序列化 JSON 文本，需要再解析一层。

工具清单：ask_memories、search_memories、bootstrap_memories、add_memory_tool、get_memory_stats、get_tags_cloud、session_extract_memories、layered_recall、related_memories、supersede_memory_tool、delete_memory_by_id、search_function、search_class、compress_memories。

工具参数逐项对照：

| 工具 | 参数与默认值 |
|---|---|
| ask_memories | query、top_k=5；内部 deep=auto，不能假设接受 HTTP 全部开关 |
| search_memories | query、top_k=5 |
| bootstrap_memories | **focus_query**、budget_chars=2400 |
| add_memory_tool | content、category、importance=0.5 |
| get_memory_stats | 无 |
| get_tags_cloud | limit=20，与 HTTP 默认 30 不同 |
| session_extract_memories | transcript、focus、dry_run=false；固定 max_items=8，非 dry_run 即写入 |
| layered_recall | focus |
| related_memories | **memory_id**、limit=8 |
| supersede_memory_tool | old_id、new_id、note |
| delete_memory_by_id | **memory_id** |
| search_function | **func_name**、top_k=5 |
| search_class | **class_name**、top_k=5 |
| compress_memories | date，或 days_ago=30 |

HTTP 路由和 MCP 参数不完全相同。尤其不要把 memory_id、func_name、class_name 都写成通用 id/name。

当前已知限制：

- initialize 的 serverInfo.version 仍硬编码为 **5.1.1**，不是运行引擎版本依据；看 `/health`。
- inputSchema 目前只声明 object，没有列出完整字段。
- 核对基线中的 14 个公开工具均有 mcp_call 分支，但参数和错误包装与 HTTP 不完全相同；未知工具名返回 Tool not found。
- MCP add_memory_tool 仍先 embedding 后数据库检查，未覆盖 HTTP 新增接口的前置拒绝顺序。

<a id="s15"></a>
## 15. 配置文件、环境变量与 CLI

### 15.1 优先级与查找顺序

**CLI > 已存在环境变量 > JSON 配置文件 > 代码默认值**。

配置位置：显式 `--config`，否则 `$NEBULA_CONFIG`，否则当前工作目录 `./nebula.json`，否则 `/etc/nebula/nebula.json`。检查配置时的工作目录必须与服务一致。

小写 JSON 键转为 `NEBULA_*`；大写键原样使用。字符串数组转逗号分隔，对象或对象数组序列化为 JSON。未知键也可能物化为环境变量，但没有代码读取就不会生效。

```bash
nebula-engine --help
nebula-engine --config /etc/nebula/nebula.json --print-config
nebula-engine --config /etc/nebula/nebula.json --port 26672 --print-config
```

`--print-config` 只打印指定键集，会对识别到的敏感键做部分遮盖；不是所有实际默认值的完整展开，也不是给外部公开的配置导出器。

### 15.2 主要配置表

| 环境变量 | 默认 / 含义 |
|---|---|
| NEBULA_DB_PATH | `/opt/nebula/data/memory_vectors.db` |
| NEBULA_BIND | `0.0.0.0`；单机入口建议显式 127.0.0.1 |
| NEBULA_PORT | 26672 |
| NEBULA_DOCS_DIR | `/opt/nebula/docs` |
| NEBULA_IMAGE_DIR | 图片目录；未设时由 Engine 按数据位置决定 |
| NEBULA_VAULT_ROOT | 显式值优先；代码还有旧 notes 路径回退，部署必须明确设置 |
| NEBULA_VAULT_SYNC_INTERVAL | 0，秒；900 表示 15 分钟 |
| NEBULA_VAULT_STATE | 默认数据目录下 vault-sync-state.json |
| NEBULA_VAULT_KEY_PREFIX | notes/，逻辑身份前缀 |
| NEBULA_VAULT_WRAP_LABEL | [笔记] |
| NEBULA_VAULT_MAX_BYTES | 120000，文件字节上限 |
| NEBULA_VAULT_CHUNK | 900 |
| NEBULA_VAULT_OVERLAP | 100 |
| NEBULA_VAULT_INCLUDE | glob 列表；实际语义见 9.3 |
| NEBULA_VAULT_EXCLUDE_DIRS | 默认含团子学习/、_归档/、.obsidian/、.trash/ |
| NEBULA_VAULT_EXCLUDE_NAMES | 文件名子串排除规则 |
| NEBULA_VAULT_RULES | JSON 分类规则，用户规则先于内置规则 |
| NEBULA_PROMOTE_SUBDIR | 06-Agent会话提炼 |
| NEBULA_READBACK_FMT | 回读模板，{path} 占位 |
| NEBULA_PATH_MAP | 前缀映射，支持 {suffix} |
| NEBULA_CANON_SUBSTR | 权威来源子串列表 |
| NEBULA_DEMOTE_SUBSTR | 降权来源子串列表 |
| NEBULA_HEARSAY_SUBSTR | 会话线索来源列表 |
| NEBULA_SCRATCH_SUBSTR | 临时碎片来源列表 |
| NEBULA_PIN_REGEX | 需要置顶识别的查询正则 |
| NEBULA_REG_LATEST | 回归报告路径，须与执行器输出对齐 |
| NEBULA_REQUIRE_TENANT | 部分接口要求 tenant；不是完整鉴权 |
| NEBULA_DEFAULT_TENANT | 默认 tenant |
| NEBULA_API_TOKEN | 有配置读取函数，但当前 router 未接入统一检查 |
| BAILIAN_API_KEY / DASHSCOPE_API_KEY | embedding 和 rerank 凭据来源 |
| NEBULA_RERANK | 精排开关，默认开启 |
| NEBULA_RERANK_MODEL | qwen3-rerank |
| NEBULA_RERANK_URL | 精排 endpoint 覆盖 |
| NEBULA_RERANK_INSTRUCT | 精排指令 |
| NEBULA_LLM_API_KEY | LLM 专用 key；无值时可能回退百炼 key |
| BAILIAN_CHAT_URL | LLM 完整请求 URL；代码默认 MiniMax chat/completions |
| NEBULA_LLM_MODEL | 代码默认 MiniMax-M3；部署可以覆盖 |
| NEBULA_EXTRACT_LLM_TIMEOUT | 会话抽取 LLM 超时，见 extract.rs |
| BW_SESSION_FILE | Bitwarden CLI 会话文件 |
| BITWARDENCLI_APPDATA_DIR | Bitwarden CLI 数据目录 |
| RUST_LOG | 日志级别 |

不要把“变量名含 BAILIAN”解释为必然访问百炼。LLM 默认 URL 与 embedding URL 是不同配置。embedding endpoint 在 embed.rs 中为固定常量，不能假设设置 BAILIAN_BASE_URL 就能改变它。

### 15.3 不带密钥的配置示例

```json
{
  "db_path": "/srv/nebula/data/memory_vectors.db",
  "bind": "127.0.0.1",
  "port": 26670,
  "docs_dir": "/srv/nebula/docs",
  "vault_root": "/srv/knowledge",
  "vault_sync_interval": 900,
  "vault_key_prefix": "notes/",
  "path_map": "notes/=/srv/knowledge/{suffix}",
  "readback_fmt": "read {path}",
  "reg_latest": "/srv/nebula/reports/latest.json"
}
```

此处 read 是描述性工具模板，不保证操作系统有同名命令；改成你的实际文件读取工具。不要把真实凭据写进提交到 Git 的配置。

<a id="s16"></a>
## 16. Rust 安装与 systemd 部署

### 16.1 构建

需要 Git、Rust/Cargo 工具链和本机编译环境。构建读取 Cargo.lock，SQLite 依赖启用 bundled。

```bash
git clone --branch rust https://github.com/123654lkj/nebula-server.git
cd nebula-server
cargo test --locked
cargo build --release --locked
./target/release/nebula-engine --version
```

资源紧张时 `CARGO_BUILD_JOBS=2 nice -n 15 cargo build --release --locked`。构建、启动新环境与恢复现网是不同动作；不要对生产数据直接试新构建。

### 16.2 新实例必须验证数据库初始化

Engine 读取现有 SQLite 结构。不要假设对一个任意空文件启动就完成全部初始化；先在独立目录验证库创建、schema 和 `/stats`。迁移已有实例时，使用一致备份副本并核对记录、向量、标签和关联。`/v4/migrate` 是写操作，不是启动前随手执行的只读检查。

### 16.3 示例 systemd unit

下面是需要自行调整用户、目录权限与配置的 Rust 示例，不是要求覆盖现有虎虎服务：

```ini
[Unit]
Description=Nebula Rust memory service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nebula
WorkingDirectory=/srv/nebula
ExecStart=/usr/local/bin/nebula-engine --config /etc/nebula/nebula.json
EnvironmentFile=-/etc/nebula/embed.env
EnvironmentFile=-/etc/nebula/llm.env
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

服务用户需能访问数据库及其 WAL/SHM 所在目录、图片目录、文档、同步状态和笔记目标目录。启用密码本桥还需访问所需 CLI 及受保护会话文件。以最小实际权限配置，不默认运行 root。

### 16.4 systemd 配置顺序

```bash
systemctl cat nebula-memory
systemctl show nebula-memory -p FragmentPath -p DropInPaths -p ExecStart -p MainPID
```

drop-in 按文件名词典顺序合并，后面的同名 Environment 设置可覆盖前面的。**90-xxx.conf 不一定最后**，它会排在 site.conf、vault.conf 前面。此外 EnvironmentFile 值与 CLI 参数也需要核对；最终以服务实际行为和允许读取的生效设置为准。

编辑 unit/drop-in 后 `daemon-reload` 只重新加载 systemd 配置，**不会自动更换已运行进程的环境变量**。代码和进程环境要重启才能生效；`/help` 文本由请求时读取，可不重启更新。

<a id="s17"></a>
## 17. 升级、灰度验证与回滚

### 17.1 升级前

1. 确认用户授权范围及允许的中断窗口。
2. 记录源码分支、提交、工作区改动、运行 PID、二进制 hash。
3. 备份旧二进制、配置、文档和数据库；备份保存到受保护目录。
4. `cargo test --locked`、构建 release、`git diff --check`。
5. 独立实例绑定 `127.0.0.1:26672`，使用数据库副本、独立同步状态，关闭自动同步。
6. 核验 health、help、ask、密钥拒绝顺序、实际回读路径，以及完整回归。

影子实例不要以 root 身份从未知公网接收请求。真实回归可能访问外部模型和密码本，确认测试调用只使用合成示例，不打印真实凭据。

### 17.2 切换

将新二进制先安装为临时文件，再原子 rename 到正式路径；不要原地写正在执行的文件。安装已审核配置，daemon-reload，执行授权的服务重启。

重启后短时间连接拒绝可能是监听器尚未起来，用有限次数重试确认；持续失败则读取日志并回滚。不能把一个重试成功掩盖成全程零中断。

### 17.3 回滚

先确认失败是代码、配置还是数据引起的：

- 代码或配置问题：恢复备份二进制与对应配置，重新加载 systemd 并启动；一般不应把数据库一起倒退。
- 数据被迁移或破坏：停服务后按第 18 章恢复一致备份；这会丢失备份之后的变化，需单独评估。
- 新加 drop-in 回滚时必须撤掉或还原，否则仍可能覆盖已恢复的旧配置。

不把“回滚”写成一条无条件覆盖数据库的命令。

<a id="s18"></a>
## 18. 备份、恢复与迁移

### 18.1 应备份什么

- SQLite 一致备份。
- 原始 Obsidian 笔记和 Git 历史。
- 图片资产目录。
- 同步状态文件和回归报告。
- 二进制、版本文件、docs、配置文件与 systemd unit/drop-in。
- 密钥文件按密码本或机密备份规则保存，不能随普通仓库推送。

### 18.2 在线 SQLite 一致备份

不要在服务写入时只复制 `.db`，忽略 WAL。可使用 Python SQLite backup API，在服务器上执行并把路径换成实际值：

```python
import sqlite3
from pathlib import Path

source = Path('/opt/nebula/data/memory_vectors.db')
target = Path('/var/backups/nebula/manual-snapshot.db')
target.parent.mkdir(parents=True, exist_ok=True)
if target.exists():
    raise RuntimeError('备份目标已存在，请使用新的文件名')
src = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
dst = sqlite3.connect(target)
try:
    src.backup(dst)
    result = dst.execute('PRAGMA integrity_check').fetchone()[0]
    if result != 'ok':
        raise RuntimeError(result)
finally:
    dst.close()
    src.close()
target.chmod(0o600)
print('备份完成，完整性检查通过')
```

该示例只备份数据库，文件、图片和配置另行备份。备份目录也应限制访问；不要把完整记忆库上传到公开 GitHub。

### 18.3 恢复步骤

确认维护窗口 → 停止实例 → 保留当前库、WAL、SHM 作为回退集合 → 验证备份完整性 → 恢复数据库及权限 → 确认配置指向恢复路径 → 启动 → 核对 `/stats`、查询和原文回读 → 最后恢复定时同步。

不要让旧 WAL 与另一时刻恢复的 DB 混用，也不要在进程仍打开库时替换生产 DB。

### 18.4 换机器或换笔记目录

迁移物理路径不一定要改逻辑 source key。保留 `vault:notes/...` 身份，更新 vault_root、path_map 和 readback_fmt，可以避免仅因换路径而重新制造整套来源身份。先同步一份核心文件，检查回读，再逐步扩大范围。

<a id="s19"></a>
## 19. 健康、回归和性能观测

### 19.1 三层检查

| 层次 | 检查 | 不足之处 |
|---|---|---|
| 进程 | systemctl、ss、/health | 不能证明检索质量 |
| 功能 | /ask、/search、/help、/vault/status | 单个成功请求不能覆盖所有路径 |
| 质量 | 固定回归题集 + 原文存在性 | 历史报告不会自动变成实时测试 |

```bash
curl -sS --max-time 10 "$BASE/health"
curl -sS --max-time 15 "$BASE/v5/health"
curl -sS --max-time 10 "$BASE/stats"
systemctl show nebula-memory -p MainPID -p MemoryCurrent -p MemoryHigh -p MemoryMax
sudo journalctl -u nebula-memory --since '10 minutes ago' --no-pager
```

日志中可能有私人路径或模型错误，公开前脱敏。

### 19.2 回归执行器

```bash
python3 scripts/nebula_regression.py \
  --base http://127.0.0.1:26670 \
  --out /srv/nebula/reports/latest.json
```

退出码：0 全通过，1 有失败，2 服务不可用。`--extra` 增加额外探针。题集含虎虎环境、密码本状态和合成密钥拒绝检查，并非所有新部署都能直接满足。

当前脚本除了 `--out` 还会尝试写入虎虎固定 latest 路径；异机使用需核对脚本与 `NEBULA_REG_LATEST`。不要通过删失败用例或降低期望让报告变绿，应修复问题或明确解释环境差异。

`/v5/health` 的 maturity 含历史报告指标。检查报告时间、total、passed 和失败 reasons，而不是只读 score。`true_nb` 是实现中的等级名称，不是第三方认证。

### 19.3 性能问题

```bash
uptime
nproc
free -h
vmstat 1 5
cat /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io
systemctl show nebula-memory -p MemoryCurrent -p MemoryHigh -p MemoryMax -p NRestarts
```

load 高但 CPU 空闲，可能是 IO 或内存回收阻塞。整体还有空闲内存，也可能因 cgroup MemoryHigh 太低而持续回收。先定位进程与 cgroup，再调资源，不要直接清 Swap 或重启网关。

`/memory/gc` 清理的是星枢内存矩阵和结果缓存，不是系统级减压万能按钮。清缓存后的第一次查询可能更慢。

<a id="s20"></a>
## 20. 常见故障逐项排查

### 20.1 连接拒绝

核对客户端所在机器 → BASE 是否正确 → SSH 转发是否仍运行 → systemctl 状态 → sudo ss owner → bind/port → 日志。不要先猜“被别的 namespace 借走端口”。

### 20.2 curl 没有输出

```bash
curl --noproxy '*' -sS --connect-timeout 3 --max-time 15 \
  -w '\nHTTP=%{http_code} total=%{time_total}\n' \
  http://127.0.0.1:26670/health
```

检查 curl 退出码；HTTP=000 不是服务器返回的 HTTP 状态。避免 `-s` 隐藏错误后把空输出解释为“连接通但不回话”。如果 `/health` 快、`/v5/health` 慢，后者还有报告与密码本状态工作，不能直接认定监听器故障。

### 20.3 查询 503

查看 error 是否指向 embedding endpoint；核对服务环境内凭据是否存在、DNS/TLS 和代理是否可用。只探测 endpoint 返回 401 可以证明 HTTP 可达，不能证明实际 key、模型权限和余额正常。不要在调试输出中打印 Authorization。

### 20.4 ask 成功但内容不相关

使用 no_cache:true → 检查 results 来源 → 核验原文存在 → 查 vault/status root → dry_run 指定文件 → 检查 embedding/rerank 和日志。HTTP 200、executable=true 都不能代替内容审核。

### 20.5 同步 files=0

检查实际目录是否迁移、only 是否匹配、扩展名和大小、exclude_names。虎虎曾把笔记从 notes/ 搬到库根目录；旧路径里仍有少数文件时，同步日志可以一直成功，但权威笔记实际未更新。

### 20.6 readback 路径重复 notes/

检查 path_map 的旧拼接行为。根目录没有 notes 子目录时，用 `notes/=/实际根目录/{suffix}`。同时确认加载了支持占位符的新二进制，而非只更新了磁盘文件。

### 20.7 改配置后仍是旧值

检查 CLI 覆盖、工作目录配置文件、EnvironmentFile、drop-in 排序、是否 daemon-reload、是否重启进程。不要连续多次盲目重启；先把最终配置顺序核对清楚。

### 20.8 密码本 503 或 locked

单独核对 Bitwarden CLI 状态、服务用户、会话文件权限、CLI 数据目录和网络。不要删除密码本数据来“重置连接”，不要把会话 token 粘到 GitHub issue。

### 20.9 写入返回不明确

检查是否已产生 id、笔记文件或指针，再决定是否重试。HTTP 成功但 nebula:null、added:0 或空 error 对象时，要按对应接口语义查原因。

### 20.10 版本字段打架

以 `/health` 加进程/二进制核对为准。`/ask` 的 version 有兼容 v4 标记、MCP 初始化有旧版字符串；这些不能推翻实际引擎 v5.2.0。文档、API 兼容版本与产品发布版本须分别记录。

<a id="s21"></a>
## 21. 已知限制与容易踩的坑

以下是核对基线的实现事实，不是对未来版本的承诺。

1. **没有统一 HTTP 鉴权中间件。** `NEBULA_API_TOKEN` 有读取函数但未接到 router；只设这个变量不会保护 API。不能直接暴露到不可信公网。使用受控监听、SSH 或经过验证的外层鉴权。
2. **租户字段不是完整隔离。** 并非所有路由过滤 tenant，结果缓存 key 也未覆盖 tenant 等维度。不要把它作为多租户安全边界。
3. **缓存参数覆盖不全。** `/search` 的 category/格式等参数、`/ask` 的部分选项未全部进入 key。诊断优先 no_cache 的 ask，过滤结果仍要复核。
4. **成功包装不总等于操作成功。** DELETE、ingest、promote 和一些维护路由存在忽略底层错误或局部成功的行为。
5. **前置密钥检查覆盖有限。** 修复覆盖 HTTP 新增文本和公共图片新增路径；PUT、MCP 新增、文本 ingest、promote 等不能假设有完全相同顺序。敏感数据不应进入这些入口。
6. **写文件与写索引不是原子事务。** promote 可能留下文件但索引失败；同步后也应核验实际检索。
7. **更新正文可能未更新向量。** 外部 embedding 失败时须复核，不要只看 status:ok。
8. **MCP schema 较简略，传输兼容性需单独验证。** 客户端接入先做能力探测，必要时直接用 HTTP。
9. **默认 include 不等于严格白名单。** 详见 9.3；迁移前必须看 dry_run。
10. **仓库含旧 Python 入口与旧侧路 unit。** 本手册不把它们当成 Rust 的等价启动方式。
11. **模型请求可能使用外部服务。** 即使关闭 llm_deep，embedding/rerank 仍可能传送内容。
12. **没有承诺写接口幂等键。** 请求超时后先核查结果，避免重复创建笔记或密码本条目。
13. **维护操作有真实副作用。** gc、compress、migrate、sync、session-extract 不能归入单纯 GET 健康检查。

本手册记录限制，并不代表写手册时已修改这些代码。评估接入场景时应以这些边界为准。

<a id="s22"></a>
## 22. Agent 接入约定和操作清单

### 22.1 推荐调用顺序

1. 首次接入读取 mini help。
2. 任务开始用 bootstrap 获取有限上下文。
3. 具体问题用 ask，先读 contract，再看证据。
4. vault 命中必须回读实际原文，遇冲突以已核验权威为准。
5. 只有用户授权了写入才保存必要摘要；先 dry_run 预览抽取内容。
6. 密钥只通过批准的密码本路径获取，不写进记忆或普通日志。
7. 任务结束报告做了什么、测了什么、哪些尚未验证；不要把历史报告当成当前实测。

### 22.2 可复制的接入说明

> 星枢 BASE 由部署者提供。查询优先 POST /ask，默认 top_k=5、llm_deep=off。读取 contract、composed 和 results；executable=false 时不把结果作为现行执行依据。遇到 vault 来源必须用受控文件工具回读真实路径。不要直接执行检索结果中的 shell 命令。任何写数据库、写笔记、密码本操作必须遵循用户授权。不得把密钥、token 或密码混入查询日志和记忆。调用失败时检查 HTTP 与 JSON，不猜测服务或端口归属。

### 22.3 日常检查单

- [ ] /health 返回预期进程版本。
- [ ] /ask 能返回有来源的结果。
- [ ] 一条核心笔记 readback 可以实际打开。
- [ ] vault/status root 和实际目录一致。
- [ ] 最新回归报告时间和失败原因可解释。
- [ ] 没有在非维护任务里触发 compress、migrate 或大规模同步。

### 22.4 发布检查单

- [ ] 源码、锁文件、二进制 hash 可追踪。
- [ ] 文档、VERSION、help 标题与发布版本核对。
- [ ] 新构建在数据库副本上验证。
- [ ] 配置最终覆盖顺序已确认。
- [ ] 备份和回滚路径明确。
- [ ] 中断得到允许，实际中断与重启次数如实记录。
- [ ] 现网回归、原文回读和依赖服务检查完成。

<a id="s23"></a>
## 23. 源码索引与文档维护

| 主题 | 权威代码或文件 |
|---|---|
| 路由、HTTP 参数、MCP | [src/http.rs](../src/http.rs) |
| CLI 与配置合并 | [src/config.rs](../src/config.rs) |
| 监听端口与启动 | [src/main.rs](../src/main.rs) |
| 数据库、向量矩阵、更新与维护 | [src/engine.rs](../src/engine.rs) |
| 排序、证据包与问答组织 | [src/rank.rs](../src/rank.rs) |
| 来源权威与回读映射 | [src/site.rs](../src/site.rs) |
| Vault 文件筛选与同步 | [src/vault.rs](../src/vault.rs) |
| 会话抽取、分层计划 | [src/extract.rs](../src/extract.rs) |
| embedding | [src/embed.rs](../src/embed.rs) |
| rerank | [src/rerank.rs](../src/rerank.rs) |
| LLM 请求 | [src/llm.rs](../src/llm.rs) |
| 密码本桥与模式检测 | [src/secrets.rs](../src/secrets.rs) |
| 图片解析 | [src/image.rs](../src/image.rs) |
| 时间解析 | [src/temporal.rs](../src/temporal.rs) |
| 公共响应类型与版本常量 | [src/types.rs](../src/types.rs) |
| 回归执行器 | [scripts/nebula_regression.py](../scripts/nebula_regression.py) |
| 配置模板（含历史站点值，使用前核对） | [nebula.example.json](../nebula.example.json) |
| Rust 项目概览 | [README.md](../README.md) |
| 按需 API 速查 | [USAGE.short.md](USAGE.short.md) |

维护本手册时：新增或删除 route 后同步第 13 章；调整参数后同步所属章节；改变配置默认值后同步第 15 章；安全或兼容性修复后更新第 21 章，并标注新的核对提交。不要为了统一文档数字而修改历史兼容字段的含义。

**验证记录（2026-09-09）**：对应修复版本在独立数据库副本与虎虎现网上通过 24/24 固定回归；代码测试 10 项通过；JSON 文本及图片描述中的合成密钥在 HTTP 新增接口被前置拒绝；核心查询返回的回读路径实测存在。本文其余接口说明依据源码审阅，未为写手册在生产库执行删除、压缩、迁移或真实密钥取出。
