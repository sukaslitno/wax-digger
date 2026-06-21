# build_windows.ps1 — build a portable WaxDigger.exe (single file).
# Run on Windows with: pip install pyinstaller pillow
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")   # repo root

python packaging\fetch_binaries.py
python packaging\make_icons.py

if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }

# bundle whatever binaries fetch_binaries produced
$binArgs = @()
Get-ChildItem packaging\bin | ForEach-Object {
  $binArgs += "--add-binary"
  $binArgs += "$($_.FullName);bin"
}

pyinstaller --noconfirm --onefile --windowed --name "WaxDigger" `
  --icon packaging\icon.ico `
  --add-data "ytdlp-gui\templates;templates" `
  @binArgs `
  ytdlp-gui\app.py

Write-Host "OK -> dist\WaxDigger.exe"
