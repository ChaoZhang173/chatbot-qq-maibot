# 爱发电小任务试运行增量包

2026-10-24。该包在既有 QQ＋B站架构上增加独立付款账本、有限任务执行器与本机适配入口。

**当前：收单服务已增量部署，主人私聊通知及 PNG 有真实平台回执，¥1 预算已入账；爱发电 API/商品未核验，接单仍关闭，不能邀请付款。**

- [实际基线](BASELINE.zh.md)：服务器核对、平台限制、已完成的 GitHub 备份。
- [能力清单](CAPABILITIES.json)：区分实现、部署、真实账号及真实付款验证。
- [部署与操作](RUNBOOK.zh.md)：增量安装、回滚、人工 B站流程、¥1 验收步骤。
- [验收矩阵](ACCEPTANCE.zh.md)：T01–T17 与证据边界。
- [交付报告](REPORT.zh.md)：完成项和仍需用户处理的条件。

本地测试：

```sh
python -m unittest discover -s tests -v
```

依赖：Python 3.12、Pillow、中文字体。源码中的网络客户端固定访问爱发电官方 HTTPS API，不记录签名、令牌、原始订单或返回调试字段。配置样例全部为空身份及默认关闭。

目录：`paidtasks/` 为收单服务，`integration/` 为两处既有插件的增量接入，`deploy/` 为独立服务及安装/回滚脚本。`integration/changes.patch` 和构建清单只针对已核对的源码哈希，遇到后续改动拒绝覆盖。

`assets/fonts/` 中本地分发包附带 Noto 中文字体及 OFL 许可；GitHub 代码包以 `scripts/fetch_font.py` 下载并校验同一版本。`private/`、数据库、运行日志、聊天历史、凭据不属于公开交付物。
