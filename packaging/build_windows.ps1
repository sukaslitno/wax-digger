# build_windows.ps1 — build a portable WaxDigger.exe (single file).
# Run on Windows with: pip install pyinstaller pillow flask pywebview
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"          # avoid cp1252 crashes on unicode output
$env:PYTHONIOENCODING = "utf-8"
Set-Location (Join-Path $PSScriptRoot "..")   # repo root

function Invoke-Checked {
  param([scriptblock]$Cmd)
  & $Cmd
  if ($LASTEXITCODE -ne 0) { throw "command failed (exit $LASTEXITCODE): $Cmd" }
}

Invoke-Checked { python packaging\fetch_binaries.py }
Invoke-Checked { python packaging\make_icons.py }

if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }

# bundle whatever binaries fetch_binaries produced
$binArgs = @()
Get-ChildItem packaging\bin | ForEach-Object {
  $binArgs += "--add-binary"
  $binArgs += "$($_.FullName);bin"
}
if ($binArgs.Count -eq 0) { throw "packaging\bin is empty — fetch_binaries.py produced nothing" }

Invoke-Checked {
  pyinstaller --noconfirm --onefile --windowed --name "WaxDigger" `
    --icon packaging\icon.ico `
    --add-data "ytdlp-gui\templates;templates" `
    --collect-all webview `
    @binArgs `
    ytdlp-gui\app.py
}

Write-Host "OK -> dist\WaxDigger.exe"
