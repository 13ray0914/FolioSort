$ErrorActionPreference = "Stop"

function Get-FolioSortDistro {
    $distros = @(& wsl.exe --list --quiet 2>$null) |
        ForEach-Object { ($_ -replace "`0", "").Trim() } |
        Where-Object { $_ }
    return ($distros | Where-Object { $_ -match "^Ubuntu" } | Select-Object -First 1)
}

try {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw "WSL is not installed. Run: wsl --install -d Ubuntu"
    }
    $distro = Get-FolioSortDistro
    if (-not $distro) { throw "Ubuntu is not installed in WSL. Run: wsl --install -d Ubuntu" }

    $launchScript = @'
set -Eeuo pipefail
install_root="$HOME/.local/share/foliosort"
venv="$install_root/venv"
workspace="${FOLIOSORT_WORKSPACE:-$HOME/desktop/review}"
[[ -x "$venv/bin/python" ]] || { echo "FolioSort is not installed in this Ubuntu distribution." >&2; exit 30; }
[[ -f "$workspace/scripts/start_review_app.sh" ]] || { echo "FolioSort workspace is missing: $workspace" >&2; exit 31; }
export REVIEW_ROOT="$workspace"
export REVIEW_PYTHON="$venv/bin/python"
cd "$workspace"
bash scripts/start_review_app.sh
'@

    & wsl.exe -d $distro -- bash -lc $launchScript
    if ($LASTEXITCODE -ne 0) { throw "FolioSort could not be started (exit code $LASTEXITCODE)." }
}
catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, "FolioSort", "OK", "Error") | Out-Null
    exit 1
}
