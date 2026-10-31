# Token 优化候选实现说明（2026-10-31）

状态：**候选补丁，未部署，未在线上验证，不承诺任何节省比例。** 基线为 `ca9ac1432ac59d08589ed00835253789587a7459`。方案背景见 [优化评估](TOKEN-OPTIMIZATION-20261031.zh.md) 与 [Opus 评审及复核](CLAUDE-OPUS-REVIEW-20261031.zh.md)。Host 改动以 `patches/token-optimizations.patch`（含 SHA256 清单）提供，不改旧补丁和旧清单。

## 开关与默认值

所有候选开关**默认全部关闭**，并且 fail-closed：指定的配置文件读不到或不是 JSON 对象；某个 section 不是对象；section 内**任何已知字段非法**（类型错误、非有限数、非正数、整数字段给了 0.5 这类非整数）；或单项环境变量无法解析——都会使该 section（配置文件损坏时为全部 section）整体回到默认的关闭状态。单项环境变量（`…=1/0`）只能覆盖有效配置的 `enabled`，不能绕过上述规则。P2/P3 的 `policy.json` 设置同样规则（`refresh_messages`、`short_description_chars` 必须是整数）。建议灰度顺序：先只启用 P4，观察日志和事实更新后再评估其它项，每次只开一个。

| 项 | 开关位置 | 默认 |
|---|---|---|
| P4 人物事实去重 | Host：环境变量 `MAIBOT_TOKEN_OPT_PERSON_FACT_DEDUP=1`，或 `MAIBOT_TOKEN_OPT_CONFIG` 指向的 JSON（示例 `config/token-optimizations.example.json`） | 关 |
| P1 回复后本地结束 | Host：`MAIBOT_TOKEN_OPT_REPLY_TERMINATION=1` 或同一 JSON | 关 |
| P2/P3 分层与工具目录 | 统一核心 `policy.json` 的 `token_optimizations.context_layers` | 关 |
| P0 费用报告 | 离线脚本，不影响运行 | — |

## P0 费用与归因

`python scripts/token_cost_report.py --pricing <定价JSON> --input <用量> --kind jsonl|json|csv|sqlite [--since --until --format json|text]`。代码在 `extensions/token_cost/report.py`，定价示例 `config/token-pricing.example.json`（`models` 故意留空，未填价格就是“未定价”，不是免费）。

- 按每次调用的 hit/miss/output 计算；输入总数与 hit+miss 的关系按 Host 记录定义（只上报 hit 时 miss=prompt−hit，其余必须相等）。缺失、bool、负数、非整数、非有限值、总数不一致的用量记为无效，不计 0 也不标 exact。
- 时区取定价配置；无时区的时间戳按该时区解释。峰谷、工作日/非工作日、节假日和调休只来自配置；日期不在配置声明的已核实范围内时给出工作日/非工作日两种口径的区间并标记估算。不内置节假日。
- 缺少 cache 记录时按“全命中/全未命中”给区间。所有调用都未定价时总额显示 UNKNOWN，不显示 0。
- since（含）/until（不含）对所有格式统一生效，必须有明确时区。数据库只读打开；历史原始 `cost` 字段原样保留，不追写。
- 新增日志仅为结构化计数/原因/指纹：`turn_structure`（批次计数与触发类型）、`person_fact_writeback_attribution`（动作、原因、人物指纹、证据键前缀）、`reply_termination`（决定与原因代码）。不记录聊天正文或密钥。

## P4 人物事实写回去重

入口：`host/src/services/memory_flow_service.py` 的 `PersonFactWritebackService._handle_message`（发送分段触发的现有路径）。缓存 `services/person_fact_dedup.py`，进程内、有 TTL 与容量上限，重启即回到原行为。

- 键 = 人物 + 会话/群私聊范围 + 记忆策略指纹（`a_memorix` 配置，含聊天过滤名单）+ 证据（目标发言的消息 ID、内容哈希、修订；目标发言之前的上下文也进入键，只排除目标之后新增的机器人分段）+ 抽取规则版本。策略读不到就不复用。
- 仅在抽取确认成功且写入被 `store_person_memory_from_answer` 确认（现返回 True/False/None）后记为完成。“确认成功”只看**原始合法 JSON**（或完整的代码围栏内的合法 JSON）：真正的 `[]`，或每一项都是长度足够的字符串/`fact` 为字符串的对象。需要 `repair_json` 才能解析的输出（例如被截断的 `[{"fact":"…"`）、`[12345]`、`fact` 非字符串、含无效项、重复之外的丢弃/截断、异常、空响应、写入失败/未知，都不记完成；修复得到的事实仍按旧行为写入。
- 已抽取但未确认写入的候选会保留并在重试时补写，第二次模型漏掉也不会被当作完成；同一键的并发串行化，后到者看到完成状态直接跳过。
- 开启时固定抽取规则前置（服务端前缀缓存友好），规则文本仍要求事实只能来自目标用户原话；关闭时提示词、解析与写入与原行为一致。
- 已知局限：已写入记忆被他处删除/撤回无法观测，只能靠 TTL（默认 15 分钟，空结果 5 分钟）自然过期。

