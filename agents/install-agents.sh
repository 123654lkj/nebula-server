#!/usr/bin/env bash
# 一键把 skills/hooks 安装到本机各 Agent
# 用法:
#   ./agents/install-agents.sh              # 全部已检测到的 Agent
#   ./agents/install-agents.sh grok codex hermes
#   ./agents/install-agents.sh --list
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS_DIR="$ROOT/agents"
SKILLS_SRC="$AGENTS_DIR/skills"
HOOK_SH="$AGENTS_DIR/hooks/common/session-start-memory.sh"
HOOK_PS1="$AGENTS_DIR/hooks/common/session-start-memory.ps1"
NEBULA_HOME="${NEBULA_HOME:-$HOME/.nebula}"
BASE_URL="${NEBULA_BASE_URL:-http://127.0.0.1:26670}"

SKILL_NAMES=(workflow-discipline ponytail recall-before-code verify-before-assert correction-capture nebula-recall)

log() { printf '%s\n' "$*"; }

install_skills_into() {
  local dest="$1"
  mkdir -p "$dest"
  for s in "${SKILL_NAMES[@]}"; do
    if [ -d "$SKILLS_SRC/$s" ]; then
      mkdir -p "$dest/$s"
      cp -f "$SKILLS_SRC/$s/SKILL.md" "$dest/$s/SKILL.md"
      log "  skill → $dest/$s"
    fi
  done
}

install_common_hook() {
  mkdir -p "$NEBULA_HOME/hooks"
  cp -f "$HOOK_SH" "$NEBULA_HOME/hooks/session-start-memory.sh"
  chmod +x "$NEBULA_HOME/hooks/session-start-memory.sh"
  if [ -f "$HOOK_PS1" ]; then
    cp -f "$HOOK_PS1" "$NEBULA_HOME/hooks/session-start-memory.ps1"
  fi
  # env helper
  cat > "$NEBULA_HOME/env.sh" <<EOF
export NEBULA_BASE_URL="${BASE_URL}"
export NEBULA_BOOTSTRAP_BUDGET=800
export NEBULA_HOOK_TIMEOUT_S=8
EOF
  log "  common hooks → $NEBULA_HOME/hooks"
}

install_grok() {
  local dir="${GROK_HOME:-$HOME/.grok}"
  [ -d "$dir" ] || mkdir -p "$dir"
  log "[grok] $dir"
  install_skills_into "$dir/skills"
  mkdir -p "$dir/hooks"
  # prefer bash hook; Windows users use install-agents.ps1
  local cmd="bash $NEBULA_HOME/hooks/session-start-memory.sh"
  cat > "$dir/hooks/session-memory.json" <<JSON
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "timeout": 12,
            "command": "$cmd"
          }
        ]
      }
    ]
  }
}
JSON
  log "  hook → $dir/hooks/session-memory.json"
  if [ -f "$dir/AGENTS.md" ]; then
    if ! grep -q "Nebula L2" "$dir/AGENTS.md" 2>/dev/null; then
      {
        echo ""
        echo "<!-- nebula-memory begin -->"
        cat "$AGENTS_DIR/snippets/AGENTS.memory.md"
        echo "<!-- nebula-memory end -->"
      } >> "$dir/AGENTS.md"
      log "  appended AGENTS.memory.md"
    fi
  else
    cp "$AGENTS_DIR/snippets/AGENTS.memory.md" "$dir/AGENTS.md"
    log "  wrote $dir/AGENTS.md"
  fi
}

install_codex() {
  local dir="${CODEX_HOME:-$HOME/.codex}"
  mkdir -p "$dir"
  log "[codex] $dir"
  install_skills_into "$dir/skills"
  if [ -f "$dir/AGENTS.md" ]; then
    if ! grep -q "Nebula L2" "$dir/AGENTS.md" 2>/dev/null; then
      { echo ""; cat "$AGENTS_DIR/snippets/AGENTS.memory.md"; } >> "$dir/AGENTS.md"
    fi
  else
    cp "$AGENTS_DIR/snippets/AGENTS.memory.md" "$dir/AGENTS.md"
  fi
  log "  skills + AGENTS.md"
}

install_claude() {
  local dir="${CLAUDE_HOME:-$HOME/.claude}"
  mkdir -p "$dir/skills"
  log "[claude] $dir"
  install_skills_into "$dir/skills"
  # hooks merge note
  cp -f "$AGENTS_DIR/hooks/claude/settings.hooks.example.json" "$dir/nebula-hooks.example.json"
  log "  skills + hooks example → nebula-hooks.example.json (merge into settings.json)"
}

