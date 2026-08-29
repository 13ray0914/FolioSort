param([string]$Launcher,[string]$WorkingDir)
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Review Literature App.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($shortcutPath)
$s.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
$s.Arguments = '"' + $Launcher + '"'
$s.WorkingDirectory = $WorkingDir
$s.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',220'
$s.Description = 'Local Literature Review Pipeline'
$s.Save()
Write-Host "Created: $shortcutPath"
