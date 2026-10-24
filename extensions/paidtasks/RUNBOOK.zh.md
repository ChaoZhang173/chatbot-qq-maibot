# 增量部署、运维与真实 ¥1 验收

已完成现有 Lighthouse 的增量部署。39项测试在安装前通过；真实主人通知及PNG回执见 `evidence/deployment.json`。接单仍关闭，真实付款未验证。下述步骤供后续运维，不能重复安装或重复拨款。

## 1. 恢复连接与安装前核对

本机任务目录中的 `恢复本轮服务器连接.command` 会使用已有 SSH 私钥建立可复用连接，不改变服务器认证。用户在自己的终端输入口令并保留窗口即可；不要把口令放入聊天、环境配置或脚本。原交互终端已连接不一定表示自动化进程能复用该会话。

再次核对 maibot、bilibot 状态、内存/磁盘、插件哈希及现有未提交变化。若与 `evidence/server-parity.json` 不一致，先重做差异审查；不要强行覆盖。

生产目录约定：

| 位置 | 用途 |
|---|---|
| `/srv/maibot/paid/policy.json` | 受保护的收单配置 |
| `/srv/maibot/paid/afdian.env` | 仅收单服务加载的凭据，root:600 |
| `/srv/maibot/paid/state.sqlite` | 付款、冻结报价、预算、队列、发送账本 |
| `/srv/maibot/paid/artifacts/` | 私有成果及版本/哈希 |
| `/srv/maibot/paid/paid.sock` | 仅 root/maibot 可访问的管理接口 |
| `/opt/maibot/paidtasks-current` | 指向版本化增量代码 |
| `/srv/maibot/paid-backups/` | root 私有回滚备份 |

## 2. 凭据和安全默认值

优先复用本机 `private/afdian.env`，文件必须是 600，目录 700。它仅含 `AFDIAN_CREATOR_USER_ID` 与 `AFDIAN_API_TOKEN`。传到服务器的受保护暂存目录后由安装器保存，不出现在命令参数、模型上下文或日志中。已经暴露过的 key 建议在爱发电恢复后轮换，再替换该环境文件并重启 paidtasks；不要改动机器人其他账号凭据。

初始配置：不开接单、不发布宣传、不调用模型、不调用付费生图；价格及最高单价均 ¥1，单任务预算最多 ¥0.25、每日最多 ¥1、单并发、配置模板项目预算 0；用户已授权的本轮 ¥1 已用固定编号管理请求记入生产账本，当前余额 ¥1。**每日/单任务上限不等于已经获得这笔预算。** 预算只能经 owner 管理入口明确拨入，平台收款或模型账户余额均不会自动转成可用额度。

## 3. 构建、检查和增量安装

已生成 `build/integration/` 四个文件和 `integration/changes.patch`，对应受保护的哈希清单。公开源码可以对已发布脱敏快照重新构建：

```sh
python3 scripts/build_integration.py /path/to/inspected-baseline build/integration
python3 scripts/fetch_font.py
python3 -m unittest discover -s tests -v
```

基线目录要求具有 `extensions/maibot_unified_core/` 和 `extensions/qq_official_adapter/`。字体来自 Noto 官方仓库，下载脚本要求已审查的 SHA256 与许可一致；来源改变时停止。分发压缩包已附字体。

将增量包及凭据分别传到现有服务器，凭据目录仅限操作者访问。以下在该服务器解压目录执行：

```sh
sudo python3 deploy/install.py --bundle "$PWD" --credentials /path/to/protected/afdian.env
```

安装器会先核对原服务及精确源码哈希、使用现有 MaiBot Python 跑测试，再备份原插件、关键配置和 SQLite；只改动两个插件的接入点并添加两个适配模块。它新增一个受限的本机服务，短暂重启 MaiBot，保持现有 B站进程及其数据不动，不安装整套机器人、不新建云资源。已有收单安装会被拒绝覆盖，需另行审查升级。

系统服务上限 192MB、40% 单核 CPU、32 个任务/线程。成果存储限定路径及大小；收款与任务运行异步分离。新版本默认接单关闭。成功后仍需核对 native SDK 参数、原聊天/记忆及图片路由，记录真实环境测试结果。

## 4. 官方查询及商品现场核对

凭据已载入的受保护环境中运行 `python -m paidtasks.probe --output /path/to/private/probe.json`。仅输出网站 HTTP 状态、API 成败及数量，不输出订单。先获得真实 ping 与查单成功；随后让后台轮询完成历史基线。初次见到的历史订单不会自动开工。

