# Nebula SessionStart bootstrap (PowerShell, portable)
# Fail-open; budgeted; env-driven. No private host inventory.
$ErrorActionPreference = "SilentlyContinue"
$sw = [System.Diagnostics.Stopwatch]::StartNew()

$env:NO_PROXY = ($env:NO_PROXY, "localhost,127.0.0.1,::1,.localhost" | Where-Object { $_ }) -join ","
$env:no_proxy = $env:NO_PROXY

$Base = if ($env:NEBULA_BASE_URL) { $env:NEBULA_BASE_URL.TrimEnd("/") } else { "http://127.0.0.1:26670" }
$Budget = if ($env:NEBULA_BOOTSTRAP_BUDGET) { $env:NEBULA_BOOTSTRAP_BUDGET } else { "800" }
$Focus = if ($env:NEBULA_BOOTSTRAP_FOCUS) { $env:NEBULA_BOOTSTRAP_FOCUS } else { "session-start" }
$TimeoutSec = if ($env:NEBULA_HOOK_TIMEOUT_S) { [int]$env:NEBULA_HOOK_TIMEOUT_S } else { 8 }

Write-Output "[nebula-hook] gates: workflow|ponytail|recall|verify|correction | L2 bootstrap budget=$Budget"
Write-Output "[nebula-hook/sop] ask→contract | readback authority | supersede on correction | no-secret-in-L2"

$bootOk = $false

# optional CLI
if ($env:NEBULA_CLI) {
  try {
    $parts = $env:NEBULA_CLI.Split(" ", [System.StringSplitOptions]::RemoveEmptyEntries)
    $out = & $parts[0] @($parts[1..($parts.Length-1)] + @("bootstrap", $Focus, "--budget", $Budget)) 2>$null | Out-String
    if ($out) {
      $snip = $out.Trim()
      if ($snip.Length -gt 700) { $snip = $snip.Substring(0,700) + "..." }
      Write-Output "[nebula-hook/cli] $($sw.ElapsedMilliseconds)ms | $snip"
      $bootOk = $true
    }
  } catch {}
}

if (-not $bootOk) {
  try {
    $body = @{ focus = $Focus; budget = [int]$Budget; budget_chars = [int]$Budget } | ConvertTo-Json -Compress
    $r = Invoke-WebRequest -Method POST -Uri "$Base/v5/bootstrap" `
      -ContentType "application/json; charset=utf-8" `
      -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) `
      -TimeoutSec $TimeoutSec -NoProxy
    $snip = $r.Content.Trim()
    if ($snip.Length -gt 700) { $snip = $snip.Substring(0,700) + "..." }
    Write-Output "[nebula-hook/http] $($sw.ElapsedMilliseconds)ms | $snip"
    $bootOk = $true
  } catch {
    Write-Output "[nebula-hook/http] $($sw.ElapsedMilliseconds)ms unavailable → degrade (L1 notes still ok)"
  }
}

if (-not $bootOk) {
  Write-Output "[nebula-hook] bootstrap skipped; still enforce recall-before-code · verify-before-assert · correction-capture"
}
exit 0
