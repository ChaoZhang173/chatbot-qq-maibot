<p align="right"><strong>简体中文</strong> · <a href="PASSPORT-IDENTITY.md">English</a></p>

# Passport 按人识别身份（档案、设备、可选的已验证QQ关联）

状态：已部署；当前主人设备的正常语音回复和称呼已由用户确认。朋友设备和真实 QQ 关联仍未实测，见末尾与[本次更新记录](UPDATE-20261103-identity-memory.zh.md)。

## 模型

* **档案（profile）**是服务器端的小型人员记录：稳定随机ID（`pf_<16位十六进制>`）、显示名、可选的“称呼偏好”和角色。档案
  只能是 `friend`。**主人不是档案**：现有主人设备仍走旧的 `voice_bindings.json`（`owner_private_id` + `session`），其身份、记忆、
  权限和完整 Maisaka 规划/工具/回复链路保持不变。
* **设备**（唯一ID + 网关令牌）由管理员**分配**给一个档案；一个档案可有多台设备。设备永远不发送档案、QQ号、会话或角色：线路协议
  不变（`{device_id, turn_uid, text}`），多余字段一律拒绝（`unexpected_fields`）。网络、Wi‑Fi、IP、MAC、显示名都不参与。
  仅有设备ID不算证明（先校验网关令牌）。
* **QQ可选。**没有QQ的人通过Host里真实的 `passport` 平台私聊会话对话（用Host的 `chat.open_session` 打开，scope=`passport`，
  用户=档案ID），不伪造任何QQ账号。
* 显示名和称呼只是**展示**。即使选了“主人”这两个字，也不会改变角色、工具、会话或记忆（有测试）。

## 文件

| 文件 | 写入者 | 用途 |
| --- | --- | --- |
| `<root>/voice_bindings.json` | 运维（保留旧绑定；可选称呼偏好） | 设备→主人私聊会话；可选 `"preferred_address": "主人"` |
| `<root>/passport_identity.json` | 仅 `passport_admin`（0600、原子写、保留 `.bak`） | 档案、设备分配、QQ关联 |
| 网关 `bindings.json` | `passport_gateway.tokens` | 设备**凭据**（令牌哈希+启用）；不含人员/QQ/角色 |

同一设备同时出现在两个身份文件中属于歧义，按拒绝处理。`passport_identity.json` 损坏时所有档案路由一律拒绝，主人设备不受影响（不同文件）。

## 管理流程

所有修改命令默认只是演练，加 `--apply`（顶层参数，写在子命令之前）才会写入。

```
# 0. 凭据（与身份分开；令牌只显示一次，未加 --rotate 不会替换已有凭据）
python -m passport_gateway.tokens --bindings B --device-id dev-xxxx
# 1. 创建档案（QQ号可选，只记为“待验证”；称呼可选）
python -m unified_core.passport_admin --root R --apply create-profile --name 小红 [--address 称呼] [--qq 号码]
# 2. 把设备分配给这个人
python -m unified_core.passport_admin --root R --apply assign-device --profile pf_... --device dev-xxxx [--reassign]
# 查看（号码打码、只显示凭据状态、无哈希）          校验（重新检查QQ关联）
python -m unified_core.passport_admin --root R list --bindings B       python -m unified_core.passport_admin --root R validate
# 其他：unassign-device / disable-device / enable-device / disable-profile / enable-profile / rename-profile / set-address
```

无需重启：每个回合都会重新读取身份文件。

## 可选的QQ关联

1. 记录号码（`--qq`、`set-qq-pending`）只是管理备注，不授予QQ历史、主人权限、发送权限或会话访问，此人继续使用Passport原生会话。
2. 激活需要同时满足：此人已完成现有“熟人核验”（私聊发 `验证熟人`，再在授权群发 `私聊核验 <码>`），因此存在已验证且已登记会话的
   `qq_private_bindings` 记录；该账号不是主人；一个私聊绑定最多属于一个档案；**有可信证据证明该账号就是待验证号码**；以及管理员显式命令：
   `verify-qq --profile pf_... --private-id <id> --confirm-number 号码`（管理员需重新输入号码）。
