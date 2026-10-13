# ZHL - regenerate all diagrams used by the implementation design doc
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\render-diagrams.ps1
#
# Source: docs\diagrams-src\*.puml   (PlantUML)
# Output: docs\images\*.png
#
# Workflow: edit a .puml -> run this script -> Markdown references stay unchanged.
# NOTE: this file is intentionally ASCII-only so it parses under PowerShell 5.1
#       regardless of the console code page.

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$PlantUmlJar = "D:\AAA\tools\plantuml\plantuml.jar"
$srcDir = "docs\diagrams-src"
$outDir = "docs\images"

if (-not (Test-Path $PlantUmlJar)) {
    Write-Host "!! PlantUML jar not found: $PlantUmlJar" -ForegroundColor Red
    Write-Host "   Edit the PlantUmlJar variable in this script." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $srcDir)) {
    Write-Host "!! Source dir not found: $srcDir" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir | Out-Null
}

Write-Host "==> Rendering PlantUML ..." -ForegroundColor Cyan
$files = @(Get-ChildItem -Path $srcDir -Filter "*.puml")
if ($files.Count -eq 0) {
    Write-Host "!! No .puml file under $srcDir" -ForegroundColor Red
    exit 1
}

# PlantUML writes PNG next to each .puml; we move them afterwards.
$renderLog = java -jar $PlantUmlJar -charset UTF-8 -tpng $files.FullName 2>&1
$renderLog | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }

if ($LASTEXITCODE -ne 0) {
    Write-Host "!! PlantUML failed (exit $LASTEXITCODE). See errors above." -ForegroundColor Red
    exit 1
}

$moved = 0
foreach ($png in @(Get-ChildItem -Path $srcDir -Filter "*.png")) {
    Move-Item $png.FullName (Join-Path $outDir $png.Name) -Force
    $moved = $moved + 1
}

Write-Host ""
Write-Host "==> Done: rendered $($files.Count) sources, moved $moved PNG files." -ForegroundColor Green
Write-Host "    Output dir: $outDir" -ForegroundColor Green
Get-ChildItem -Path $outDir -Filter "*.png" | ForEach-Object {
    Write-Host ("      {0,-26} {1,7:N0} B" -f $_.Name, $_.Length) -ForegroundColor DarkGray
}
