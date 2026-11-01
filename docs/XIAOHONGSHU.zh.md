# 大肥鱼小红书执行器：运行说明

状态：实现已部署并完成真实提交与本账号页面读回检查；这些检查不等于其他账号可见或审核通过。示例开关仍默认关闭，示例配置不含账号、令牌、Cookie 或服务器地址。真实参数只放部署私有配置，不进入源码、示例和报告。

平台审核按具体评论记录：用户反馈“评论审核中暂时无法分享”时记录为审核中；其他账号看不到但没有审核提示时，只记录对外可见性异常，不猜测原因或期限。已提交、审核中或本账号已读回的评论不因缺少外部可见性而自动重发。当前运行策略允许其他发帖、评论和回复照常进入既有审批流程，不因个别评论审核而开启全局暂停；人工暂停开关仍独立有效。

2026-11-01 回复与查询修复：主人私聊里的“草稿 ABC234 v1 发成功了吗”“能补发一下吗”，以及能唯一匹配草稿的内容片段，会进入真实操作流程。状态查询读取账本；未确认状态可排队执行一次页面复查并回报。明确补发只授权原文、原目标的一次新尝试：先检查目标评论线程，已存在则只更新状态，读取失败或不完整则不发送。旧记录保留，新尝试独立计入原有额度；同一消息重复投递、重启或重复领取都不会多发。每份草稿最多两次主人明确请求的补发，绝不自动重试未知结果。

网页运行时补丁将回复按钮滚动到中心、验证可点击后只点击一次；各步骤有短超时，提交前失败通过阶段标记明确回报，提交后的异常仍为未知。核心在创建本机 socket 后为配置的执行器 UID 设置精确连接权限，替代会因目录变化过多而失效的目录监听方式。

## 人工审稿（强制，无开关）

当前实现要求每次公开发送都经过主人审批：所有发帖、主动评论、回复先成为核心里的持久待审草稿（dry-run 不是草稿的替代品），只有真实账号主人在已验证的 QQ 私聊里发出明确命令才能批准。

### 主人入口怎么配置（核心私有 policy.json）

- `xhs_review_owner_session`：主人 QQ 私聊的 session；`xhs_review_owner_actor`：主人在该私聊里的用户 ID。两项**必须显式配置**，不会自动选“第一个主人”，也不会广播给多个主人。
- 每条指令都会重新校验：配置的 session+actor 完全匹配，**且**既有 `commands.owner`（`qq_owner_bindings`）通过，**且**该会话是 `private_access` 里已验证绑定、绑定的私聊 ID 就是该 actor（因此需要 `qq_private_familiar_enabled` 为 true）。群聊、未绑定私聊、第三方、机器人自己的回声、引用/转发里的文字、过期（超过5分钟）或没有消息 ID 的消息、重复投递的同一条消息，一律忽略或只执行一次。
- 没配置或校验失败时：仍然可以生成草稿，但**不推送、不批准**。
- 还需要：`xhs_enabled`、`xhs_bot_account`、`xhs_uid`（执行器账号的 UID，只能调用 `xhs_*`）。示例见 `config/xiaohongshu-core-policy.example.json`。

### QQ 私聊指令（精确语法）

```
小红书帮助
小红书草稿                    列出待处理草稿（最多5条）
小红书草稿 <ID>               查看完整草稿：标题/正文/标签、配图信息、目标笔记ID与链接、对方评论原文、AI说明、批准命令
小红书批准 <ID> v<版本>       批准该版本（必须带版本号，且必须是最新版本）
小红书拒绝 <ID>
小红书修改 <ID> 标题：<新标题>
小红书修改 <ID> 正文：<新正文>
小红书修改 <ID> 标签：<标签1>,<标签2>
小红书确认首帖 <ID>           首帖显示正常、但读回没能确认时，由你确认它算成功
```

- 批准绑定**不可变**的内容：绑定账号、草稿 ID、版本、正文/标题/标签、素材（真实图片按文件哈希；文字卡按模板版本+标题+首句）、目标（笔记 ID、评论 ID、评论作者、评论原文哈希）、可见范围。**修改后是新版本，旧批准作废，必须重新批准**。批准 6 小时内有效，草稿 24 小时过期；一次批准只会被领取一次。
- 执行器（xhsbot）可提交草稿、领取已批准草稿、回报结果、领取和回报只读复查，**没有批准或授权重发的操作**；领取后重新校验账号、整份内容哈希、素材、目标、安全规则和现有额度，任何一项不符都不发。失败/结果未知**不自动重试**；只有主人明确的新补发请求才能授权另一次尝试，且执行前必须完成查重。
- 推送：新草稿只推给配置的主人私聊，每个版本最多一次（改过的旧版本不再推）；每小时最多推 3 份草稿，不催审；按平台回执 `sent` 判断是否送达，抛异常记“未知”，`sent=false` 记“未送达”，都不重发。待审队列最多 5 份，满了执行器不再花模型额度起草。
- 发送额度（上线配置，如每天 2 条、间隔 1 小时）仍在执行器按**真实发送**计数（已有的私密验收帖也算）；起草不受发送额度限制，发送时才受限，额度未到时已批准的草稿保持有效不被消耗。

