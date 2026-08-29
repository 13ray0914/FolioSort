#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${REVIEW_ROOT:-$HOME/desktop/review}"
WIN_DIR="$ROOT/windows"
mkdir -p "$WIN_DIR"

command -v powershell.exe >/dev/null 2>&1 || { echo "ERROR: powershell.exe is not available from WSL."; exit 2; }
command -v wslpath >/dev/null 2>&1 || { echo "ERROR: wslpath is unavailable."; exit 2; }

# A VBS launcher keeps the WSL console hidden. The server itself opens the browser only after it is ready.
VBS="$WIN_DIR/launch_foliosort.vbs"
ROOT_ESC=${ROOT//\"/\"\"}
cat > "$VBS" <<EOF
Set shell = CreateObject("WScript.Shell")
cmd = "wsl.exe -e bash -lc ""cd '$ROOT_ESC' && ./scripts/start_review_app.sh"""
shell.Run cmd, 0, False
EOF

WIN_VBS=$(wslpath -w "$VBS")
WIN_ROOT=$(wslpath -w "$ROOT")
PS1="$WIN_DIR/create_foliosort_shortcut.ps1"
cat > "$PS1" <<'PS'
param([string]$Launcher,[string]$WorkingDir)
$desktop = [Environment]::GetFolderPath('Desktop')
$oldShortcutPath = Join-Path $desktop 'Review Literature App.lnk'
if (Test-Path $oldShortcutPath) { Remove-Item $oldShortcutPath -Force -ErrorAction SilentlyContinue }
$shortcutPath = Join-Path $desktop 'FolioSort.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($shortcutPath)
$s.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
$s.Arguments = '"' + $Launcher + '"'
$s.WorkingDirectory = $WorkingDir
$s.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',220'
$s.Description = 'FolioSort local literature workspace'
$s.Save()
Write-Host "Created: $shortcutPath"
PS
WIN_PS1=$(wslpath -w "$PS1")
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$WIN_PS1" -Launcher "$WIN_VBS" -WorkingDir "$WIN_ROOT"

echo
echo "Windows launcher installed."
echo "Double-click 'FolioSort' on the Windows Desktop."
echo "Ubuntu does not need to be opened manually; WSL starts hidden in the background."
