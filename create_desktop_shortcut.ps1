$desktop = [Environment]::GetFolderPath('Desktop')
$projDir = $PSScriptRoot
if (-not $projDir) { $projDir = (Get-Location).Path }
$vbsPath = Join-Path $projDir 'Launch_Journal_Guard_Silent.vbs'
$iconPath = Join-Path $projDir 'static\images\app_icon.ico'

$WshShell = New-Object -ComObject WScript.Shell

# Shortcut on User Desktop
$shortcutPath = Join-Path $desktop 'Journal Guard.lnk'
$shortcut = $WshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = 'wscript.exe'
$shortcut.Arguments = '"' + $vbsPath + '"'
$shortcut.WorkingDirectory = $projDir
$shortcut.IconLocation = $iconPath
$shortcut.Description = 'Journal Guard — Manuscript & Artwork QC Platform'
$shortcut.Save()

Write-Host "Desktop shortcut created successfully at: $shortcutPath"
