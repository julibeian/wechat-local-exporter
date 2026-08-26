# 微信聊天 TXT / PDF 导出工具

把本人电脑里的微信聊天记录导出为可搜索的 TXT 和 PDF。所有处理都在本机完成，不上传聊天内容。

## 下载

[前往 Releases 下载 Windows 一键安装包](https://github.com/julibeian/wechat-txt-pdf-exporter/releases/latest)

当前只维护 Windows 10/11 x64 正式版。下载安装包后双击运行，按向导完成安装即可。

当前安装包尚未配置商业代码签名证书，Windows 可能显示“未知发布者”。

## 功能

- 自动识别当前微信账号和安装位置。
- 选择联系人或群聊，可按日期范围导出。
- 支持 TXT、快速 PDF、带图片/表情的完整 PDF。
- 可保存微信已有的语音转文字。
- 联系人、群聊和每个会话分别保存，不会混在同一文件夹。
- 保留导出历史，可查看时间并直接打开文件位置。

## 使用

1. 启动软件，确认连接微信。
2. 在微信窗口完成登录。
3. 选择联系人或群聊、日期、格式和输出目录。
4. 点击“导出选中会话”。

导出目录结构：

```text
微信聊天导出/
├─ 联系人/
│  └─ 联系人名称 [标识]/
└─ 群聊/
   └─ 群聊名称 [标识]/
```

> 请只处理本人有权访问的数据，并妥善保管导出的聊天记录。

## 兼容范围

- Windows 10/11 x64
- 微信 4.x（已针对 4.1.12.55 验证）
- TXT：UTF-8 with BOM
- PDF：A4、中文可搜索文本

## 重要版本历程

- **v0.3–v0.4**：完成本机 TXT/PDF 基础导出和 Windows 独立程序。
- **v0.5–v0.6**：完善微信 4.x 读取、语音文字、图片和表情导出。
- **v0.7**：加入耗时预估、实时进度和图片并行读取。
- **v0.8**：加入联系人/群聊独立目录、导出历史和程序内部完整性检查。
- **v1.0**：首个公开正式版，加入 GitHub Star 提示和 Windows 一键安装包。

## 开发

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m wechat_exporter
```

打包：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean packaging\WeChat-TXT-PDF-Exporter.spec
```

## 说明

- 项目不提供远程接口，不修改微信原始数据库。
- 数据库密钥仅在运行内存中使用。
- 开源协议：[MIT](LICENSE)
- 第三方参考与许可：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)

如果这个工具对你有帮助，欢迎点亮 GitHub Star。
