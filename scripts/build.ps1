param(
    [string]$Python = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python environment not found: $Python"
}

& $Python -m pytest
if ($LASTEXITCODE -ne 0) {
    throw "Tests failed with exit code $LASTEXITCODE."
}
& $Python -m PyInstaller --noconfirm --clean "packaging\WeChat-TXT-PDF-Exporter.spec"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

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
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}

$installer = (Resolve-Path "dist\WeChat-TXT-PDF-Exporter-Installer-v$version.exe").Path
$installDir = Join-Path $env:LOCALAPPDATA "Programs\WeChatChatExporter"
$installedExe = Join-Path $installDir "WeChat-TXT-PDF-Exporter-v$version.exe"
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Copy-Item -LiteralPath $portable -Destination $installedExe -Force
Copy-Item -LiteralPath "LICENSE" -Destination (Join-Path $installDir "LICENSE") -Force
Copy-Item `
    -LiteralPath "THIRD_PARTY_NOTICES.md" `
    -Destination (Join-Path $installDir "THIRD_PARTY_NOTICES.md") `
    -Force

$desktopDir = [Environment]::GetFolderPath("Desktop")
if (-not $desktopDir) {
    throw "Desktop directory could not be resolved."
}
$wechatChatName = -join @(
    [char]0x5FAE,
    [char]0x4FE1,
    [char]0x804A,
    [char]0x5929
)
$exportName = -join @([char]0x5BFC, [char]0x51FA)
$shortcutPath = Join-Path $desktopDir "$wechatChatName TXT-PDF $exportName.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $installedExe
$shortcut.WorkingDirectory = $installDir
$shortcut.IconLocation = "$installedExe,0"
$shortcut.Description = "WeChat TXT/PDF chat and HTML/JSON Moments local exporter"
$shortcut.WindowStyle = 1
$shortcut.Save()

$portableHash = (Get-FileHash -LiteralPath $portable -Algorithm SHA256).Hash
$installedHash = (Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash
if ($portableHash -ne $installedHash) {
    throw "Installed executable does not match the packaged executable."
}
$savedShortcut = $shell.CreateShortcut($shortcutPath)
if ($savedShortcut.TargetPath -ne $installedExe) {
    throw "Desktop shortcut target verification failed."
}
if ($savedShortcut.WorkingDirectory -ne $installDir) {
    throw "Desktop shortcut working-directory verification failed."
}
if ($savedShortcut.IconLocation -ne "$installedExe,0") {
    throw "Desktop shortcut icon verification failed."
}

Get-FileHash `
    $portable, `
    $installer, `
    $installedExe `
    -Algorithm SHA256

Write-Host "Desktop shortcut updated: $shortcutPath"
