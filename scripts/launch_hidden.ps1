# Launch a long-running command DETACHED (through WMI, so it survives the end of the Claude Code session) and with NO
# console window (the user, 2026-09-19: "a pile of cmd windows" from GPT workers). Prints "rc=<0 ok> pid=<pid>".
#
#   powershell -File scripts/launch_hidden.ps1 -CommandLine "C:\...\app.exe -m tools.agentops dispatch"
#
# The dispatcher (`python -m tools.agentops dispatch`, #46/#50) polls its own sessions and subagents, so no
# separate Claude Code watcher task is needed any more; the monitor (`python -m tools.agentops monitor`) shows
# what is running.
param(
    [Parameter(Mandatory = $true)][string]$CommandLine,
    [string]$WorkDir = (Resolve-Path (Join-Path $PSScriptRoot ".."))
)
$startup = New-CimInstance -ClassName Win32_ProcessStartup -Property @{ ShowWindow = [uint16]0 } -ClientOnly
$full = "cmd /c cd /d $WorkDir && $CommandLine"
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $full; CurrentDirectory = $WorkDir; ProcessStartupInformation = $startup }
"rc=$($r.ReturnValue) pid=$($r.ProcessId)"
