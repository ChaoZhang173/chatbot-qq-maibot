# 通用语音终端网关 0.2.0 接入说明

该预览版把终端连接、设备鉴权、语音识别、流式播报与聊天后端分开，能独立安装。第三方后端实现下面的 HTTP 接口即可接入；不用安装大肥鱼、QQ 适配器或 MaiBot。设备仍需兼容现有 Passport 语音协议，固件不包含在 Python 包中。

```text
语音终端 ← WSS/PCM → Passport Gateway ← HTTPS/JSON → 你的聊天后端
                         ↕ ASR / TTS                  ↕ 身份、记忆、工具
```

## 安装与本机验收

使用 Linux 或 macOS，Python 3.11 以上；本次验证环境是 Python 3.12。Windows 未支持，设备管理工具使用 POSIX 文件锁。

```sh
git clone https://github.com/ChaoZhang173/chatbot-qq-maibot.git
cd chatbot-qq-maibot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
passport-smoke
```

`passport-smoke` 完成一次真实本机 WebSocket → HTTP → WebSocket 往返：临时生成设备凭据，上传合成 PCM，得到文本与音频帧后退出并清理。识别和合成使用模拟服务，后端只回显文本；不访问付费 API，不代表真实语音质量或设备验收。

独立安装包仅包含 `passport_gateway` 及其子模块。当前通过 GitHub 源码或自行构建的 wheel 安装，未发布到 PyPI。可用 `python -m pip wheel --no-deps . -w dist` 构建 wheel；生产安装应固定已审核的提交或包哈希。

## 必需的后端接口

管理员在网关配置 `PASSPORT_BACKEND=http`、`PASSPORT_BACKEND_URL` 和 `PASSPORT_BACKEND_API_KEY`。URL 是完整的 POST 地址。远程地址必须为 HTTPS；仅本机回环允许 HTTP。URL 内不允许账号密码、查询参数或 fragment；鉴权放在请求头中。网关不跟随重定向，也不读取环境代理设置。

请求头：

```http
Authorization: Bearer <网关与后端之间的独立令牌>
Content-Type: application/json
X-Passport-Protocol: 1
Idempotency-Key: <device_id 与 turn_uid 组合的 SHA-256>
```

请求正文：

```json
{"op":"voice_turn","device_id":"demo-device","turn_uid":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","text":"你好"}
```

`device_id` 来自已通过网关鉴权的设备，`turn_uid` 由网关生成。终端提供的角色、账号、会话等字段不进入这个接口；设备令牌也不转发。后端验证独立令牌后，以自己的设备登记表决定用户、会话和权限。不要把 `device_id` 直接当作管理员权限，也不要把多位用户放进同一个共享会话。

成功响应 HTTP 200：

```json
{"ok":true,"reply":"你好，很高兴见到你。","mood":"neutral","memory_stored":false}
```

`reply` 必须是非空字符串。`mood` 可用 `neutral`、`pleased`、`gentle`、`serious`、`puzzled`；缺省或未知值按普通语气处理。`memory_stored` 是可选的布尔值，只有实际写入记忆后才返回 true；`persona_version` 是可选整数。整个响应上限 64 KiB，建议回复简短：终端字幕目前只显示前 600 字节，语音还受合成时间限制。

已识别的业务错误仍使用 HTTP 200：

```json
{"ok":false,"error":"voice_unbound"}
```

| 业务错误 | 设备结果 |
| --- | --- |
| `voice_unbound`、`voice_disabled`、`voice_binding_changed` | 身份未绑定或权限已变更 |
| `voice_busy` | 繁忙 |
| `voice_timeout` | 后端超时 |
| 其他错误 | 通用处理失败，不转发后端原始诊断 |

