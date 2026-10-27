<#
.SYNOPSIS
  Start the Guard Playground on http://127.0.0.1:3010/guard without Docker (Requirement 16.8, D13).

.DESCRIPTION
  Default: Replay mode only, no model API is ever called and no key is needed.
  -Live   : also enables Live mode. API keys are read from the environment or from the repo's
            .env (BENCH_OPENAI_API_KEY / OPENAI_API_KEY, BENCH_OPENROUTER_API_KEY). Key values
            are never printed. Each Live run calls a paid model API.
  The service listens on 127.0.0.1 only: it has no login (ADR-0009).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\playground\run_playground.ps1
  powershell -ExecutionPolicy Bypass -File scripts\playground\run_playground.ps1 -Live
#>
param(
    [switch]$Live,
    [switch]$ReplayOnly,
    [int]$Port = 3010
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python environment not found: $python (run: uv sync)" }

$env:PYTHONPATH = "$repo;$(Join-Path $repo 'agentmailguard')"
$env:PLAYGROUND__REPO_ROOT = $repo
$env:PLAYGROUND__PORT = "$Port"

if ($Live -and -not $ReplayOnly) {
    $env:PLAYGROUND__LIVE_ENABLED = "true"
    $dotenv = Join-Path $repo ".env"
    $wanted = @("BENCH_OPENAI_API_KEY", "OPENAI_API_KEY", "BENCH_OPENROUTER_API_KEY")
    if (Test-Path $dotenv) {
        foreach ($line in Get-Content $dotenv) {
            if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*)$' -and $wanted -contains $Matches[1]) {
                $name = $Matches[1]
                $value = $Matches[2].Trim().Trim('"').Trim("'")
                if ($value -and -not [Environment]::GetEnvironmentVariable($name)) {
                    [Environment]::SetEnvironmentVariable($name, $value, "Process")
                }
            }
        }
    }
    foreach ($name in $wanted) {
        $state = if ([Environment]::GetEnvironmentVariable($name)) { "set" } else { "not set" }
        Write-Host ("  {0,-26} {1}" -f $name, $state)
    }
    Write-Host "Live mode ON: every Live run calls a paid model API." -ForegroundColor Yellow
} else {
    $env:PLAYGROUND__LIVE_ENABLED = "false"
    Write-Host "Replay mode only (start with -Live to enable Live mode)."
}

$server = Start-Process -FilePath $python -PassThru -NoNewWindow -ArgumentList @(
    "-m", "uvicorn", "services.guard_playground.main:app", "--host", "127.0.0.1", "--port", "$Port"
)
$url = "http://127.0.0.1:$Port"
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    try {
        $ready = Invoke-RestMethod -Uri "$url/readyz" -TimeoutSec 2
        Write-Host ""
        Write-Host "Guard Playground is ready: $url/guard" -ForegroundColor Green
        Write-Host ("  cases: {0}, replay entries: {1}, live: {2}" -f `
            $ready.case_count, $ready.replay_entry_count, $ready.live_enabled)
        Write-Host "  Press Ctrl+C to stop."
        Wait-Process -Id $server.Id
        exit 0
    } catch {
        if ($server.HasExited) { throw "The Playground stopped while starting (see the output above)." }
    }
}
throw "The Playground did not answer $url/readyz within 60 seconds."
