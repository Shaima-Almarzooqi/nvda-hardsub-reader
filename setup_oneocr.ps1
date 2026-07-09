# HardSub Reader: OneOCR engine setup
# Copies the OneOCR engine files from the Windows 11 Snipping Tool into
# the folder where the add-on expects them. The Snipping Tool ships
# engine binaries matching your machine, so no configuration is needed.
# Run as administrator:  powershell -ExecutionPolicy Bypass -File setup_oneocr.ps1

$ErrorActionPreference = "Stop"

Write-Host "HardSub Reader - OneOCR engine setup"

$pkg = Get-AppxPackage Microsoft.ScreenSketch
if (-not $pkg) {
    Write-Host "ERROR: Snipping Tool is not installed. Install it from the Microsoft Store, or the add-on will use the lower-accuracy legacy engine."
    exit 1
}
$loc = $pkg.InstallLocation
Write-Host "Snipping Tool found at: $loc"

$candidates = @("$loc\SnippingTool", "$loc")
$srcDir = $null
foreach ($c in $candidates) {
    if (Test-Path "$c\oneocr.dll") { $srcDir = $c; break }
}
if (-not $srcDir) {
    $found = Get-ChildItem $loc -Recurse -Filter oneocr.dll -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $srcDir = $found.DirectoryName }
}
if (-not $srcDir) {
    Write-Host "ERROR: oneocr.dll not found inside the Snipping Tool package. Your Snipping Tool version may not include the OCR engine; update it from the Microsoft Store."
    exit 1
}
Write-Host "Engine files located in: $srcDir"

$dest = "$env:USERPROFILE\.config\oneocr"
New-Item -ItemType Directory -Force $dest | Out-Null

$files = @("oneocr.dll", "oneocr.onemodel", "onnxruntime.dll")
foreach ($f in $files) {
    $src = Join-Path $srcDir $f
    if (-not (Test-Path $src)) {
        Write-Host "ERROR: missing $f in $srcDir"
        exit 1
    }
    try {
        Copy-Item $src $dest -Force
    } catch {
        Write-Host "Copy-Item failed for $f, trying robocopy..."
        robocopy $srcDir $dest $f /NJH /NJS | Out-Null
        if (-not (Test-Path (Join-Path $dest $f))) {
            Write-Host "ERROR: could not copy $f. Run this script as administrator."
            exit 1
        }
    }
    Write-Host "Copied: $f"
}

Write-Host ""
Write-Host "Done. The OneOCR engine is ready at $dest"
Write-Host "You can verify with:  python -c `"import oneocr; oneocr.OcrEngine(); print('OK')`""
