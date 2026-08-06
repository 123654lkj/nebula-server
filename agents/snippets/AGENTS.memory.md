# 记忆栈门禁（Nebula L2 · 可合并进任意 Agent 的 AGENTS.md）

> 复制本段到 Grok / Codex / Claude / Cursor / Hermes / Reasonix / OpenClaw 的全局或项目指令。

## 分层
- **L1** 黑曜石/Obsidian 笔记 = 权威正文  
- **L2** 星枢 Nebula = 语义索引（默认 `http://127.0.0.1:26670`）  
- **L4** 密码管理器 = 密钥（禁止进笔记/向量）

## 硬门禁
1. 写代码 / 改 infra / 用户提「之前/上次」→ **先** bootstrap 或 ask  
2. 只用 **contract / pack / bootstrap**；禁止把 raw hits 当全文上下文  
3. `executable=false` 或 superseded → **禁止当现行**  
4. 命中带 **readback** → 回读 L1 原文再结论  
5. 密钥 / token → 只走 L4；L2 只存指针  
6. 用户纠正 → 立刻改行为并 **add/supersede** 写回  

## 接任务顺序
召回 → 懒惰阶梯(ponytail) → 最小改动 → 验证再断言 → 纠正写回

## API 速查
```
POST /v5/bootstrap  {"focus":"主题","budget":1500}
POST /ask           {"query":"...","top_k":5}
POST /memory/add    {"content","category","importance"}
GET  /v5/health
GET  /help
```

## 配套 skills（安装后按名触发）
`workflow-discipline` `ponytail` `recall-before-code` `verify-before-assert` `correction-capture` `nebula-recall`
