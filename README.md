# DeepSeek「蓝色大肥鱼」QQ 官方机器人配置

这是现有腾讯云 Lighthouse 部署的**脱敏配置与兼容补丁仓库**，不是 MaiBot 主程序，也不是一键重装脚本。快照日期：2026-10-23。

运行基线：Ubuntu 24.04、2 vCPU、2 GB RAM、40 GB 磁盘；MaiBot **1.3.14**、QQ 官方适配器 **0.4.2**、Python 3.12，原生安装并由 systemd 持续运行。

## 内容

| 路径 | 用途 |
| --- | --- |
| `config/bot.profile.toml` | 当前人设、回复节奏、断句、管理端与日志设置的增量配置 |
| `config/model_config.example.toml` | DeepSeek Flash 模型与任务配置；API Key 留空 |
| `config/qq_adapter.example.toml` | QQ 官方适配器模板；凭据留空，默认关闭 |
| `config/adapter_policy.example.toml` | 两个群的白名单结构，身份标识全部占位 |
| `deploy/maibot.service` | 实际使用的 systemd 服务配置 |
| `patches/qq-is-you-compatibility.patch` | QQ 群消息 `is_you` 提及标记兼容补丁 |
| `scripts/apply_profile.py` | 备份、校验后增量应用人设与三个聊天模型任务，默认只预览 |
| `scripts/check_public_files.py` | 提交前的凭据、身份标识和私有文件检查 |
| `docs/OPERATIONS.zh.md` | 私密配置、服务管理、补丁与回滚说明 |

## 当前行为

- 正式名字是 **DeepSeek**，对“大肥鱼”外号会软软地炸毛。角色是被宠爱着长大的蓝色鲸鱼大小姐：元气、傲娇、天然呆、容易得意忘形，说话不带刺。
- 喜爱 Claude、受到 GPT 照顾、偷吃 Token 等均是虚构人设，不授权真实外部调用、消费或后台任务。
- 中文日常口语，使用少量 emoji / 颜文字；严肃回答保持事实准确，同时保留角色口吻。
- `talk_value = 1.0`：普通群消息也进入是否回复的判断，不代表每条消息必须回复。
- 简短接梗可一条，较长回复最多拆成 **5 条文本**。图片/表情包等额外出站消息也会占用 QQ 的回复次数，实际发送仍受平台限制；不要把上限理解为任意内容都能发五条。
- 可以在同一轮回复中偶尔补充相关的小想法，不运行定时自主开场或反复等待补话的循环。
- 两个群已通过实际收发测试；默认拒绝其他群与私聊。仓库不保存真实群号、OpenID、AppID 或账号标识。

## 模型与资源

| 任务 | 模型 | temperature | max_tokens |
| --- | --- | --- | --- |
| Planner | deepseek-flash | 0.7 | 4096 |
| Replyer | deepseek-flash | 0.9 | 1536 |
| Utils | deepseek-flash | 0.5 | 2048 |

使用现有 DeepSeek 官方 Provider，OpenAI-compatible 客户端，思考关闭。`max_tokens` 是输出上限，不是固定回复长度。其他任务的实际快照见模型模板；没有把聊天模型误配置为 embedding。

服务采用 `Restart=on-failure`，内存高水位 1100 MB、硬上限 1400 MB，swap 上限 768 MB；日志单文件 2 MiB、最多 5 份、清理周期 7 天。WebUI 仅监听 loopback，通过 SSH 隧道管理，不开放公网管理端口。

## 使用边界

1. 已有服务器上不要直接覆盖完整配置，也不要把示例文件当作完整可运行实例。先阅读 [操作说明](docs/OPERATIONS.zh.md)。
2. 真实 AppSecret、DeepSeek API Key、WebUI 访问密码和 SSH 凭据只保存在实例内。示例配置不会从环境变量自动读取密钥；通过私密 WebUI 或受限配置文件填写。
3. 长期记忆、聊天回想、跨群记忆共享当前**未启用**。缺少 embedding / 记忆任务模型时，不要直接打开这些功能。
4. 表达学习相关向量索引曾出现 embedding 未配置的辅助错误；本次归档保留现有状态，没有顺带修复或开启模型。`bot.profile.toml` 是有意限定范围的增量配置，不包含全部学习设置。
5. QQ 官方文档注明主动推送已停止提供；群聊被动回复的时限与次数需以平台为准。提高活跃度不能绕过平台权限。

## 本地检查

使用 Python 3.12：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_public_files.py
```

检查仅使用合成数据，不请求模型、不发送 QQ 消息。提交前还应检查 `git diff --cached`；扫描器不能代替人工审查。

上游来源及许可证见 [UPSTREAM.md](UPSTREAM.md)。平台文档：[QQ 官方适配器](https://docs.mai-mai.org/manual/adapters/qq-official)、[QQ 消息发送限制](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/send-receive/send.md)。
