# Open the desktop app's agent view for this repo (issue #50, requirement 5): finds claude.exe
# and runs `claude agents` from the repo root, so ORCH's background sessions show up.
#
#   tools\agentops\agents.cmd
#   tools\agentops\agents.ps1
#
# CLAUDE_EXE overrides the discovered path (same override `tools/agentops/dispatch.py` reads).
# The desktop app is an MSIX package: `%APPDATA%\Claude\claude-code\<ver>\claude.exe` only
# resolves for a process running INSIDE the app's own container; a plain terminal (this script)
# gets "not recognized" there. The real file for everyone else is under the package's LocalCache:
# `%LOCALAPPDATA%\Packages\Claude_<publisher>\LocalCache\Roaming\Claude\claude-code\<ver>\
# claude.exe` (the publisher id is installation-specific, so this globs `Claude_*`). Order,
# same as `find_claude_exe` in dispatch.py: CLAUDE_EXE -> newest under the package's LocalCache ->
# newest under %APPDATA% -> `claude` on PATH.

$ErrorActionPreference = "Stop"

function Get-NewestVersionExe([string]$base) {
    if (-not (Test-Path $base)) { return $null }
    $versions = Get-ChildItem -LiteralPath $base -Directory |
        Where-Object { $_.Name -match '^\d+(\.\d+)*$' }
    if (-not $versions -or $versions.Count -eq 0) { return $null }
    $sorted = $versions | Sort-Object { [version]("$($_.Name)" -replace '^(\d+(\.\d+){0,3}).*$', '$1') } -Descending
    foreach ($version in $sorted) {
        # flat (`<ver>\claude.exe`) before 2026-10-02, one level deeper (`<ver>\<hash>\claude.exe`) since;
        # the same rule as `_newest_version_exe` in dispatch.py (the newest hash dir by write time wins)
        $flat = Join-Path $version.FullName "claude.exe"
        if (Test-Path -LiteralPath $flat -PathType Leaf) { return $flat }
        $deeper = Get-ChildItem -LiteralPath $version.FullName -Directory |
            ForEach-Object { Join-Path $_.FullName "claude.exe" } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            Sort-Object { (Get-Item -LiteralPath $_).LastWriteTime } -Descending
        if ($deeper) { return ($deeper | Select-Object -First 1) }
    }
    return $null
}

$exe = $env:CLAUDE_EXE

if (-not $exe -and $env:LOCALAPPDATA) {
    $packagesRoot = Join-Path $env:LOCALAPPDATA "Packages"
    if (Test-Path $packagesRoot) {
        $packageDirs = Get-ChildItem -LiteralPath $packagesRoot -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue
        foreach ($pkg in $packageDirs) {
            $base = Join-Path $pkg.FullName "LocalCache\Roaming\Claude\claude-code"
            $candidate = Get-NewestVersionExe $base
            if ($candidate) { $exe = $candidate; break }
        }
    }
}

if (-not $exe -and $env:APPDATA) {
    $base = Join-Path $env:APPDATA "Claude\claude-code"
    $exe = Get-NewestVersionExe $base
}

if (-not $exe) {
    $onPath = Get-Command claude -ErrorAction SilentlyContinue
    if ($onPath) { $exe = $onPath.Source }
}

if (-not $exe) {
    Write-Error "no claude.exe found: set CLAUDE_EXE, or check %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code and %APPDATA%\Claude\claude-code"
    exit 1
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    & $exe agents
} finally {
    Pop-Location
}