HTTP 401/403 表示网关与后端的配置鉴权失败；429/503 映射为繁忙，408/504 映射为超时，其他非 200 响应映射为失败。请求总时限为 25 秒，建议后端在 22 秒内结束。连接超时、取消、断线或超时后网关不会自动重发聊天请求。HTTP 取消不能保证后端停止执行工具，后端应在执行写操作前持久化去重状态，并按设备和 `turn_uid` 返回已知结果，避免重复副作用。

## 接入示例与配置

安装后运行：

```sh
# 这里生成的是示例后端的独立令牌；请保持私密。
export PASSPORT_BACKEND_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PASSPORT_DEMO_DEVICE_IDS=demo-device
passport-demo-backend --port 8766
```

示例只监听本机，执行鉴权和设备白名单检查后回显输入；没有大模型、长期记忆或工具。实现见 `passport_gateway/demo_backend.py` 的 `do_POST`：把回显部分替换为你的后端调用，并自行实现会话映射、权限和持久化去重。自定义后端不必用 Python，只要遵守 JSON 合约即可。**普通 Chat Completions API 不能直接填入此地址**，需要适配上述请求与响应格式。

正式运行参考 [配置模板](../config/passport-http.example.env)：提供自己的 TLS 证书、设备绑定文件、后端令牌和语音服务密钥。配置模板默认监听本机；终端跨设备连接时填写实际监听地址，并保持 TLS。设备凭据用 `passport-device` 管理；令牌只显示一次，撤销或轮换对已连接设备也生效。语音提供商、模型、音色和风格均可配置；示例清空了原实例的风格提示。

网关不复制大肥鱼的人设、私人记忆、账号或授权。定制人格、跨渠道记忆、联网搜索及工具能力来自你的聊天后端。

## 可选主动提醒

普通 HTTP 接入默认不轮询提醒。仅当后端实现了全部提醒操作，并有支持提醒协议的终端时，设置 `PASSPORT_HTTP_REMINDERS=1`。当前网关认可标准和定制的 0.3.0/0.3.1 固件标识；其他终端需要实现相同扩展并自行增加能力识别。

以下操作 POST 到同一地址，使用同一鉴权和协议版本头，正文包含 `op`、`device_id` 及表中字段。所有成功响应为 HTTP 200 且包含 `ok: true`。

| op | 额外请求字段 | 成功响应字段与含义 |
| --- | --- | --- |
| `reminder_seen` | 无 | 确认此设备具备提醒能力；不能据此创建提醒 |
| `reminder_claim` | 无 | `job: null`，或 `job: {id, token, expires_at}`；领取独占租约，过期时间为 Unix 秒 |
| `reminder_prepare` | `id`, `token` | `reply`, `mood`；生成本次提醒内容 |
| `reminder_arm` | `id`, `token` | 在播放前持久化不可重复播放的边界 |
| `reminder_report` | `id`, `token`, `outcome` | `outcome` 为 `delivered`、`defer` 或 `unknown`；幂等记录实际结果 |

后端自行负责自然语言预约、时区、取消/改期、任务持久化、设备归属检查、租约过期和并发。播放前未拿到设备确认时不能记为已送达；已进入播放边界却丢失回执时应记未知并避免重放。报告操作可能重试一次，因此必须幂等。此扩展保留现有大肥鱼适配能力，没有为所有第三方后端提供完整提醒调度器。

## 已有大肥鱼实例

默认 `PASSPORT_BACKEND=unix` 继续使用现有本机套接字与服务端身份验证，保持原有 Planner、Replyer、记忆和工具流程。这种接法仍依赖 MaiBot Host 补丁和统一核心。线上源码快照是 `3f2b20d`；通用版本单独提交，本次没有更新线上服务或刷写终端。

## 验证范围

参见 [验证记录](PORTABLE-GATEWAY-VALIDATION.json)。测试覆盖 HTTP 合约、凭据边界、错误、超时、取消、提醒开关和完整本机协议链。真实语音提供商、不同硬件和第三方聊天系统尚未逐个联调，因此版本定位为预览版。
