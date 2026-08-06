# 03 · Agent SOP

## 1. 何时必须调星枢（L2）

| 触发 | 动作 |
|------|------|
| 会话/任务开始且主题已知 | `bootstrap(focus, budget)` |
| 写代码 / 修 bug / 动 infra | `ask` 再动手 |
| 用户说「之前 / 上次 / 不是已经」 | `ask`；未命中也要说搜过 |
| 架构选型 | `ask` + 外部资料，不从零发明 |
| 重要收尾 | `add` 或 `session/extract` |

**无召回就改 = 违规**（与方法论五门一致）。

## 2. 吃什么字段

```text
优先：bootstrap 文本 / ask.contract
其次：pack（仍受预算）
禁止默认：raw results 全文 dump
```

裁决：

1. `executable == false` → 不当现行  
2. `trust` 低或 `superseded` → 当线索或丢弃  
3. 有 `readback` → **必须**再读 L1 原文  
4. 具体 IP/端口/路径/版本 → 记忆后还要 L5 验证  

## 3. 标准流水线

```text
1) bootstrap（若接任务）
2) ask
3) 裁决 trust / executable / readback
4) 必要时 L1 回读、L5 实测
5) 执行（懒惰阶梯：配置 > 一行 > 库 > 最小改）
6) 验证
7) 重要结论 add；纠正则 supersede
```

## 4. 与 MCP / CLI 的关系

实现可暴露：

- HTTP REST  
- MCP tools：`memory_ask` / `memory_add` / …  
- CLI：`mem bootstrap` / `mem ask`  

**工具名可变，SOP 不变。**

## 5. Token 纪律

- SessionStart 只注 mini help + 小 bootstrap  
- 禁止把 full 文档每轮塞进 system  
- 多跳任务复用已注入 contract，勿重复 ask 同一句  

## 6. 失败与降级

| 情况 | 行为 |
|------|------|
| L2 不可用 | 明说降级；L1/L5 仍可用则继续 |
| 零命中 | 「搜过，未命中」；禁止编造「上次是 X」 |
| 超时 | 缩短 top_k / 关 rerank；先返回向量 top |
