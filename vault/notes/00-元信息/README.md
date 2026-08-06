# 黑曜石笔记 × 星枢

## 部署后路径
- 默认 vault：包内 `vault/notes`
- 也可把 `VAULT_ROOT` 指到你已有的 Obsidian vault

## 同步机制
`scripts/vault_to_nebula_sync.py` 按白名单 + 体积帽增量同步到星枢。
- 排除：大体量目录、`.obsidian`、`.trash`
- 白名单：见脚本顶部 `INCLUDE_REL_GLOBS`
- 状态：`data/vault-sync-state.json`

## Obsidian
用 Obsidian 打开 `vault/` 或 `vault/notes` 上级目录即可。
