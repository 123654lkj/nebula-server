# 10 · Hooks 与门禁 skill

L2 契约 alone 不够：Agent 还要 **会话起强制提醒 + 行为门禁**。  
本仓库用 **hook（机制）+ skills（纪律）** 补齐。

## 1. 两层分工

| 层 | 载体 | 做什么 | 不做 |
|----|------|--------|------|
| Hook | SessionStart 脚本 | 打印口令、拉短 bootstrap、fail-open | 不替代全程裁决 |
| Skills | 五门 + nebula-recall | 编码路径硬门禁、纠正写回 | 不绑定某台机器路径 |

```text
SessionStart hook
        │
        ▼
  短 bootstrap（预算内）
        │
        ▼
   用户任务进入
        │
        ├─ workflow-discipline
        ├─ recall-before-code  ←→  L2 ask
        ├─ ponytail
        ├─ verify-before-assert
        └─ correction-capture  ←→  L2 add/supersede
```

## 2. SessionStart 推荐行为

1. 一行五门口令（让模型“看见”门禁名）  
2. 调 L2 `bootstrap(focus, budget)`，截断输出  
3. 失败则降级提示，**exit 0**  
4. 总耗时控制在 hook timeout 内（如 12s）  

实现样例：[`hooks/session-start-memory.sh`](../hooks/session-start-memory.sh)

## 3. 配置形态（多 Agent）

| 产品 | 样例文件 |
|------|----------|
| Grok | `hooks/grok-session-start.example.json` |
| Claude Code | `hooks/claude-settings.example.json` |
| Cursor | `hooks/cursor-hooks.example.json` |

事件名可能是 `SessionStart` 或 `sessionStart`；以各产品文档为准。  
**命令路径请改成你机器上的安装路径**，不要把私人绝对路径提交回公共仓库。

## 4. 五门 skill（目录）

| Skill | 一句话 |
|-------|--------|
| `workflow-discipline` | 总门禁与接任务顺序 |
| `ponytail` | 最少代码阶梯 |
| `recall-before-code` | 改前召回 |
| `verify-before-assert` | 断言前验证 |
| `correction-capture` | 纠正写回 |
| `nebula-recall` | L2 字段吃法与 hook 衔接 |

拷贝到各 Agent 的 skills 目录即可；保持 SOP 稳定，只改 base URL / CLI。

## 5. 与方法论仓库

全栈 L0–L5 叙事见 [agent-memory-methodology](https://github.com/123654lkj/agent-memory-methodology)。  
本仓库强调：**L2 契约 + 可跑参考实现 + hook/skill 落点**。

## 6. 验收

- [ ] 新会话能看到 hook 输出（或明确降级）  
- [ ] bootstrap 有预算、非全文 dump  
- [ ] 五门 skill 可被 Agent 加载  
- [ ] 纠正路径能 add/supersede  
- [ ] 样例中无私人主机清单与密钥  