## P1 回复后本地结束（实验）

入口：`reasoning_engine._handle_tool_calls` 批次末尾 → `_should_stop_after_single_reply`，判定为纯函数 `maisaka/reply_termination.py`。不在 reply 上设置 `stop_after_execution`。

只接受 planner 在 reply 参数中**明确给出 `final_reply=true`**（开启时由 `reasoning_engine._build_action_tool_definitions` 通过 `add_final_reply_parameter` 为 reply 的工具定义追加该可选参数；工具执行前由 `strip_final_reply_argument` 移除，不会传给 replyer/Hook；`builtin_tool/reply.py` 未改动），并同时满足：首轮；批次来源**全部已验证**且恰为一条普通外部聊天消息（`_classify_batch`：确认为机器人自身的消息可忽略；身份无法识别、缺消息 ID、命令/通知，或这些标记未知的任何一项，都使该次优化禁用，绝不通过忽略它们把批次当成单条）；非主动/超时/聚焦；仅一个 reply、成功、所有分段发送确认、目标就是该消息、无额外结果；无待处理消息、无挂起 wait、内部触发队列为空。判定与停止之间没有 await。任何未知或不满足都按原流程，并记录原因代码；未知 mode 继续原流程。结束原因为独立的 `reply_completed_local_stop`，空闲退避按“原流程 reply 轮重置 + stop 轮计 1”等价处理。不读取回复文本，也不按私聊身份推断。该开关不能证明回复里没有未标注的后续承诺，所以需要模型显式声明；效果依赖模型是否使用该参数，未实测。

## P2/P3 分层与工具目录（实验）

入口：`maibot_unified_core/context_layers.py`、`service.qq_context_layers`、`plugin._inject`。`policy.json` 示例见 `config/token-optimizations.example.json` 的 `policy_context_layers_example`。

- 稳定规则（每次都带）与低频快照（动态人设、群级表达/俚语）分开；当前说话者资料、query 事实、资格表、命令/媒体上下文、回执每轮重建，在即时块里。
- 快照按会话隔离；外部消息计数排除机器人自身、命令前缀和无内容事件；满 N 条（默认 20）、超过时间上限（默认 1800 秒）、人设版本/context_revision/授权指纹变化即失效。每个可发布边界（快照构建后、即时结果返回前）重新读取策略并校验版本，await 期间变化则丢弃并回到实时旧路径。任何异常也回到旧路径。设置非法时整体关闭。
- 位置与长度可独立选择：`prefix_position`（tail/front）、`tool_catalog`（full/short）、`tool_catalog_position`（tail/front）。缩短只截断描述，保留编号、工具名和 `tool_search` 说明；其它项（图片、工具调用/返回）原样，重复注入会替换而不是累加。前移不稳定内容可能降低整体缓存命中，必须用真实 hit/miss 对照，收益不是保证。没有新增摘要模型调用。

## 如何复现测试

Host 候选补丁是在**已打过旧补丁**的 Host 源码上的增量，所以顺序是：

1. 取 MaiBot 1.3.14 wheel 的源码；
2. 先应用 `patches/maibot-host-current.patch`（当前部署基线，4 处补丁）；
3. 再应用 `patches/token-optimizations.patch`（候选增量，`-p1`，在包含 `src/` 的目录下；新增文件以 `/dev/null` 差异给出）。完整性可用 `patches/token-optimizations.hashes.json` 的 SHA256 核对。

得到的 `src` 目录通过环境变量交给测试（测试只加载其中被改动的 Host 文件，其余 `src.*` 依赖是桩，不需要安装 MaiBot 依赖，也没有网络/模型调用）：

```
DAFEIYU_HOST_SOURCE_ROOT=/path/to/rebuilt/src python scripts/run_current_tests.py
# 只跑新增测试：
DAFEIYU_HOST_SOURCE_ROOT=/path/to/rebuilt/src PYTHONPATH=extensions:extensions/paidtasks:extensions/qq_channel:extensions/tests_dafeiyu \
  python -m unittest discover -s extensions/tests_token_optimizations -p 'test_*.py'
```

未设置 `DAFEIYU_HOST_SOURCE_ROOT`（或不是目录）时，依赖 Host 源码的测试会被明确标为 **skipped**，输出里可见，不算通过；P0 费用和 P2/P3 分层测试不需要 Host，照常运行。

统计边界：既有基线是 448 次执行（447 通过 + 1 项上游基线测试跳过）；`scripts/run_current_tests.py` 现在把 `extensions/tests_token_optimizations` 的新增测试（本次为 100 项）作为最后一个套件一起运行，所以完整运行 = 448 + 新增。任务检查脚本会另外再单独跑一遍新增测试，这是同一批测试的重复执行，不应再次累加。

## 未实现

P5 只加了结构观测，没有跳过行为；P6/P7/P8 暂缓；不调整额度、图库权限、模型、人设、平台发送或历史窗口。未部署、未做线上或付费模型验证。

## 回退

关闭对应开关即可（删除环境变量/JSON 字段/policy 字段）；Host 开关文件 5 秒内重读，环境变量需重启 Host。补丁整体回退为不应用 `token-optimizations.patch`。