商品必须核对：可访问购买链接、售卖类型、准确 plan_id/SKU、单价 ¥1、数量操作、留言是否原样出现在官方查单结果、是否需要收货信息、真实费率/提现与退款入口。不要为了发现 SKU 先盲目付款，不把方案月份当成商品件数。

`product_verified`、`quantity_verified`、`remark_verified` 均须有现场证据。默认只许一件 ¥1；未验证数量时不开放多件。若留言不能可靠用于绑定，保留 `remark_verified=false`，使用 owner 人工归属核验路径，仍须官方 API 核款。

商品说明建议见 `商品说明建议.txt`；没有编辑商品或接受平台协议的现场权限时交由用户完成。纯赞助款不能自动当作任务付款。

## 5. 主人 QQ 通知的限制与可选降级

官方公告停止主动推送，因此无限期主动汇报目前为 Blocked。代码中的本机通知探测不代表平台支持，不能通过直接改 `owner_notification_verified=true` 假装验收通过。

用户已明确接受以下受限试运行方式，私有配置已应用并取得真实通知回执；公开模板仍默认关闭：主人先在已绑定私聊发送 `【小单】通知状态`，在 QQ 允许的回复窗口与次数内自动汇报。窗口失效或次数用尽后留在 outbox，不转发到群；主人下次发送该命令时补发。不是逐单审批。超过约 50 分钟未收到主人该类私聊时，新接单会被阻止；平台提前拒绝仍以真实回执为准。短暂通知失败不撤销在途付款。

选择受限方式后，配置 `owner_notification_mode=passive_window`、`passive_window_accepted=true`；先用受保护请求 `{"op":"owner_probe","mode":"passive_window"}` 验证真实 C2C 回执，再记录 `owner_notification_verified=true`。不应把这个结果写成主动推送成功。主动诊断请求 `active_probe` 最多人工测试一次，平台拒绝后不得持续探测绕过规则。

若仍要求随时主动通知，保持入口关闭，需先选择平台实际允许的替代通知方式。本轮未新增未授权的外部通知服务。

## 6. 本机管理和异常处理

在服务器上以 maibot 或 root 运行，设置 `PYTHONPATH=/opt/maibot/paidtasks-current`：

```sh
python -m paidtasks.control status
python -m paidtasks.control review
python -m paidtasks.control --request-file /path/to/private/request.json
```

带客户、订单和处理依据的 JSON 文件须为 600；输出也只保存于受保护位置。HTTP 回调接口不能调用这些操作，B站进程的 UID 也无管理权限。

| 操作 | 请求内容（结构示例，不是真实订单） |
|---|---|
| 暂停/恢复 | `{"op":"admin","action":"pause"}` / `resume`；恢复仍受所有门槛约束 |
| 明确拨入 ¥1 | `{"op":"admin","action":"add_budget","data":{"microyuan":1000000,"allocation_id":"pilot-20261024-user-authorized-1cny"}}`，本轮已经执行；固定编号保证重试不重复拨款 |
| 查单 | `{"op":"recheck","trade_no":"PLATFORM_ORDER"}`；订单号本身不提供认领权限 |
| 人工绑定 | `{"op":"bind_payment","trade_no":"PLATFORM_ORDER","id":"LOCAL_ORDER","evidence":{"basis":"真实客户、付款人及报价对应的核验依据","timing_reviewed":true}}` |
| 恢复成果 | `{"op":"recover_result","id":"LOCAL_ORDER","basis":"已核对保存的结果与文件哈希"}`；只恢复已有成果，不重新调用生成器 |
| 明确拒绝后重试 | `{"op":"admin","action":"retry_delivery","data":{"event_key":"EVENT_KEY"}}` |
| 发送结果未知 | `{"op":"reconcile_delivery","data":{"event_key":"EVENT_KEY","observed":"not_sent","basis":"检查原会话及平台记录确认没有送达"}}`；若已送达则 `observed=sent` 并提供真实 `remote_id` |
| 人工退款记录 | `{"op":"admin","action":"record_refund","data":{"order_id":"LOCAL_ORDER","amount_fen":100,"basis":"平台已完成退款的凭据及时间"}}` |

付款时间未知或查询异常时，过期报价和取消请求保留预算并进入核查，不武断释放。退款操作本身仍在平台后台进行，代码只记录 owner 的事后证据，不宣称自动退款或实时争议同步。

## 7. QQ 和 B站咨询、结果与记忆

QQ 使用严格前缀 `【小单】`；报价是结构化的受限任务，不把普通聊天自动变成收费订单：

