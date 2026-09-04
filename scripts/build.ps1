param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [switch]$Install,
    [switch]$PackageOnly,
    [switch]$ForceStopInstalled,
    [int]$UpdateWaitSeconds = 1800
)

$ErrorActionPreference = "Stop"

if ($Install -and $PackageOnly) {
    throw "-Install and -PackageOnly cannot be used together."
}
if ($PackageOnly -and $ForceStopInstalled) {
    throw "-ForceStopInstalled is only valid when installing the local build."
}
if ($UpdateWaitSeconds -lt 1) {
    throw "-UpdateWaitSeconds must be at least 1."
}

# Local builds should become the desktop build immediately. Automated and
# explicitly package-only builds must not mutate the current machine.
$isCi = $env:CI -match '^(1|true|yes)$'
$installAfterBuild = $Install -or (-not $PackageOnly -and -not $isCi)

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
$packagedHash = (Get-FileHash -LiteralPath $portable -Algorithm SHA256).Hash
$selfTestDir = Join-Path $PWD "tmp\package-self-test"
$process = Start-Process `
    -FilePath $portable `
    -ArgumentList @("--self-test-offline", $selfTestDir) `
    -Wait `
    -PassThru `
    -WindowStyle Hidden
if ($process.ExitCode -ne 0) {
    throw "Packaged self-test failed: $($process.ExitCode)"
}
$selfTestReceipt = Get-Content (Join-Path $selfTestDir "self-test-result.json") -Raw | ConvertFrom-Json
if ($selfTestReceipt.status -ne "ok") {
    throw "Packaged self-test did not produce a valid receipt."
}
& $Python "scripts\verify_packaged_update.py" $portable
if ($LASTEXITCODE -ne 0) { throw "Packaged updater smoke test failed." }

$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) {
    throw "Inno Setup 6.4 or newer is required to build the installer."
}
& $iscc "/DAppVersion=$version" "packaging\installer.iss"
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}

$installer = (Resolve-Path "dist\WeChat-TXT-PDF-Exporter-Installer-v$version.exe").Path
if (-not $installAfterBuild) {
    Remove-Item -LiteralPath $portable
    Write-Host "One-click installer build complete; the desktop installation and shortcut were not updated."
    Get-FileHash -LiteralPath $installer -Algorithm SHA256
    return
}
$installDir = Join-Path $env:LOCALAPPDATA "Programs\WeChatChatExporter"
$installedExe = Join-Path $installDir "WeChat-TXT-PDF-Exporter.exe"
$resolvedInstalledExe = [System.IO.Path]::GetFullPath($installedExe)
function Get-InstalledExporterProcesses {
    @(Get-Process -Name "WeChat-TXT-PDF-Exporter" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Path -and [System.IO.Path]::GetFullPath($_.Path) -eq $resolvedInstalledExe
        })
}
$runningExporters = @(Get-InstalledExporterProcesses)
if ($runningExporters.Count -gt 0) {
    if ($ForceStopInstalled) {
        $runningExporters | Stop-Process -Force -ErrorAction Stop
    } else {
        try {
            $updateEvent = [System.Threading.EventWaitHandle]::OpenExisting(
                "Local\WeChatChatExporter.v1.UpdateExit"
            )
        } catch [System.Threading.WaitHandleCannotBeOpenedException] {
            throw "The running exporter predates safe update coordination. Exit it first, or explicitly use -ForceStopInstalled."
        }
        try {
            if (-not $updateEvent.Set()) {
                throw "Failed to request a safe shutdown from the running exporter."
            }
        } finally {
            $updateEvent.Dispose()
        }
        Write-Host "Waiting for the running exporter to finish current work and exit safely..."
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($UpdateWaitSeconds)
    do {
        Start-Sleep -Milliseconds 250
        $runningExporters = @(Get-InstalledExporterProcesses)
    } while ($runningExporters.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline)
    if ($runningExporters.Count -gt 0) {
        throw "The running exporter did not exit within $UpdateWaitSeconds seconds; installation was not started."
    }
}
$installProcess = Start-Process -FilePath $installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCLOSEAPPLICATIONS', '/SP-') -Wait -PassThru -WindowStyle Hidden
if ($installProcess.ExitCode -ne 0) { throw "Installer failed: $($installProcess.ExitCode)" }

$installedHash = (Get-FileHash -LiteralPath $installedExe -Algorithm SHA256).Hash
if ($packagedHash -ne $installedHash) {
    throw "Installed executable does not match the packaged executable."
}

& (Join-Path $PSScriptRoot "update_desktop_shortcut.ps1") `
    -TargetPath $installedExe `
    -WorkingDirectory $installDir

Remove-Item -LiteralPath $portable
Get-FileHash $installer, $installedExe -Algorithm SHA256
