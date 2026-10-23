# 操作与恢复

## 保留现有实例

本仓库不创建云资源、不更改 SSH 认证、不上传私钥，不自动安装或重启线上服务。默认实例目录 `/srv/maibot`，专用系统用户 `maibot`，Python 环境 `/opt/maibot/tools/maibot`。

`config/bot.profile.toml` 是增量配置，**不能直接复制成 bot_config.toml**。它只保留本次调整的字段，省略凭据、身份和与本次配置无关的默认项。现有完整配置由 MaiBot 自己生成和维护。

## 增量应用配置

先把此仓库放在专用用户可读取的目录，例如 `/srv/maibot/deployment`。在已有 MaiBot 1.3.14 环境中预览：

```sh
sudo -u maibot /opt/maibot/tools/maibot/bin/python \
  /srv/maibot/deployment/scripts/apply_profile.py --instance /srv/maibot
```

需要已有名为 `deepseek-flash` 的模型，模型标识也为 `deepseek-flash`，并设置 `thinking.type = "disabled"`。Provider 与 API Key 由用户私密配置。脚本不创建 Provider，不改模型凭据、QQ 适配器配置或白名单。

确认预览通过后，使用同一命令追加 `--apply`。脚本会调用 MaiBot 自身配置校验器，在实例 `backups/` 下创建权限 700 的备份目录，配置文件权限 600，然后替换配置。它只调整 bot profile 与 planner/replyer/utils 三个任务；其余模型任务保持不变。失败信息不回显配置值。

配置通常会热加载。在日志中确认“配置热重载完成”，再由群成员发一条少量人工测试消息。不要为了校验配置反复调用付费模型。

## 凭据与群权限

- `model_config.example.toml` 的 `api_key` 留空；不要用这个空模板覆盖已配置好的实例。
- `qq_adapter.example.toml` 的 AppID、AppSecret 留空且插件默认关闭。由用户本人通过隧道里的 WebUI 配置，再启用适配器；不要通过聊天或 Git 传递凭据。
- `adapter_policy.example.toml` 中的账号与群标识都是占位符，需要替换为官方适配器实际看到的 OpenID。AppID 不能当作机器人 OpenID。
- 仅添加用户明确授权的群；继续保留 `default_action = "block"`，不要添加 `*`。私聊默认拒绝。
- 由群主在 QQ 群设置里为机器人开启“获取群内全部消息”。这不等于 MaiBot 的白名单已放行，两层都需要满足。
- 不使用 `/assign_group_id` 临时绕过范围控制；管理命令在示例中使用禁用占位标识。

## SSH 隧道和服务

在自己的终端设置连接目标与现有密钥路径。仓库不提供真实地址或密码：

```sh
export MAIBOT_SSH_TARGET='ubuntu@YOUR_SERVER_HOST'
export MAIBOT_SSH_IDENTITY='/path/to/your/existing/key'
ssh -N -i "$MAIBOT_SSH_IDENTITY" \
  -L 127.0.0.1:18001:127.0.0.1:8001 "$MAIBOT_SSH_TARGET"
```

然后访问 `http://127.0.0.1:18001`。WebUI 访问密码由实例维护，不是 QQ AppSecret 或 SSH 密钥口令。关闭隧道不影响服务器机器人运行。

在服务器上管理已存在的服务：

```sh
sudo systemctl status maibot.service
sudo systemctl restart maibot.service
sudo systemctl stop maibot.service
sudo systemctl start maibot.service
sudo systemctl is-enabled maibot.service
sudo journalctl -u maibot.service -n 100 --no-pager
```

日志可能含聊天内容或访问令牌，只私密查看，勿复制到 issue、仓库或公开报告。`deploy/maibot.service` 是现有配置的记录，不会自动安装；如果要重新配置服务，先确认目录、专用用户和许可证/隐私条款确认文件。不要为了满足 `ConditionPathExists` 而跳过阅读相关条款。

## QQ @ 识别补丁

补丁基于 QQ 官方适配器 0.4.2 的指定提交，位置是 `codec.py` 的 `_is_bot_mentioned`。全量群消息中，机器人在群内的 OpenID 可能不同于网关账号 ID；补丁识别官方事件 `mentions[].is_you = true`，并保留旧 ID 比较逻辑。只有明确布尔值 `true` 才视为自身提及。

此部署已应用补丁，勿重复打补丁。对匹配的未修改基线，可先在适配器目录执行：

```sh
patch --dry-run -p1 < /path/to/deployment/patches/qq-is-you-compatibility.patch
```

仅在 dry-run 成功、已备份 `codec.py` 且确实需要时去掉 `--dry-run`。代码变更需要插件/服务重启，可能短暂中断连接。上游升级后先检查是否已修复，不要强行套用旧补丁。临时群标识诊断已经从线上移除，不属于本仓库内容。

## 回滚与记忆

恢复时停止服务，选择本次操作生成的准确备份目录，将配置恢复为专用用户所有、权限 600，再启动服务。不要把含密钥的备份复制进本仓库。

两个群目前共用人设，但实时上下文与长期记忆未共享。要开启记忆，需要先配置真正的 embedding 模型，并安排 `mid_memory` 等总结任务；不能将 Flash 聊天模型填成向量模型。共享组会允许两个群互相检索记忆，应单独决定和验证，不由本同步脚本自动开启。
