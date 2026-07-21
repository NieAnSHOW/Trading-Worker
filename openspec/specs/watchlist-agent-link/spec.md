# watchlist-agent-link Specification

## Purpose
TBD - created by archiving change market-watchlist. Update Purpose after archive.
## Requirements
### Requirement: 选中股票发送到 Agent 会话
用户 SHALL 能在盯盘页面勾选一支或多支股票，点击「发给 Agent 分析」后跳转到 `/agent` 页面，输入框预填分析请求文本（不自动提交，用户可先编辑）。

#### Scenario: 选中单支股票发给 Agent
- **WHEN** 用户勾选「000001 平安银行」并点击「发给 Agent 分析」
- **THEN** 跳转到 `/agent?prefill=帮我分析 000001 平安银行`，Agent 页面输入框预填该文本

#### Scenario: 选中多支股票发给 Agent
- **WHEN** 用户同时勾选「000001 平安银行」和「600519 贵州茅台」并点击「发给 Agent 分析」
- **THEN** 跳转到 `/agent?prefill=帮我分别分析以下股票：000001 平安银行、600519 贵州茅台`

#### Scenario: 未选中任何股票时按钮禁用
- **WHEN** 用户未勾选任何股票
- **THEN** 「发给 Agent 分析」按钮处于禁用状态（`disabled`），无法点击

### Requirement: Agent 页面支持 prefill query param
Agent 页面（`/agent`）SHALL 读取 URL `prefill` query param，并在页面加载后将其填入消息输入框。用户可以在发送前编辑预填内容。

#### Scenario: 携带 prefill param 进入 Agent 页面
- **WHEN** 用户访问 `/agent?prefill=帮我分析 000001 平安银行`
- **THEN** 消息输入框中预填「帮我分析 000001 平安银行」，光标位于文末，用户可编辑后发送

#### Scenario: 不携带 prefill param 时正常显示
- **WHEN** 用户访问 `/agent`（无 prefill param）
- **THEN** 输入框为空，行为与现有一致，不受影响

