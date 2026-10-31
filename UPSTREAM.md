# 上游来源

- MaiBot: https://github.com/Mai-with-u/MaiBot
- QQ 官方适配器: https://github.com/Mai-with-u/MaiBot-QQ-Adapter
- 适配器基线提交: `7a0c321bfc65e157abdb5e8a10913181387b6081`
- 本仓库只保存部署配置与小范围兼容补丁，不复制 MaiBot 主程序。
- QQ 适配器补丁适用源文件的许可证见 `licenses/QQ-Adapter-LICENSE`。

- Bilibili AI bot: https://github.com/chenluQwQ/bilibili-ai-bot ; source adaptation license: `licenses/Bilibili-AI-Bot-LICENSE`.
- `extensions/` contains local deployment additions and adapted source; it is not a full upstream distribution.

- 原生 QQ 适配器（SnowLuma／NapCat）：https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter ，本次部署版本 1.1.9；作者 MaiBot Team。安装包 `_manifest.json` 声明 `GPL-v3.0-or-later`，许可证正文见 `licenses/QQ-Native-Adapter-GPL-3.0.txt`（标准文本取自 SPDX license-list-data）。上游及已安装目录未含单独 LICENSE 文件；这里保留 manifest 声明，不将 QQ 官方适配器的 MIT 许可证误用于原生适配器。图标和第三方程序从原上游发行版取得。
