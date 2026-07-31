$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $PSScriptRoot
$Python = 'C:\Users\55055\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PATH = 'E:\postgresql\bin;' + $env:PATH
Set-Location $Project
& $Python main.py web --host 127.0.0.1 --port 8765
