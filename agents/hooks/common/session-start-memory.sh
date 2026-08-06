#!/usr/bin/env bash
# Nebula / L2 SessionStart bootstrap (portable reference)
# - Fail-open: never block the session
# - No hard-coded lab inventory; configure via env
# - Budgeted bootstrap only (not full dump)
set +e

START_MS=$(date +%s%3N 2>/dev/null || echo 0)

NEBULA_BASE_URL="${NEBULA_BASE_URL:-http://127.0.0.1:26670}"
NEBULA_BOOTSTRAP_PATH="${NEBULA_BOOTSTRAP_PATH:-/v5/bootstrap}"
NEBULA_BOOTSTRAP_BUDGET="${NEBULA_BOOTSTRAP_BUDGET:-800}"
NEBULA_BOOTSTRAP_FOCUS="${NEBULA_BOOTSTRAP_FOCUS:-session-start}"
NEBULA_HOOK_TIMEOUT_S="${NEBULA_HOOK_TIMEOUT_S:-8}"
NEBULA_NO_PROXY="${NEBULA_NO_PROXY:-localhost,127.0.0.1,::1,.localhost}"

# Optional: keep local L2 off system proxy
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$NEBULA_NO_PROXY"
export no_proxy="$NO_PROXY"

elapsed() {
  local now
  now=$(date +%s%3N 2>/dev/null || echo 0)
  if [[ "$START_MS" =~ ^[0-9]+$ && "$now" =~ ^[0-9]+$ && "$START_MS" -gt 0 ]]; then
    echo $((now - START_MS))
  else
    echo "?"
  fi
}

snip() {
  local s="$1"
  s=$(printf '%s' "$s" | tr '\n' ' ' | sed 's/  */ /g')
  if [ "${#s}" -gt 700 ]; then
    printf '%s...' "${s:0:700}"
  else
    printf '%s' "$s"
  fi
}

echo "[nebula-hook] gates: workflow|ponytail|recall|verify|correction | L2 bootstrap budget=${NEBULA_BOOTSTRAP_BUDGET}"

boot_ok=0

# 1) Optional external CLI (e.g. rxt mem / nebula-memory wrapper)
if [ -n "${NEBULA_CLI:-}" ]; then
  # Expected: CLI prints bootstrap text or JSON to stdout
  out=$(timeout "${NEBULA_HOOK_TIMEOUT_S}s" \
    env NEBULA_BASE_URL="$NEBULA_BASE_URL" \
    $NEBULA_CLI bootstrap "$NEBULA_BOOTSTRAP_FOCUS" --budget "$NEBULA_BOOTSTRAP_BUDGET" 2>/dev/null)
  if [ -n "$out" ]; then
    echo "[nebula-hook/cli] $(elapsed)ms | $(snip "$out")"
    boot_ok=1
  else
    echo "[nebula-hook/cli] $(elapsed)ms empty/timeout → HTTP fallback"
  fi
elif command -v nebula-memory >/dev/null 2>&1; then
  # Reference package CLI (process-local demo unless you wire a remote backend)
  out=$(timeout "${NEBULA_HOOK_TIMEOUT_S}s" \
    nebula-memory bootstrap "$NEBULA_BOOTSTRAP_FOCUS" --budget "$NEBULA_BOOTSTRAP_BUDGET" 2>/dev/null)
  if [ -n "$out" ]; then
    echo "[nebula-hook/nebula-memory] $(elapsed)ms | $(snip "$out")"
    boot_ok=1
  fi
fi

# 2) HTTP bootstrap
if [ "$boot_ok" -eq 0 ] && command -v curl >/dev/null 2>&1; then
  url="${NEBULA_BASE_URL%/}${NEBULA_BOOTSTRAP_PATH}"
  # Support both budget and budget_chars field names
  payload=$(printf '{"focus":"%s","budget":%s,"budget_chars":%s}' \
    "$NEBULA_BOOTSTRAP_FOCUS" "$NEBULA_BOOTSTRAP_BUDGET" "$NEBULA_BOOTSTRAP_BUDGET")
  out=$(timeout "${NEBULA_HOOK_TIMEOUT_S}s" curl -sS --noproxy '*' \
    -H 'Content-Type: application/json; charset=utf-8' \
    -d "$payload" \
    "$url" 2>/dev/null)
  if [ -n "$out" ]; then
    echo "[nebula-hook/http] $(elapsed)ms | $(snip "$out")"
    boot_ok=1
  else
    echo "[nebula-hook/http] $(elapsed)ms unavailable → degrade (L1 notes / L5 code still ok)"
  fi
fi

if [ "$boot_ok" -eq 0 ]; then
  echo "[nebula-hook] bootstrap skipped; still enforce: recall-before-code · verify-before-assert · no-secret-in-L2"
fi

echo "[nebula-hook/sop] pack before multi-list | ask→contract only | readback authority | supersede on correction"
exit 0