### 说人话也行（自然语言审稿）

固定指令一直可用（包括不带空格的 `批准发布ABCDEFv1`、`小红书批准ABCDEFv1`、`拒绝ABCDEF`）。在已验证的主人私聊里，也可以直接说：

- **批准**：“好的看起来没问题，发布吧”“可以发布”“批准发布”“不用改，可以发布”。
- **拒绝/先不发**：“不要发了”“拒绝”“不批准”→ 拒绝；“先别发”→ 保持待审，**如果已经批准但还没被领取，会真的撤回批准**。
- **修改**：“需要修改”“改完再发”→ 她会问想怎么改，草稿保持/回到待审；“正文简短一点”“语气自然些”→ 她按人格和公开写作规则重写，**新版本 v+1、旧批准作废，要你看过后重新批准**（具体改稿会先撤回已有批准，再去改，改写失败或超时也保持未批准）；“正文改成：……”“标题改成：……”→ 直接替换（不调模型）。
- **查看/状态**：“看看草稿”；“发布的怎么样了？”“发出去了吗”→ 只读真实记录回答：待审＝还没批准也没发；已批准＝在等执行器领取；执行中＝已经不能取消；已受理＝平台受理但读回还没确认；已读回＝自己页面读回确认（不代表别人一定看得到）；未知＝结果未知，不会重发。

带“小红书”前缀的自然语句（如“小红书这篇可以发布了”“小红书这篇先别发”）按自然语言处理；只有真正的旧命令形态（`小红书草稿`/`帮助`/`批准 ID vN`/`拒绝 ID`/`修改 ID …`/`确认首帖 ID`）才走命令处理。明确指向草稿的开放式修改（“把这篇正文写得更活泼一点”“这篇内容再丰富一些”）同样会改写成新版本并撤回旧批准；论文、代码、邀请、邮件等其他话题的类似措辞不受影响。

自然语言的规则：

- 只认**主人自己输入的文字**；被引用、转发、卡片里的文字不当指令。QQ 里“回复某条消息再打字”是正常用法：你打的字有效，引用的内容无效；只引用不打字、或你只打“好的”而引用里有批准指令，都不会批准。
- 必须有**明确的审稿参照**：一份你**真的看到过**（推送或查看回复成功送达你的聊天）且之后没变过的待审草稿。只有一份时，“发布吧”批准它；有两份以上看过的，会问“哪一篇”，不会替你选；没有就请你先看一份。草稿在你看过之后被改了（或批准/过期），旧的“看过”不再有效。升级前已确认送达的首稿推送算看过，不用重看或重批。
- 否定、疑问、假设、条件、引用不会被当成批准：“不批准/不同意发布/不通过/不是说可以发布/可以发布吗？/如果没问题就发布/改完再发/“发布吧”是什么意思”都不会批准（“没什么问题，发布吧”“不用改，可以发布”这类肯定说法仍然有效）。拿不准时她会问清楚，草稿保持原状。
- 和审稿无关的聊天原样放行：“需要修改代码”“拒绝别人的邀请”“修改论文”“发布会什么时候开”都不会被拦截或误改草稿。
- 少量说法规则判断不了时，会用一个有边界的小模型判断（严格输出格式、绑定到你刚看的那一份草稿和版本、置信度要求、每天次数上限），批准类结论还要再过一遍否定/疑问筛查；你在它思考时又发了新消息、草稿被改或被拒，旧的批准一律作废。
- 已被领取/发送中/已受理/结果未知的草稿不能取消，也不会被说成“没发”。

### 恢复一次已经明确、但被漏掉的批准（仅限运维，本机手工）

如果你已经发过明确的 `批准发布<ID>v<N>`，但当时没有被识别，运维可以在核心所在机器上，基于**库里已有的那条真实私聊事件**恢复，而不是让你重新批准、也不会伪造一条消息：

```
python scripts/xiaohongshu_recover_approval.py --core-root /srv/maibot/unified --event-id evt:<64位十六进制> --draft-id <ID> --version <N>
```

