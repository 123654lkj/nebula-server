# 02 · API 契约

> 路径名可改；**语义字段尽量保持**，以便多 Agent / 多实现互通。  
> 传输：HTTP JSON 即可；也可 gRPC / MCP，只要字段同构。

## 1. 最小能力集

| 能力 | 语义 | 必选 |
|------|------|------|
| `GET /health` | 存活 + 版本 + 成熟度信号 | ✅ |
| `GET /help?level=mini\|short\|full` | 分级文档；默认 mini | ✅ |
| `POST /v5/bootstrap` | 会话起主题包（强预算） | ✅ |
| `POST /ask` | 日常检索问答 | ✅ |
| `POST /memory/add` | 写入一条记忆 | ✅ |
| `POST /memory/supersede` | 废止旧、树立新 | ✅ |
| `POST /session/extract` | 从 transcript 抽教训（可 auto_write） | 推荐 |
| lifecycle / scorecard / 回归 | 治理与可观测 | 推荐 |

可选：`search` 原始 hit、管理端 CRUD——**Agent 默认路径应走 ask/bootstrap**。

## 2. bootstrap

**请求（概念）**

```json
{
  "focus": "主题或任务摘要",
  "budget": 1500
}
```

**响应应包含**

- `bootstrap`：可直接注入的短文本或结构化小包  
- 预算内；超长必须截断并标注  
- 可选：命中条目的 id 列表（便于追溯）

**语义**：会话或任务开始时「带一点相关记忆」，不是把库倒进 prompt。

## 3. ask

**请求**

```json
{
  "query": "自然语言问题",
  "top_k": 5
}
```

**响应优先形态：contract（推荐默认）**

见 [`schemas/contract.schema.json`](../schemas/contract.schema.json)。核心字段：

| 字段 | 含义 |
|------|------|
| `answer` / `summary` | 压缩结论（可空，若仅候选） |
| `items[]` | 候选记忆 |
| `items[].id` | 稳定 id |
| `items[].trust` | 信任级，见 04 |
| `items[].executable` | 是否可当现行方案 |
| `items[].readback` | 若有：权威原文路径/URI（抽象） |
| `items[].category` | lesson / decision / infrastructure / … |
| `pack` | 可选：比 contract 稍完整但仍有预算 |

**硬约定**：Agent **不要**默认序列化全部 raw hits 进上下文。

## 4. memory/add

```json
{
  "content": "可独立理解的完整句子或短文",
  "category": "lesson",
  "importance": 0.6,
  "tags": ["optional"]
}
```

服务端应：

- 内容哈希去重或近重合并  
- 拒绝明显密钥形态（见 07）  
- 返回 id + 是否新建  

## 5. supersede

```json
{
  "old_id": "…",
  "new_content": "…",
  "reason": "纠正原因"
}
```

或 `old_query` 定位 + 新正文。旧条目标 `superseded` / `executable=false`。

## 6. help 分级

| level | 用途 |
|-------|------|
| `mini` | 默认；Agent 系统提示可常驻 |
| `short` | 人/Agent 速查 |
| `full` | 人类手册；**禁止**默认注入每轮 prompt |

## 7. 版本

路径中的 `v5` 仅表示契约代际；实现可自定，但变更破坏字段时须 bump 并写迁移说明。