install_cursor() {
  local dir="${CURSOR_HOME:-$HOME/.cursor}"
  mkdir -p "$dir"
  log "[cursor] $dir"
  # project-local is more common; still drop global skills if folder exists
  install_skills_into "$dir/skills"
  cp -f "$AGENTS_DIR/hooks/cursor/hooks.example.json" "$dir/nebula-hooks.example.json"
  log "  skills + hooks example"
}

install_hermes() {
  local dir="${HERMES_HOME:-$HOME/.hermes}"
  mkdir -p "$dir"
  log "[hermes] $dir"
  install_skills_into "$dir/skills"
  cp -f "$AGENTS_DIR/snippets/AGENTS.memory.md" "$dir/AGENTS.nebula.md"
  log "  skills + AGENTS.nebula.md"
}

install_opencode() {
  local dir="${OPENCODE_HOME:-$HOME/.config/opencode}"
  mkdir -p "$dir/plugins" "$dir/skills"
  log "[opencode] $dir"
  if [ -f "$AGENTS_DIR/integrations/opencode/plugins/nebula-memory.js" ]; then
    cp -f "$AGENTS_DIR/integrations/opencode/plugins/nebula-memory.js" "$dir/plugins/"
  fi
  install_skills_into "$dir/skills"
  if [ -f "$AGENTS_DIR/integrations/opencode/AGENTS.snippet.md" ]; then
    cp -f "$AGENTS_DIR/integrations/opencode/AGENTS.snippet.md" "$dir/AGENTS.nebula.md"
  fi
  log "  plugin + skills"
}

install_reasonix() {
  # Reasonix config dir varies; write into NEBULA_HOME and document
  local dir="$NEBULA_HOME/reasonix"
  mkdir -p "$dir"
  log "[reasonix] templates → $dir"
  cp -f "$AGENTS_DIR/hooks/reasonix/AGENTS.memory.md" "$dir/"
  cp -f "$AGENTS_DIR/snippets/AGENTS.memory.md" "$dir/AGENTS.full.md"
  install_skills_into "$dir/skills"
  cat > "$dir/INSTALL.txt" <<EOF
1) 将 AGENTS.memory.md 合并到 Reasonix 项目 AGENTS.md
   或 config.toml: [agent] system_prompt_file = "$dir/AGENTS.memory.md"
2) 可选拷贝 skills 到 Reasonix 支持的 skill 目录
3) export NEBULA_BASE_URL=$BASE_URL
EOF
  log "  see $dir/INSTALL.txt"
}

install_openclaw() {
  local dir="${OPENCLAW_WORKSPACE:-}"
  if [ -z "$dir" ]; then
    for c in "$HOME/.openclaw/workspace" "/root/.openclaw/workspace"; do
      [ -d "$c" ] && dir="$c" && break
    done
  fi
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    log "[openclaw] skip (set OPENCLAW_WORKSPACE=...)"
    return 0
  fi
  log "[openclaw] $dir"
  install_skills_into "$dir/skills"
  if [ -f "$dir/AGENTS.md" ]; then
    if ! grep -q "Nebula L2" "$dir/AGENTS.md" 2>/dev/null; then
      { echo ""; cat "$AGENTS_DIR/snippets/AGENTS.memory.md"; } >> "$dir/AGENTS.md"
    fi
  else
    cp "$AGENTS_DIR/snippets/AGENTS.memory.md" "$dir/AGENTS.md"
  fi
}

list_targets() {
  cat <<EOF
Supported targets:
  grok codex claude cursor hermes opencode reasonix openclaw all
Env:
  NEBULA_BASE_URL  NEBULA_HOME  GROK_HOME  CODEX_HOME  HERMES_HOME
  OPENCODE_HOME  OPENCLAW_WORKSPACE
EOF
}

main() {
  if [ "${1:-}" = "--list" ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    list_targets; exit 0
  fi
  local targets=("$@")
  if [ ${#targets[@]} -eq 0 ] || [ "${targets[0]}" = "all" ]; then
    targets=(grok codex claude cursor hermes opencode reasonix openclaw)
  fi
  log "== nebula agents install =="
  log "repo=$ROOT base=$BASE_URL"
  install_common_hook
  for t in "${targets[@]}"; do
    case "$t" in
      grok) install_grok ;;
      codex) install_codex ;;
      claude) install_claude ;;
      cursor) install_cursor ;;
      hermes) install_hermes ;;
      opencode) install_opencode ;;
      reasonix) install_reasonix ;;
      openclaw) install_openclaw ;;
      all) ;;
      *) log "unknown target: $t"; list_targets; exit 1 ;;
    esac
  done
  log "== done =="
  log "export NEBULA_BASE_URL=$BASE_URL"
  log "restart agents to load hooks/skills"
}

main "$@"