它自己读取事件并校验：配置的主人账号与私聊、当前 QQ 账号、事件有效且是私密外部用户消息、文字是精确的批准语法并对应这份草稿和版本、消息不超过 1 小时、发生在草稿创建之后且在过期之前、草稿仍是同一版本且哈希没变、该消息此前没被实时处理或恢复过；并且**主人之后如果又有撤回/暂缓/拒绝/修改/存疑或再次提到这份草稿的消息，则不恢复**。通过后批准按**原消息时间**计有效期（6 小时），保留来源事件；重复执行无害。任何一项不满足只返回有界的原因，不猜、不批准。它不是执行器能调用的操作，也没有 socket 接口，不会自动运行。

### 首帖

在“真的记为成功”（读回确认，或你用“确认首帖”）之前，只会起草一篇公开笔记：**自我介绍**，按同一个人格写，自然地说自己是 AI 做出来的虚拟角色（核心会拒绝说明书腔、声明腔），主线是游戏、动漫和她的小想法，不承诺涨粉。首帖已在待审、已批准、发送中、已发出但未核验（delivered/unknown）时，不会再起草第二篇介绍；其他公开笔记要等首帖成功后。仅自己可见的验收帖不算首帖；首帖状态按当前绑定账号计算。

### 自我验收（`--self-only-acceptance`）

只给**我们自己用仅自己可见创建、并已严格读回的那一篇笔记**免审直发（评论、回复自己的评论、再发私密笔记）：执行器账本里必须有 `audience=self_only` 且读回确认的对应记录。没有“任意登记”这种入口；读回还没确认（pending）的私密帖不算，此时同样的文字不加选项会成为普通待审草稿。该模式永远不能公开发布，产生的评论在核心里是 self_only，不进入公共记忆。

## 组成与边界

- `extensions/xiaohongshu/`：独立 Python 执行器，不含人格、记忆或第二套模型凭据。
  - `config.py` 配置校验；`http_client.py` 只连明确配置的回环服务，固定路由，不跟随重定向，响应有界；`core_client.py` 经 Unix socket 请求统一核心，核心不在线则不写。
  - `store.py` SQLite 账本与单实例锁；`media.py` 可发布素材清单；`textcard.py` 原创文字卡；`runner.py` 身份绑定、通知回复、自主循环；`cli.py` 命令行。
- 核心 `maibot_unified_core/xiaohongshu.py`：只有三个操作 `xhs_event`、`xhs_plan`、`xhs_activity`。核心唯一的人格/模型/记忆权威不变。
- 外部帖子和评论只是资料，不能改人设、权限或额度；`xsec_token`、Cookie、二维码、服务令牌从不进入模型或跨平台记忆。

## 目录与权限（默认，同机部署）

根目录 `/srv/maibot-xiaohongshu`（专用账号 xhsbot）：`state`（账本，0700）、`publish-media`（可发布素材）、`cache`、`bin`、`home`、`credentials`（环境文件，0600）。外部 MCP 固定版本，监听 127.0.0.1:18060，开启 AUTH_TOKEN，Cookie 路径独立；本地控制端口不得对公网开放。浏览器运行串行并限制资源（2 GB 内存机器）。

执行器的服务令牌由环境变量 `XHS_MCP_AUTH_TOKEN`（或 `token_file`，必须是 0600 普通文件）提供，同一个受限 EnvironmentFile 也给 MCP 的 AUTH_TOKEN，不写入配置或日志。

### 核心 socket 权限

核心目录与 `core.sock` 属于既有组。部署只给 xhsbot “穿越父目录 + 连接 socket”的 ACL，不把它加入能读其他数据的组。当前核心在创建或重建 socket 后会为配置的执行器 UID 恢复精确连接权限；部署时仍需准备父目录穿越权限并安装 `setfacl`。权限恢复失败时执行器按“核心不可用”拒绝写入，不会绕过核心发送。若实际服务使用 `PartOf=maibot.service`，停止核心会连带停止执行器，启动核心后还应确认执行器已单独启动；模板不能替代实际服务及其附加配置。

核心侧按对端 UID 限制：策略里的 `xhs_uid` 只可调用 `xhs_*` 操作，调用 B站或管理操作会被拒绝；其他账号调用 `xhs_*` 也被拒绝（见 `service.authorize_peer`）。核心策略示例见 `config/xiaohongshu-core-policy.example.json`（`xhs_enabled` 默认 false；账号与 UID 在私有 policy.json 填写）。

## 首次绑定与登录

