param(
    [string]$WheelPath = ""
)

$ErrorActionPreference = "Stop"

function Get-FolioSortDistro {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw "WSL is not installed. Run 'wsl --install -d Ubuntu' in an elevated PowerShell window, restart Windows, and run this installer again."
    }
    $distros = @(& wsl.exe --list --quiet 2>$null) |
        ForEach-Object { ($_ -replace "`0", "").Trim() } |
        Where-Object { $_ }
    $ubuntu = $distros | Where-Object { $_ -match "^Ubuntu" } | Select-Object -First 1
    if (-not $ubuntu) {
        throw "An Ubuntu WSL distribution is required. Run 'wsl --install -d Ubuntu', restart Windows, and run this installer again."
    }
    return $ubuntu
}

if (-not $WheelPath) {
    $wheel = Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot "payload") -Filter "foliosort-*.whl" |
        Select-Object -First 1
    if (-not $wheel) { throw "The FolioSort wheel is missing from the installer payload." }
    $WheelPath = $wheel.FullName
}

$WheelPath = (Resolve-Path -LiteralPath $WheelPath).Path
$distro = Get-FolioSortDistro
$wslWheel = (& wsl.exe -d $distro -- wslpath -a $WheelPath).Trim()
if ($LASTEXITCODE -ne 0 -or -not $wslWheel) {
    throw "Could not translate the installer payload path for WSL."
}

$installScript = @'
set -Eeuo pipefail
wheel="$1"
install_root="$HOME/.local/share/foliosort"
venv="$install_root/venv"
workspace="${FOLIOSORT_WORKSPACE:-$HOME/desktop/review}"

command -v python3 >/dev/null 2>&1 || {
  echo "ERROR: Python 3 is missing in Ubuntu. Run: sudo apt update && sudo apt install python3 python3-venv python3-pip" >&2
  exit 20
}

mkdir -p "$install_root"
if [[ ! -x "$venv/bin/python" ]]; then
  if ! python3 -m venv "$venv"; then
    echo "ERROR: Python venv support is missing. Run: sudo apt update && sudo apt install python3-venv" >&2
    exit 21
  fi
fi

"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install --upgrade "$wheel"
"$venv/bin/foliosort" init "$workspace" --force --no-openalex-prompt
"$venv/bin/foliosort" --version
printf '%s\n' "$workspace" > "$install_root/workspace-path"
'@

Write-Host "Installing FolioSort in WSL distribution: $distro"
& wsl.exe -d $distro -- bash -lc $installScript bash $wslWheel
if ($LASTEXITCODE -ne 0) {
    throw "FolioSort installation in WSL failed with exit code $LASTEXITCODE. See the message above for the missing prerequisite."
}

Write-Host "FolioSort was installed successfully."
Write-Host "Before analysis, configure OpenAlex, Docker/GROBID, and the local Qwen server as described in the README."
