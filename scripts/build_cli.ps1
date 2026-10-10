param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $pythonPath = (Get-Command $Python -ErrorAction Stop).Source
    if (-not $SkipTests) {
        & $pythonPath -m pytest
        if ($LASTEXITCODE -ne 0) { throw "Tests failed: $LASTEXITCODE" }
    }
    & $pythonPath -m PyInstaller --noconfirm --clean packaging/WeChat-Local-Exporter-CLI.spec
    if ($LASTEXITCODE -ne 0) { throw "CLI build failed: $LASTEXITCODE" }
    $version = (Select-String -Path pyproject.toml -Pattern '^version = "(.+)"$').Matches.Groups[1].Value
    $executable = (Resolve-Path -LiteralPath "dist/WeChat-Local-Exporter-CLI-v$version.exe").Path
    & $pythonPath scripts/verify_cli.py $executable
    if ($LASTEXITCODE -ne 0) { throw "CLI verification failed: $LASTEXITCODE" }
    & $pythonPath scripts/package_cli.py --executable $executable --version $version --output-dir dist/cli-assets
    if ($LASTEXITCODE -ne 0) { throw "CLI packaging failed: $LASTEXITCODE" }
    Get-ChildItem -LiteralPath dist/cli-assets -File | Get-FileHash -Algorithm SHA256
} finally {
    Pop-Location
}