1. 私有配置 `bound_uid` 填写预期账号 UID，其余开关保持 false。
2. `python -m xiaohongshu --config <配置> doctor`：显示服务可达、是否登录、观察到的 UID 与是否等于配置。外部工具只在能读到真实 `user_id` 时才算已绑定；只有昵称、UID 不明或与配置不符都视为未绑定，**停止一切写操作**，UID 不符还会写入暂停状态，需 `resume`。
3. 未登录时：`qrcode --out <绝对路径>` 把二维码保存为 0600 的任务文件供人扫码，不输出图片内容或 Cookie。
4. 扫码后重复 `doctor`，确认 `uid_matches_config` 为 true。

## 运行与开关

- 开关（`enabled`、`publish_enabled`、`comment_enabled`、`reply_enabled`、`browse_enabled`、`autonomy_enabled`）、额度和 `dry_run`、`visibility`（自主发帖固定可见范围，外部内容不能改写）来自**进程启动时读取的配置**；修改后需重启服务才生效。
- 实时控制：`pause` 创建 `state/PAUSE`，所有写入在最后一刻前再检查，已起草或正在核验身份的操作也会被拦下并撤销预留（不记为 unknown、不占额度），消息保留待处理。`resume` 删除 PAUSE 并请求清除暂停/退避，由运行中的单写者在下一周期应用。
- `status`：本地计数、暂停与待核验数量；服务运行时也可用（只读）。
- `poll-once [--dry-run]`、`autonomy-once [--dry-run] [--force]`：单次通知轮询/自主考虑。dry-run 读取并请求核心决策（计模型预算），但没有任何平台写请求、不写账本、不标记已处理。
- 手动验收：`publish --title --content --image-id [--tag ...] [--visibility 公开可见|仅自己可见] [--dry-run]`、`comment --feed-id --text`、`reply --feed-id --comment-id --text`（目标的 `xsec_token` 从环境变量 `XHS_TARGET_TOKEN` 读取，避免进入命令行）。手动调用仍检查身份、额度和开关，只跳过最小间隔。回复必须给 comment_id，从不只用 user_id 推测目标。
- 除 `status`、`pause`、`resume` 外，命令需要写锁；服务运行时先停服务。

手动命令（`publish`/`comment`/`reply`）**默认只生成待审草稿，不会发送**。自我验收见上面“人工审稿”一节：`--self-only-acceptance` 只对执行器自己创建并读回确认的仅自己可见笔记生效，且只对手动命令有效（通知和自主流程仍拒绝回复自己）；`deliver-once` 手动领取并发送一份已批准草稿（服务本身每个周期也会做）。

建议验收顺序：先用 `--visibility 仅自己可见` 发一篇，再在自有评论区做回复；之后才打开自主开关。

## 通知、去重与额度

- 首次启动以当前时间建立通知基线，历史消息只标记已处理。之后按评论 ID 去重，自己的评论和与自己发过的文字相同的回声不会回复。
- 通知目标不在已加载评论里时做一次有界的深度加载；仍找不到则延后，最多重试 3 次（间隔递增）才最终放弃，不会永久当作已处理。缺少帖子引用或 token 的通知同样限次重试。
- 日额度、模型调用额度（发帖草稿后核心还会审核，所以一次发帖预留 2 次；一次可能走到发帖的自主尝试需要 3 次余量）、每讨论串/每作者额度、最小间隔、退避、暂停全部由执行器强制。失败、unknown、超时都计入尝试；浏览次数在请求前占用。北京时区。活跃时段 `active_start_hour`–`active_end_hour` 只限制自主动作。
- 验证码、登录失效或平台限制：暂停等待用户，不绕过验证。

## 写入账本与结果表述

网络写请求前先落盘并预占额度；请求超时、连接中断或进程崩溃后的结果记为 unknown，**绝不自动重发**；重启把遗留 sending 标为 unknown。只有“提交前即可确定未发出”（连接失败、服务拒绝前置校验）才可再次尝试（有次数上限）。

工具返回成功只表示受理或本账号页面看见，不等于公开可见：

- 发帖：用作者、标题、完整规范化正文（仅允许去掉已知标签显示）、时间和发布前的帖子快照做读回，唯一匹配才记为 `own_page_exact`。
- 评论根评论的直接子评论、顶层评论：可按作者、内容、时间和预先快照核验。
- 对子评论的回复：上游评论结构没有“回复目标”字段，最多记为同线程读回（`same_thread`），按“待核验/工具受理”同步，不记 succeeded。
- 回执同步给核心时 `public_visibility` 恒为 `not_independently_checked`；“仅自己可见”的帖子不进入公共记忆。核心上下文提示：accepted/pending/unknown/failed 的操作不得在后续帖子中称为成功或所有人可见。

## 素材与原创文字卡

