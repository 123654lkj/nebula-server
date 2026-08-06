# 一键安装 skills/hooks 到 Windows 上的 Grok / Codex / Reasonix 等
# 用法: pwsh -File agents/install-agents.ps1 [-Targets grok,codex,reasonix]
param(
  [string[]]$Targets = @("grok", "codex", "reasonix", "claude", "cursor")
)
$ErrorActionPreference = "Stop"
$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (Test-Path (Join-Path $PSScriptRoot "skills")) {
  $AgentsDir = $PSScriptRoot
  $Root = Split-Path $PSScriptRoot -Parent
} else {
  $AgentsDir = Join-Path $Root "agents"
}
$SkillsSrc = Join-Path $AgentsDir "skills"
$HookPs1 = Join-Path $AgentsDir "hooks/common/session-start-memory.ps1"
$Snippet = Join-Path $AgentsDir "snippets/AGENTS.memory.md"
$NebulaHome = if ($env:NEBULA_HOME) { $env:NEBULA_HOME } else { Join-Path $env:USERPROFILE ".nebula" }
$BaseUrl = if ($env:NEBULA_BASE_URL) { $env:NEBULA_BASE_URL } else { "http://127.0.0.1:26670" }
$SkillNames = @("workflow-discipline","ponytail","recall-before-code","verify-before-assert","correction-capture","nebula-recall")

function Install-Skills([string]$Dest) {
  New-Item -ItemType Directory -Force -Path $Dest | Out-Null
  foreach ($s in $SkillNames) {
    $src = Join-Path $SkillsSrc "$s/SKILL.md"
    if (Test-Path $src) {
      $d = Join-Path $Dest $s
      New-Item -ItemType Directory -Force -Path $d | Out-Null
      Copy-Item $src (Join-Path $d "SKILL.md") -Force
      Write-Host "  skill → $d"
    }
  }
}

function Install-CommonHook {
  $h = Join-Path $NebulaHome "hooks"
  New-Item -ItemType Directory -Force -Path $h | Out-Null
  Copy-Item $HookPs1 (Join-Path $h "session-start-memory.ps1") -Force
  @"
`$env:NEBULA_BASE_URL = '$BaseUrl'
`$env:NEBULA_BOOTSTRAP_BUDGET = '800'
"@ | Set-Content (Join-Path $NebulaHome "env.ps1") -Encoding UTF8
  Write-Host "  common hook → $h"
}

function Merge-AgentsMd([string]$Path) {
  $frag = Get-Content $Snippet -Raw -Encoding UTF8
  if (Test-Path $Path) {
    $cur = Get-Content $Path -Raw -Encoding UTF8
    if ($cur -notmatch "Nebula L2") {
      Add-Content -Path $Path -Value "`n$frag" -Encoding UTF8
      Write-Host "  appended AGENTS.memory → $Path"
    }
  } else {
    Set-Content -Path $Path -Value $frag -Encoding UTF8
    Write-Host "  wrote $Path"
  }
}

Write-Host "== nebula agents install (Windows) =="
Write-Host "root=$Root base=$BaseUrl"
Install-CommonHook
$hookCmd = "pwsh -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $NebulaHome 'hooks/session-start-memory.ps1')`""

foreach ($t in $Targets) {
  switch ($t.ToLower()) {
    "grok" {
      $dir = Join-Path $env:USERPROFILE ".grok"
      Write-Host "[grok] $dir"
      Install-Skills (Join-Path $dir "skills")
      $hd = Join-Path $dir "hooks"
      New-Item -ItemType Directory -Force -Path $hd | Out-Null
      $json = @{
        hooks = @{
          SessionStart = @(
            @{ hooks = @(@{ type = "command"; timeout = 12; command = $hookCmd }) }
          )
        }
      } | ConvertTo-Json -Depth 8
      Set-Content (Join-Path $hd "session-memory.json") $json -Encoding UTF8
      Merge-AgentsMd (Join-Path $dir "AGENTS.md")
    }
    "codex" {
      $dir = Join-Path $env:USERPROFILE ".codex"
      Write-Host "[codex] $dir"
      Install-Skills (Join-Path $dir "skills")
      Merge-AgentsMd (Join-Path $dir "AGENTS.md")
    }
    "claude" {
      $dir = Join-Path $env:USERPROFILE ".claude"
      Write-Host "[claude] $dir"
      Install-Skills (Join-Path $dir "skills")
    }
    "cursor" {
      $dir = Join-Path $env:USERPROFILE ".cursor"
      Write-Host "[cursor] $dir"
      Install-Skills (Join-Path $dir "skills")
    }
    "reasonix" {
      $dir = Join-Path $NebulaHome "reasonix"
      Write-Host "[reasonix] $dir"
      New-Item -ItemType Directory -Force -Path $dir | Out-Null
      Install-Skills (Join-Path $dir "skills")
      Copy-Item (Join-Path $AgentsDir "hooks/reasonix/AGENTS.memory.md") $dir -Force
      Copy-Item $Snippet (Join-Path $dir "AGENTS.full.md") -Force
      $rx = Join-Path $env:APPDATA "Reasonix"
      if (Test-Path $rx) {
        Merge-AgentsMd (Join-Path $rx "AGENTS.nebula.md")
        Write-Host "  hint: set Reasonix system_prompt_file to AGENTS.nebula.md or merge into project AGENTS.md"
      }
    }
    default { Write-Host "skip unknown: $t" }
  }
}
Write-Host "== done =="
Write-Host "Set user env NEBULA_BASE_URL=$BaseUrl and restart agents."
