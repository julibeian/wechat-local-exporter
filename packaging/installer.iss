#ifndef AppVersion
  #define AppVersion "1.5.0"
#endif

#define AppName "微信聊天本地导出工具"
#define AppPublisher "julibeian"
#define AppURL "https://github.com/julibeian/wechat-txt-pdf-exporter"
#define AppExeName "WeChat-TXT-PDF-Exporter.exe"
#define ReleaseExeName "WeChat-TXT-PDF-Exporter-v" + AppVersion + ".exe"
#define DesktopShortcutName "微信聊天本地导出工具"

[Setup]
AppId={{A7639FD4-469B-41C7-A3A0-901C8E6D12E3}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\WeChatChatExporter
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=WeChat-TXT-PDF-Exporter-Installer-v{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes
; The application owns its export lifecycle and exits through an explicit
; update handoff. The installer must never terminate an active export.
CloseApplications=no
RestartApplications=no

[Files]
Source: "..\dist\{#ReleaseExeName}"; DestDir: "{app}"; DestName: "{#AppExeName}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\RELEASE_NOTES.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; Narrow legacy executable pattern; exports, settings and unrelated files are untouched.
Type: files; Name: "{app}\WeChat-TXT-PDF-Exporter-v*.exe"
Type: files; Name: "{autodesktop}\微信聊天 TXT-PDF 导出.lnk"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#DesktopShortcutName}"; Filename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent
