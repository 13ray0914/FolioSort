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
