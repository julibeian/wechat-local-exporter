# 微信聊天本地导出工具

把本机微信记录导出为 JSON、TXT、PDF，或带图片、表情和视频的 JSONL 资料包。适合查找、归档，或交给 AI 分析。聊天内容在本机处理。

**[下载最新版本](https://github.com/julibeian/wechat-txt-pdf-exporter/releases/latest)** · [使用方法](#使用方法) · [历史版本](#历史版本) · [反馈问题](https://github.com/julibeian/wechat-txt-pdf-exporter/issues)

## 下载

支持 Windows 10/11 x64、微信 4.x，无需安装 Python。Release 只提供一键安装包，安装后会创建桌面快捷方式。

安装包没有商业代码签名，Windows 可能提示“未知发布者”。请只从本项目下载。

## 能做什么

| 任务 | 输出 | 用途 |
| --- | --- | --- |
| 聊天文字 | JSON、TXT 或 PDF | 搜索、阅读、文字分析 |
| AI 完整资料包 | 每个会话一个 ZIP（JSONL + 媒体） | 交给 AI 分析较完整的上下文 |
| 批量聊天文件 | 每个会话一个附件 ZIP | 集中提取 Word、PDF、Excel 等文件 |
| 朋友圈归档 | HTML + JSON + 媒体 | 离线浏览和保存本机可见朋友圈 |

JSON/TXT 适合快速导出纯文字。PDF 适合阅读；完整版会额外读取本机图片和表情。AI 资料包默认收录本机已有的图片、表情和单个不超过 100 MB 的视频；原始语音不装包，只保留微信已有的转写。聊天文件请用独立的批量导出任务。

## 使用方法

1. 在常用电脑登录微信并打开本工具。
2. 选择对象，再选任务、时间和格式。
3. 选择保存位置，确认导出。

同电脑、同账号再次打开时会校验并复用本地缓存。关闭窗口后程序留在系统托盘，导出可继续运行；完成或失败会通知。

## 说明

- 仅读取本机已有记录，不修改微信，也不能补齐电脑上没有的历史。
- 聊天数据库和导出内容不上传。检查更新只访问本项目 Release；朋友圈媒体和主动开启的媒体补全可能联网。
- 缓存中的密钥受 Windows DPAPI 保护；缓存的解密数据库和导出文件仍是敏感数据，请妥善保管。

这是个人边学边做的项目，目前只能算基本可用，离成熟还有明显距离。UI 较粗糙，作者开发经验和测试条件有限；不同微信版本、少见消息和大批量导出仍可能出错。请先用少量记录测试，欢迎提交 [问题、建议或测试结果](https://github.com/julibeian/wechat-txt-pdf-exporter/issues)，也欢迎 PR。请勿上传真实聊天、数据库、密钥或联系人信息。

v1.5 是最后一个以新增功能为主的版本。后续主要修复 Bug，并处理微信兼容和安全问题。

## 历史版本

- **[v1.5.0](https://github.com/julibeian/wechat-txt-pdf-exporter/releases/tag/v1.5.0)**（最新）：四类导出任务、同账号缓存和托盘后台导出。
- **[v1.3.0](https://github.com/julibeian/wechat-txt-pdf-exporter/releases/tag/v1.3.0)**：软件内更新、多账号连接修复和更新失败恢复。
- **[v1.2.0](https://github.com/julibeian/wechat-txt-pdf-exporter/releases/tag/v1.2.0)**：朋友圈离线归档、媒体处理和 SHA-256 清单。
- **[v1.0.0](https://github.com/julibeian/wechat-txt-pdf-exporter/releases/tag/v1.0.0)**：首个公开版，支持 TXT、快速 PDF 和完整 PDF。

v1.1 和 v1.4 未单独发布 GitHub Release，相关改动分别并入 v1.2.0 和 v1.5.0。更早的 v0.x 为本地开发版本。

## 开发

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m wechat_exporter
```

[MIT License](LICENSE) 完全开源；第三方许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
