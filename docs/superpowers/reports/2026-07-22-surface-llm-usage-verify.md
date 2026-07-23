# surface-llm-usage 验证报告

## 结论

实现已完成并满足 OpenSpec proposal、design、delta spec 的范围：AgentLoop 仍为唯一采集点；运行级摘要、SSE、运行详情和新的跨会话聚合 API 均采用白名单契约；缓存缺失不显示为零；`vip_server` 资格不接入余额、支付、扣量或额度逻辑。全局用量中心位于 `/usage`，Agent 聊天流不再呈现单次用量，Run Detail 保留运行级追溯。

## 完整性

| 检查 | 结果 |
| --- | --- |
| OpenSpec 任务 | `17/17` 完成 |
| 实施计划步骤 | `34/34` 完成 |
| OpenSpec 严格校验 | `openspec validate surface-llm-usage --strict` 通过 |
| 规格能力 | 1 个 `llm-usage-visibility` 能力已覆盖 |
| 设计文档 | 已定位；实现遵循按需聚合、无第二采集管线、无轮询和运行级追溯决策 |

## 正确性与安全性

| 检查 | 结果 |
| --- | --- |
| 后端相关回归 | `80 passed`：聚合、路由、运行摘要和终态行为覆盖通过 |
| 前端相关 Vitest | `55 passed`：用量中心、Agent 行为、布局、路由和五种 locale 覆盖通过 |
| 前端生产构建 | `npm run build` 通过；仅现有 chunk-size 警告 |
| Python 静态编译 | `.venv/bin/python -m compileall -q agent/cli agent/api_server.py agent/mcp_server.py` 通过 |
| API 响应边界 | 聚合 DTO 明确构造公开 totals；敏感字段检索未发现聚合生产代码将凭据、消息正文、prompt 或模型响应写入响应 |
| Agent 与 Usage 生命周期 | Agent 的 `llm_usage` 仅维持 SSE watchdog；Usage 不创建 interval、轮询 timeout 或 SSE |
| 差异静态检查 | `git diff --check` 通过 |

## 已接受的基线偏差

用户于 2026-07-23 明确授权不修复以下范围外失败并完成本任务收尾。它们均已在实施计划基线 `91a5de4` 存在，且没有任何失败文件出现在本 change 的提交范围内：

| 全量命令 | 结果 | 基线问题 |
| --- | --- | --- |
| Python 全量 pytest（排除现有 E2E 与 3 个已知 `test_run_card` 断言） | 收集阶段失败 | `agent/tests/test_serve_open_flag.py` 引用不存在的 `api_server._should_open_browser` |
| `frontend/npx vitest run` | 14 个文件、17 个断言失败 | 已删除认证组件的导入，以及固定英文断言与当前中文 locale 不匹配等既有失败 |

这些问题不改变本 change 的定向回归与生产构建结论，但意味着当前仓库的全量套件并非零失败。

## 已授权跳过

未启动真实 Provider、服务会话或任何交易写入流。仓库没有可安全调用的无凭据测试 Provider；自动化测试使用本地 stub 覆盖运行摘要、SSE、历史聚合与前端展示的契约。

## 范围边界

本 change 只展示本地运行统计和未来计量资格标签；未实现登录关联、购买 Token、余额、周期重置、账单、扣量或额度拦截。