3. 证据（仅此两种）：已验证账号的平台ID（私聊ID或其群内actor ID）等于该号码；或运维在 `policy.json` 中把号码映射到该私聊ID
   （`"passport_qq_number_evidence": {"<号码>": "<private_id>"}`）。ID不透明又没有映射时，号码保持“待验证”：不能把别人的已验证ID挂到某个号码上。
4. 激活后，该档案的Passport回合使用她真实的QQ私聊会话（与她的QQ聊天共享历史），权限仍是**friend**，绝不会通向主人。每个回合都会重新校验关联，
   失效即拒绝（不会悄悄回退）。`unlink-qq` 回到原生会话。旧原生历史仍保留，不会自动迁移到 QQ。

## 权限（审计的是真实路径，不是提示词）

* 路由与角色只来自服务器文件。插件把解析出的路由和设备记入当前回合的作用域；每次钩子/工具入口都会重新解析设备（`scope_for`），
  设备被撤销/禁用/改派、QQ关联变化或主人绑定被撤销时，立即停止派发。
* 插件工具默认仅主人可用（`voice_scoped`）；只有上下文钩子以及 `public_web_search`、`public_web_read`、`unified_memory`、
  `unified_activity`、`unified_persona` 对朋友开放。B站操作/设置、图片导入/发送、图库均仅主人可用，且不对 `passport` 平台声明。
* Host 在其工具注册表（列出、子代理授权、调用）对朋友路由强制**白名单**：内置工具名必须来自内置Provider，插件工具名必须来自
  `local.unified-core`，MCP/Agent 工具永远无法匹配。这会拦住Host里不分范围的 `query_person_profile` / `query_memory`、子代理、表情/图片/转发工具。
  受限路由没有白名单时，能力会直接拒绝。
* 朋友的上下文、记忆检索、活动与回忆只使用该人自己的会话（`state.allowed`，平台 `passport`），不使用共享群回忆和主人规则；
  朋友不能读取主人的历史/配置、管理机器人或批准公开发布。语音交付仍被捕获（不会经QQ发送）。
* 持久化/来源：事件保留真实平台（`qq` 或 `passport`）、actor、`profile_id`、路由类型和 `channel=device_voice`。

## 重放、改派与回合中途变化

* 幂等记录、执行预留、记忆任务和事件ID按档案加命名空间（主人保留历史键）。设备从A改派给B，不会重放A的结果、历史、待处理回合或记忆；
  改派后同一turn id对B是全新执行；改回A只会重放A自己的记录。
* 在每次等待（幂等锁、会话锁、朋友名额）之后、Host返回之后、记忆同步等待之后都会重新校验路由。权限已变化时不发布答案
  （`voice_binding_changed`，设备端显示为 `backend_unbound`），持久化预留会拒绝重跑，避免重复工具副作用。若变化发生在记忆同步等待期间，已经写入的记录仍归属原人员；不向改派后的身份交付该回复。
  朋友已完成的回合在档案仍启用时，仍会把记忆同步到其自己的档案聊天。
* 并发：同一个人会话一次只跑一个回合（3秒后 `voice_busy`）；朋友共用2个名额池（Host为3个名额中的2个），主人始终留有名额。

## 称呼偏好

档案上可选 `preferred_address`（1–16个字母/数字/汉字/空格/-/_；`--address`、`set-address`），旧主人条目上也可设置。仅用于Passport语音：
作为明确偏好写入语音提示，并作为Host语音消息的昵称。真实QQ昵称/账号和QQ聊天不变；未设置则保持原有行为。

## 此处未验证

第二台实物、朋友独立 Passport 会话在真实 Host 数据库中的行为、朋友的实际语音表现、真实 QQ 核验流程尚未实测。已有主人设备的回复和称呼已确认；部署时的插件能力声明已核对。

公开目录 `extensions/maibot_unified_core` 安装后使用 `unified_core` 包名；以上命令在安装后的环境执行。示例不包含个人号码、设备令牌或生产绑定。
