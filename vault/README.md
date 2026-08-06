# Obsidian Vault（黑曜石笔记模板）

用 [Obsidian](https://obsidian.md) **Open folder as vault** 打开本目录。

笔记正文在 `notes/`。星枢通过 `scripts/vault_to_nebula_sync.py` 增量索引。

```bash
export VAULT_ROOT="$(pwd)/notes"   # 可选，默认已指向此处
npm run sync
```
