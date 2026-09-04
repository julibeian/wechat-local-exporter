# 微信聊天本地导出工具

不用再逐张整理聊天截图。把本机微信记录导出为给人看的 PDF、给 AI 读的 JSONL，或通常几秒即可生成的 JSON/TXT。所有聊天内容都在本机处理。

**[下载最新版](https://github.com/julibeian/wechat-txt-pdf-exporter/releases/latest)** · [使用方法](#使用方法) · [问题与建议](https://github.com/julibeian/wechat-txt-pdf-exporter/issues) · [历史版本](https://github.com/julibeian/wechat-txt-pdf-exporter/releases)

## 下载

支持 Windows 10/11 x64、微信 4.x，无需安装 Python。

- **安装版（推荐）**：自动安装并创建桌面快捷方式，可在软件内检查更新。
- **便携版**：无需安装，下载后直接运行。

安装包暂未配置商业代码签名，Windows 可能提示“未知发布者”。请只从本项目 Release 下载，并可使用同页 SHA-256 文件校验。

## 四种导出任务

| 任务 | 输出 | 适合 |
| --- | --- | --- |
| 聊天文字 | JSON、TXT 或 PDF | 快速搜索、阅读和普通文字分析 |
| AI 完整资料包 | 每个会话一个 JSONL + 媒体 ZIP | 把较完整、可追溯的上下文交给 AI |
| 批量聊天文件 | 每个会话一个附件 ZIP | 集中提取 Word、PDF、Excel、压缩包等文件 |
| 朋友圈归档 | HTML + JSON + 媒体 + SHA-256 | 离线浏览和长期保存本机可见朋友圈 |

JSON 是默认的快速纯文字格式。PDF 只有在选中后才显示快速版和完整版：快速版使用媒体占位，完整版额外读取本机图片和表情。

AI 完整资料包保存结构化消息及本机已有的图片、表情和视频。视频默认单个不超过 100 MB；普通文档附件不装入资料包。原始语音不装包，只保留微信已有的转写文字。缺失、过大或无法识别的内容会记录状态，不会静默丢弃或猜测。

## 使用方法

1. 在常用电脑登录微信，然后启动本工具。
2. 选择联系人、群聊或“我自己”。
3. 依次选择任务、时间、格式或内容范围。
4. 确认保存位置后导出。

同电脑、同账号再次打开时，软件会校验并复用本地缓存，只刷新变化的数据库。关闭窗口后程序驻留系统托盘，导出继续运行；完成或失败会显示 3 秒通知。托盘菜单提供“真正退出”。

## 数据与联网边界

- 仅读取本机微信已有数据，不修改微信，也不能补齐尚未同步到电脑的历史记录。
- 聊天数据库和导出内容不上传。检查更新只访问本项目的公开 GitHub Release。
- 朋友圈媒体获取及用户主动开启的表情补全可能联网。
- 快速缓存位于当前 Windows 用户的本地应用数据目录；密钥由 Windows DPAPI 加密，但解密数据库快照仍属于敏感数据。
- 请只处理本人有权访问的数据，并妥善保管导出文件。

## v1.5

v1.5 是最后一个以新增功能为主的版本，后续主要处理兼容、安全和缺陷。主要变化见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

欢迎在 [Issues](https://github.com/julibeian/wechat-txt-pdf-exporter/issues) 提交使用意见、兼容性结果和可复现的 Bug；请勿上传真实聊天、数据库、密钥或其他隐私材料。

## 开发

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m wechat_exporter
```

运行 `scripts\build.ps1` 可完成测试、打包、自检、安装包和 SHA-256 清单生成。本机构建默认同步安装版和桌面快捷方式；仅生成发布文件时使用 `-PackageOnly`。

项目采用 [MIT License](LICENSE) 完全开源；第三方参考与许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
