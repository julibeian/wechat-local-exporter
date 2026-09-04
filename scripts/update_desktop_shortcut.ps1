param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath,
    [string]$WorkingDirectory = (Split-Path -Parent $TargetPath),
    [string]$ShortcutPath
)

$ErrorActionPreference = "Stop"

$resolvedTarget = (Resolve-Path -LiteralPath $TargetPath).Path
$resolvedWorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
if (-not $ShortcutPath) {
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
    $localExportName = -join @(
        [char]0x672C,
        [char]0x5730,
        [char]0x5BFC,
        [char]0x51FA,
        [char]0x5DE5,
        [char]0x5177
    )
    $ShortcutPath = Join-Path $desktopDir "$wechatChatName$localExportName.lnk"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = $resolvedTarget
$shortcut.WorkingDirectory = $resolvedWorkingDirectory
$shortcut.IconLocation = "$resolvedTarget,0"
$shortcut.Description = "WeChat local chat and Moments exporter"
$shortcut.WindowStyle = 1
$shortcut.Save()

$savedShortcut = $shell.CreateShortcut($ShortcutPath)
if ($savedShortcut.TargetPath -ne $resolvedTarget) {
    throw "Desktop shortcut target verification failed."
}
if ($savedShortcut.WorkingDirectory -ne $resolvedWorkingDirectory) {
    throw "Desktop shortcut working-directory verification failed."
}
if ($savedShortcut.IconLocation -ne "$resolvedTarget,0") {
    throw "Desktop shortcut icon verification failed."
}

Write-Host "Desktop shortcut updated: $ShortcutPath"
