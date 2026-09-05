# 微信聊天本地导出工具

把本机微信记录导出为 JSON、TXT、PDF，或带图片、表情和视频的 JSONL 资料包。适合查找、归档，或交给 AI 分析。聊天内容在本机处理。

**[下载 Windows 一键安装包](https://github.com/julibeian/wechat-local-exporter/releases/latest)** · [使用方法](#使用方法) · [反馈问题](https://github.com/julibeian/wechat-local-exporter/issues)

## 下载

当前仅支持 Windows 10/11 x64、微信 4.x，无需安装 Python。

最新版本v1.5只提供一键安装包，安装后会创建桌面快捷方式。

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

1. 在**常用电脑**打开本工具，微信窗口将在数秒后弹出，届时请手动点击登录。
2. 进入软件页面操作。
3. 选择保存位置，确认导出。

同电脑、同账号再次打开时会校验并复用本地缓存以**加速启动**（首次加载将自动解密数据库，耗时较长，请耐心等待）。

关闭窗口后程序留在系统托盘，导出继续运行；完成或失败会通知。

## 补充

当前版本v1.5，如有新版发布，软件内会自动提示。

欢迎大家通过 [Issues](https://github.com/julibeian/wechat-local-exporter/issues) 提交问题反馈、功能建议或使用体验。

如有帮助，欢迎在 GitHub 上点一个 **Star** 支持项目持续维护，感谢您的认可！

## 开发

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m wechat_exporter
```

[MIT License](LICENSE) 完全开源；第三方许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
