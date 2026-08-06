# 验证记录

## 完整包应包含
- [x] 星枢 HTTP 服务（脱敏）
- [x] 黑曜石/Obsidian vault 模板
- [x] vault → 星枢同步脚本
- [x] scorecard / regression / backup
- [x] Docker Compose（API + sync）
- [x] npm CLI（setup/start/sync/docker:up）
- [x] systemd 单元

## 部署验证（发布前）
```bash
cp .env.example .env   # 填 key
npm run docker:up
npm run health
npm run sync
```
