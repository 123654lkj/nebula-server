# 验证记录

## 完整包应包含
- [x] 星枢 HTTP 服务（脱敏，无硬编码 key / 内网路径）
- [x] 黑曜石/Obsidian vault 模板
- [x] vault → 星枢同步脚本（`NEBULA_DB_PATH` 可移植）
- [x] scorecard / regression / backup
- [x] Docker Compose（API + sync）
- [x] npm CLI（setup/start/sync/docker:up）
- [x] systemd 单元 + install-systemd.sh

## 部署验证

```bash
cp .env.example .env   # 填 BAILIAN_API_KEY
./install.sh           # 或 docker compose up -d --build
./start.sh             # 后台可: nohup ./start.sh &
curl -sf http://127.0.0.1:26670/v5/health
curl -sf http://127.0.0.1:26670/help
npm run sync           # 可选：同步 vault 模板
```

## Agent 层
- [x] agents/skills 五门 + nebula-recall
- [x] hooks common sh/ps1 + 各 Agent 说明
- [x] OpenCode plugin
- [x] install-agents.sh / .ps1