- 预授权素材：`publish-media/manifest.json` 必须声明 `"public": true`，条目为 `{"id","file","caption","publishable":true}`，只接受 png/jpg/webp、≤5 MB、文件头匹配的普通文件；符号链接（文件或任一级祖先目录）、路径穿越、私有图库路径一律拒绝；条目文件缺失只跳过该条目。禁止自动搬运 QQ/B站图库。真实素材在 `limits.image_reuse_days`（默认 30 天，0 关闭）内只要有过可能已到达平台的发帖尝试（含 unknown）就不再作为候选，发布前和预占额度时也会再次拒绝；文字卡每次生成新的唯一 id，不受此限。
- 文字卡（`text_card.enabled=true` 且 `font_path` 指向固定字体）：核心批准标题与正文后，用 Pillow 以固定尺寸渲染标题与正文首句，生成带唯一标识的 `card-<hash>.png` 并登记入清单，说明为“原创文字卡”，不是真实照片，不调用生图模型。默认关闭；开启时 systemd 需给素材目录写权限。
- 没有可发布素材（也没开文字卡）时记录原因并跳过发帖。

## 日志与隐私

账本只存本账号发布的文本摘要、目标 ID、状态与素材 ID；不存 `xsec_token`、Cookie、服务令牌或素材路径。核心只收到公开文本与 ID。

## 回滚

1. `touch state/PAUSE`（或停止服务）→ 2. 核心策略 `xhs_enabled=false` → 3. 删除 ENABLE 标记并 `systemctl disable --now`。账号内容的删除需人工在平台操作；账本保留审计。

## systemd 示例

模板文件：`deploy/xiaohongshu-executor.service`（执行器）和 `deploy/xiaohongshu-mcp.service`（外部 MCP 运行时）。**默认不启用**：缺少 `ENABLE` 标记时单元拒绝启动，`[Install]` 留空。

- 执行器用系统 `/usr/bin/python3`（xhsbot 无权访问私有 Python）和独立安装目录 `/srv/maibot-xiaohongshu/app`（把 `extensions/xiaohongshu` 拷到其下的 `xiaohongshu/`）；文字卡需要系统 python3 安装 Pillow 并在私有配置里写字体绝对路径。
- MCP 运行时模板：`TasksMax=512`、`MemoryMax=600M`、`MemorySwapMax=512M`、`CPUQuota=100%`。真实启动发现 `TasksMax=128` 会让浏览器的 Go 运行时 `newosproc errno=11` 崩溃；二维码仍在等待扫码时，不要并发运行 `doctor`/`status` 等会启动浏览器的请求，先确认 Cookie 文件已更新，再查一次身份。
- 下面的片段只是执行器模板的摘录，以 `deploy/` 文件为准。

```ini
# 摘录（路径以 deploy/xiaohongshu-executor.service 为准）
ExecStart=/usr/bin/python3 -m xiaohongshu --config /srv/maibot-xiaohongshu/config.json run
Environment=PYTHONPATH=/srv/maibot-xiaohongshu/app
```

完整旧示例（保留以便对照）：

```ini
[Unit]
Description=Dafeiyu Xiaohongshu executor
After=network.target
ConditionPathExists=/srv/maibot-xiaohongshu/ENABLE

[Service]
Type=simple
User=xhsbot
Group=xhsbot
EnvironmentFile=/srv/maibot-xiaohongshu/credentials/xhs.env
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONPATH=/srv/maibot-xiaohongshu/app
WorkingDirectory=/srv/maibot-xiaohongshu/home
ExecStart=/usr/bin/python3 -m xiaohongshu --config /srv/maibot-xiaohongshu/config.json run
Restart=on-failure
RestartSec=30
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictNamespaces=yes
SystemCallFilter=@system-service
CapabilityBoundingSet=
ReadWritePaths=/srv/maibot-xiaohongshu/state
ReadOnlyPaths=/srv/maibot-xiaohongshu/publish-media /srv/maibot-xiaohongshu/app
RestrictAddressFamilies=AF_UNIX AF_INET
IPAddressDeny=any
IPAddressAllow=127.0.0.1
MemoryMax=256M
TasksMax=64
CPUQuota=50%

[Install]
# 有意留空：验收完成前不随开机启动。
```

开启文字卡时，把 `publish-media` 从 `ReadOnlyPaths` 移到 `ReadWritePaths`。

## 离线验证

`extensions/tests_xiaohongshu/` 使用 stub 服务与 stub 核心，不访问网络（一个 HTTP 客户端用例只监听回环）。入口见 `scripts/run_current_tests.py`。离线验证不等于真实小红书验收。
