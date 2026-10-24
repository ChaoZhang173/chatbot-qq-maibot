# QQ／B站 DeepSeek 机器人部署扩展

2026-10-24 已与当前服务器代码核对的脱敏快照。增量保存 MaiBot 1.3.14 的统一核心、QQ 官方适配器补丁和 B站执行器，不包含完整上游程序。

## 当前代码范围

- `extensions/maibot_unified_core`：有来源与会话边界的共同记忆、人格、QQ 主人命令、熟人私聊身份验证、媒体与公开资料工具。
- `extensions/paidtasks`：已增量部署的爱发电小任务试运行模块、受保护配置样例、部署/回滚脚本与验收证据；接单仍关闭。
- `extensions/phase2`：既有 B站动态、评论、互动、直播文字聊天与账户设置执行器；与统一核心通信，保留动作回执。
- `extensions/qq_official_adapter`：当前本地适配器源文件，包括图片、提及和熟人私聊接入变更。
- `extensions/tests_phase25`、`extensions/tests_phase2`：离线契约与回归测试。
- `config`：脱敏模板和人格配置。旧 `bot.profile.toml` 与 `model_config.example.toml` 是 10月23日基线，不能用来覆盖今天的线上配置；今天的人格增量见 `persona_revision_20261024.json`；当前服务器配置投影见 `*.current.example.json`。
- `deploy`、`scripts`：服务模板、受限备份及历史增量应用工具。

## 部署与验证边界

本次核对了服务器44个扩展源文件／清单：42个与本地逐字节一致，另补入2个适配器文件。QQ、B站服务均 active/enabled。当前配置投影来自服务器，密钥与身份映射已清除；这不是新的真人收发或支付验收。

原始生产配置、账号／群／主人标识、API key、密码、Cookie、会话、聊天内容、数据库、运行日志、截图和私有备份不包含在仓库。真实配置只在受保护的服务器位置维护。示例默认关闭对外接入并保持白名单。

爱发电收单模块已在现有 Lighthouse 增量部署，主人受限私聊通知和测试 PNG 已取得真实 QQ 回执，内部试运行预算 ¥1 已记账。爱发电 API/商品尚未核验，接单关闭、真实支付未验收、宣传未发布。详见 [收单报告](extensions/paidtasks/REPORT.zh.md) 和 [验收矩阵](extensions/paidtasks/ACCEPTANCE.zh.md)。

原机器人源码目录保留收单前的精确基线；收单对统一核心/QQ适配器的变更保存在 `extensions/paidtasks/integration`，通过带源码哈希检查的构建器得到线上四个增量文件，不能忽略补丁直接覆盖线上插件。

不要直接覆盖整个 `/srv/maibot` 或 `/opt/bilibot/app`。升级前应核对版本、现有配置、插件能力注册、服务参数和准确的代码差异，完成私密备份后只替换对应文件。源程序依赖既有 Host 扩展，单独复制插件不等于可以从零启动。

查看 [当前快照说明](docs/SNAPSHOT-20261024.zh.md)、[历史运行说明](docs/OPERATIONS.zh.md) 及 [上游许可证](UPSTREAM.md)。
