# wechatexporter CLI：供 Agent 和脚本调用

这是图形版之外的免安装入口，使用相同的微信连接、账号校验、数据库缓存和导出引擎。支持 Windows 10/11 x64、微信 4.x，无需安装 Python。聊天数据输出到指定的本机目录。

CLI 不打开导出界面。第一次连接仍需本人完成微信登录；已有经过验证的同电脑、同 Windows 用户、同微信账号缓存时，可以直接批量导出。CLI 不能绕过微信登录，不能导出本机不存在的历史记录。

## 下载与启动

在本项目 GitHub Releases 中选择 `wechatexporterCLI_v<版本>.zip`（标有“命令行”），解压后运行 `wechatexporter.exe`。安装包供日常点击操作，CLI ZIP 供 Agent、终端和自动化任务使用。

Agent 可通过 GitHub API 选择最新包含 CLI 的正式版本并自动下载：

```powershell
$releases = Invoke-RestMethod 'https://api.github.com/repos/julibeian/wechat-local-exporter/releases?per_page=30'
$release = $releases | Where-Object {
    -not $_.draft -and -not $_.prerelease -and
    @($_.assets | Where-Object { $_.name -match '^wechatexporterCLI_v[0-9.]+\.zip$' }).Count -eq 1
} | Select-Object -First 1
if (-not $release) { throw '尚无已发布的 CLI 包；请使用本地开发构建。' }
$asset = $release.assets | Where-Object { $_.name -match '^wechatexporterCLI_v[0-9.]+\.zip$' }
$digest = [regex]::Match($asset.digest, '^sha256:([a-fA-F0-9]{64})$')
if (-not $digest.Success) { throw 'CLI 包的 SHA-256 校验码缺失。' }
$expected = $digest.Groups[1].Value
$targetDir = Join-Path $PWD 'wechat-cli'
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
$zipPath = Join-Path $targetDir $asset.name
Invoke-WebRequest $asset.browser_download_url -OutFile $zipPath
if ((Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash -ne $expected) { throw 'CLI 包校验失败。' }
Expand-Archive -LiteralPath $zipPath -DestinationPath (Join-Path $targetDir 'bin') -Force
& (Join-Path $targetDir 'bin/wechatexporter.exe') version
```

ZIP 内含运行程序、本文、任务示例、许可说明及文件校验清单。下载页附件旁的 SHA-256 可直接复制，用于校验整个包；Agent 可读取 GitHub API 的 `digest` 字段。

## Agent 最短调用流程

```powershell
.\wechatexporter.exe version
.\wechatexporter.exe schema
.\wechatexporter.exe diagnose
.\wechatexporter.exe conversations --query '示例项目组' --quiet
.\wechatexporter.exe export --query '示例项目组' --format json --output '.\exports' --quiet
```

首次无法连接时，会返回退出码 `3` 和 `CONNECTION_REQUIRED` / `LOGIN_REQUIRED`。只有本人明确允许重启微信时才执行：

```powershell
.\wechatexporter.exe connect --allow-restart
```

该命令可能退出并启动微信以获取密钥，需要本人在微信完成登录；后续导出无需逐次点击软件。默认命令不会主动重启微信。微信未运行时，连接器可使用上次已确认账号的有效本机缓存，并刷新变化的本机数据库。

## 选择会话和时间

`conversations` 返回 `id`、`name`、`type`、`last_timestamp`，不返回聊天正文。`accounts` 列出本机数据目录的账号 ID。建议 Agent 先列出会话，再用返回的稳定 ID 调用；重名的联系人也能区分。

```powershell
.\wechatexporter.exe export --conversation 'wxid_example' --conversation '12345@chatroom' --format json --output '.\exports'
.\wechatexporter.exe export --all --type group --since '2026-10-01' --until '2026-10-10' --format txt --output '.\exports'
.\wechatexporter.exe export --query '项目组' --format html --output '.\archives'
.\wechatexporter.exe export --all --type contact --output '.\exports' --dry-run
```

- `--conversation` 只接受精确 ID，可重复；`--query` 按名称或 ID 包含匹配，导出全部匹配会话；`--all` 需要显式给出。三个选择方式互斥。
- `--type all|group|contact` 限定会话类型；本人条目只用于朋友圈。
- `--since` / `--until` 接受日期或 ISO 8601 时间。纯日期按运行电脑的本地时区解释，结束日期包含整天；ISO 时间可以带明确时区偏移。
- `--account` 是预期账号校验，匹配账号目录 ID 或微信 ID；不自动切换账号。不匹配时停止导出并返回退出码 `4`。
- `--dry-run` 会连接、验证账号并解析所有选择，返回计划，不生成归档。

## 四类导出

