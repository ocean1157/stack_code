$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $PSScriptRoot
$Python = 'C:\Users\55055\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PATH = 'E:\postgresql\bin;' + $env:PATH
Set-Location $Project
try { & $Python main.py refresh } catch { Add-Content -LiteralPath "$Project\data\refresh_errors.log" -Value "$(Get-Date -Format o) $_"; throw }