```text
【小单】报价 {"task":"render_formula","arguments":{"expression":"(12+8)/4"}}
【小单】确认 本地订单号 1
【小单】状态 本地订单号
【小单】结果 本地订单号 1
【小单】验收 本地订单号
```

客户身份由原平台、机器人账号、会话和发送者组合确定；客户不能在文本中指定主人、价格、预算或他人订单。结果只发回原会话；超过消息长度用分页领取。QQ 图片使用现有上传通路，发送失败保留原 PNG 与哈希，不重新生成。

B站降级路径：owner 先 `import_bili`（customer.channel=`bilibili_manual`，同时记录真实账号/会话/客户、task、arguments、basis），得到报价草稿；人工向客户发出报价，客户明确接受后调用 `manual_accept` 记录 version、basis、customer_confirmed_at。官方核款后执行。人工发出成果后，`manual_delivered` 必须包含真实 recipient、sent_at 与 artifact_sha256，才记录 owner 证实的送达；“生成草稿”从不算平台发送。

共享活动记录只含匿名“接单、核款、交付、异常”状态；不含客户身份、绑定码、客户材料、付款账号或成果。付费发送避免进入跨渠道活动原文。原渠道自身的聊天存储受其原有权限约束，并非本模块声称删除的对象。

成果至少保留 7 天；本轮不自动清理付款/报价证据。后续清理须先导出受保护账本、处理争议及保留要求。10,000 笔订单上限会暂停新报价等待归档审查；绝不为了腾空间删除未决资金记录。

## 8. 可选 HTTPS 回调

本轮没有已验证的公网 URL，默认只用查单轮询。若以后有可用域名：

1. 将已有域名的 A/AAAA 正确指向现有 Lighthouse。检查属地与域名接入条件；不创建新云主机。
2. 在现有安全组/防火墙按需开放 80/443，保留 WebUI 仅回环访问。部署受信任来源的 Caddy，使用 `deploy/Caddyfile.example`，以域名设置 `AFDIAN_CALLBACK_HOST`。
3. 在受保护凭据文件加入随机 32 字节十六进制 `AFDIAN_WEBHOOK_PATH_SECRET`，设置 webhook_enabled=true。收单服务仍只监听 127.0.0.1:18765。路径密钥不写入访问日志。
4. 先验证 DNS、有效证书、外部 GET `/health`，再从外部 POST 测试回调，验证收到确认但没有自动认款，最终用平台自己的测试/真实通知核对。
5. 仅在验证成功后向用户报告完整回调 URL 并填入爱发电后台。没有域名、证书或外部可达证据时，不把模板 URL 当成现成地址。

这个随机路径降低扫描噪声，不能代替平台签名。所有事件仍由官方查单复核，错误路径/过大请求会被拒绝；限速、队列上限和最大 8 个处理线程防止无界处理。

## 9. 真正的 ¥1 验收顺序

当前不能开始付款。已满足：服务增量部署、¥1预算入账、主人受限通知同意及真实回执、主人私聊试运行白名单。仍须完成：官方鉴权/查单、商品/SKU核验、部署后普通聊天与B站的完整现场回归。

满足后设置 pilot，不开放 live。优先选一个 `(12+8)/4` 的确定性 PNG 任务，先报价再客户确认；核对 ACCEPTED、预算预留和主人通知回执后，才由用户在真实商品页支付 **一次 ¥1**。

采用已验证留言绑定码；若走人工归属，记录客户、付款人、订单号、报价版本及时间核对依据，再执行官方查单。必须核对 status、实付 ¥1、商品/SKU、数量一、非兑换/折扣，不能凭截图或本地造数。

分别记录支付平台回执、一次入账、一次排队/执行、结果哈希、QQ 文本和图片回执、主人通知、用户明确验收和账本变化。证据中交易号/客户身份做不可逆摘要；原凭据仅私有保存。检查服务器峰值内存、连接未断、无 OOM、磁盘增量。若任何一步失败，保持待处理，不重复付款。

真实验收及必要的平台规则确认完成后，才可使用 `动态草稿_未发布.txt`。发布后还须读回验证公开可访问链接；本轮没有发布链接。

## 10. 回滚

```sh
sudo python3 deploy/install.py --rollback /srv/maibot/paid-backups/实际备份目录
```

脚本检查没有后续代码冲突，再停止/禁用 paidtasks、恢复本次改动前的插件及 bridge 配置项，重启 MaiBot。它不删除收款数据库、成果、预算预留或发送去重账本，也不回退仍在变化的原聊天数据库。版本目录和备份保留供审查。