| 命令 | 输出和默认行为 |
| --- | --- |
| `export` | 默认快速 JSON；`--format` 可选 `json`、`txt`、`html`、`pdf`。HTML 使用本机图片和动态表情；`--include-videos` / `--include-files` 为 HTML 收录本机视频 / 文件实体。PDF 可加 `--pdf-images`。 |
| `package` | 每个会话一个 JSONL + 图片/表情 ZIP，适合 AI 分析；视频默认不收录，可加 `--include-videos`，默认单视频上限 100 MB。原始语音不装包，保留微信已有转写。 |
| `files` | 每个会话一个聊天附件 ZIP；`--category` 可选 `pdf`、`word`、`excel`、`powerpoint`、`archive`、`other`，可重复，默认所有类型，单文件上限 100 MB。 |
| `moments` | 联系人 / 本人本机可见朋友圈的 HTML + JSON + 媒体归档。`--self` 选择本人，不支持群聊或日期筛选；当前朋友圈引擎可能下载朋友圈媒体。 |

```powershell
.\wechatexporter.exe package --all --type group --include-videos --max-file-mb 100 --output '.\ai-data'
.\wechatexporter.exe files --query '项目组' --category pdf --category word --output '.\files'
.\wechatexporter.exe moments --self --output '.\moments'
```

聊天 HTML 和 AI 资料包默认只处理本机资源；显式加 `--allow-network-media` 后才尝试补全缺失资源。CLI 与图形版使用相同的导出历史。重复运行会生成新的可用文件名，保留已有归档；当前不提供跨次运行的增量消息导出或断点续传。

## 一次连接批量执行多组任务

```powershell
.\wechatexporter.exe batch --job '.\example-job.json' --dry-run --quiet
.\wechatexporter.exe batch --job '.\example-job.json' --quiet > '.\result.json'
```

任务文件使用 UTF-8 JSON，兼容 UTF-8 BOM。相对输出目录基于**任务文件所在目录**。版本必须为 `1`，任务最多 1000 组、文件最多 2 MB。例如：

```json
{
  "version": 1,
  "account": "wxid_expected_account",
  "jobs": [
    {
      "task": "export",
      "all": true,
      "type": "group",
      "format": "json",
      "since": "2026-10-01",
      "until": "2026-10-10",
      "output": "./exports/text"
    },
    {
      "task": "package",
      "conversations": ["12345@chatroom"],
      "include_videos": false,
      "output": "./exports/media"
    }
  ]
}
```

可省略 `account`，使用当前已确认账号。先用 `accounts` / `conversations` 获取真实账号和会话 ID，再替换示例。所有任务参数和会话选择会在开始导出前校验；未知字段、未知 ID、无匹配会话或非法日期会停止整批任务，不先导出一部分。

同一批次只连接并准备数据库一次，再依次处理会话。单个会话失败时默认继续，已完成结果保留；加 `--fail-fast` 可在首个失败后停止。任务文件不能自行授权重启微信，重启必须在命令行显式使用 `--allow-restart`。

## 返回约定

除帮助和 `--version` 外，stdout 只输出一个 UTF-8 JSON 对象，`schema_version` 固定为 `1`。进度为 stderr 上的 JSON 行，`--quiet` 关闭进度。聊天正文只写到归档文件。

导出结果包含 `success`、`status`、`account`、`summary`、`results`。每项结果给出任务序号、会话 ID、绝对文件路径、耗时和警告；`counts` 按任务区分 `messages`、`attachments`、`moments`，聊天任务另有 `message_count`。失败项给出 `error.code` / `error.message`。缺失媒体等信息保留在 `warnings`，Agent 应检查这些信息后再判断资料是否够用。

`attachments` 表示附件索引记录总数，包含成功、缺失、过大或未选类型的记录；成功保存的文件数量与具体状态以 ZIP 内 `files.jsonl` 为准。

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令成功；导出警告仍需读取 |
| 1 | 操作失败 / 所有已尝试的会话失败 |
| 2 | 参数、日期、任务文件或会话选择无效 |
| 3 | 需要微信登录 / 首次连接 |
| 4 | 账号不匹配 |
| 5 | 另一个 CLI 或图形版正在运行 |
| 6 | 一部分会话成功，一部分失败 |
| 130 | Ctrl+C 取消；当前未完成会话清理，其他已完成归档保留 |

不要同时启动多个 CLI 进程来处理同一台电脑上的账号缓存；使用 `batch`。图形版托盘驻留时也持有实例锁，CLI 返回 `BUSY`，不自动中断已有导出；请在图形版实际退出后重试。

## 开发与离线验证

```powershell
.\.venv\Scripts\python.exe -m wechat_exporter.agent_cli --help
.\.venv\Scripts\python.exe -m wechat_exporter.agent_cli self-test --output '.\tmp\cli-self-test'
pwsh -NoProfile -File scripts/build_cli.ps1
```

`build_cli.ps1` 运行测试、构建控制台 EXE、验证实际 EXE 的 JSON 输出 / 参数错误 / 合成数据导出，并生成 `dist/cli-assets` 中的 ZIP 和校验文件；不安装图形版。项目完整构建和 GitHub Release 工作流也生成此下载方式。

CLI 自检使用合成数据，验证 JSON、TXT、PDF、HTML、AI 资料包及朋友圈的底层导出能力，不连接真实微信。真实账号连接和首次密钥获取仍需在受支持微信版本下验证。
