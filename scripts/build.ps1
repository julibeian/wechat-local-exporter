param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found: $Python"
}

& $Python -m pytest
& $Python -m PyInstaller --noconfirm --clean "packaging\WeChat-TXT-PDF-Exporter.spec"

$version = (Select-String -Path "pyproject.toml" -Pattern '^version = "(.+)"$').Matches.Groups[1].Value
$portable = (Resolve-Path "dist\WeChat-TXT-PDF-Exporter-v$version.exe").Path
$selfTestDir = Join-Path $PWD "tmp\package-self-test"
$process = Start-Process `
    -FilePath $portable `
    -ArgumentList @("--self-test", $selfTestDir) `
    -Wait `
    -PassThru `
    -WindowStyle Hidden
if ($process.ExitCode -ne 0) {
    throw "Packaged self-test failed: $($process.ExitCode)"
}

$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) {
    throw "Inno Setup 6 is required to build the installer."
}
& $iscc "/DAppVersion=$version" "packaging\installer.iss"

Get-FileHash `
    $portable, `
    "dist\WeChat-TXT-PDF-Exporter-Installer-v$version.exe" `
    -Algorithm SHA256
