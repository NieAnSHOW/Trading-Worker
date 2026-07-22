## Why

AgentLoop 已按每个运行记录大模型输入、输出和总 Token，并通过 SSE 发送增量，但 WebUI 未消费该数据，用户无法查看实际用量。现有记录也未保留供应商返回的缓存分项，且没有区分未来可计费的 VIP 调用与用户自带 API Key 调用，无法为账户额度产品提供稳定的展示与契约基础。

## What Changes

- 复用现有运行级 `llm_usage.json`、`llm_usage` SSE 事件和运行详情 API，将模型、调用次数、输入、输出、总 Token 以及供应商报告的缓存 Token 呈现到 WebUI。
- 扩展既有用量规范化和响应契约，以保留可用的缓存读写分项；供应商未报告时明确表示为未提供，而不进行估算。
- 为平台托管的 VIP 模型标注未来可计费用量，为账户、结算周期和余额摘要保留兼容的数据边界；用户自带 API Key 的调用保持本地可见但不可计费。
- 保持本地运行产物为观测与展示数据，而非充值、扣费、每日重置或余额裁决的权威账本。

## Capabilities

### New Capabilities

- `llm-usage-visibility`: 提供可在运行中和运行完成后查看的 LLM 用量数据，并规定缓存可用性与未来 VIP 计量兼容边界。

### Modified Capabilities

- 无。

## Impact

- 后端：`agent/src/agent/loop.py` 的用量规范化与持久化、运行详情 API 模型和路由。
- 前端：SSE 事件消费、运行 API TypeScript 类型、Agent 运行中界面与运行详情展示、国际化和测试。
- 桌面登录与 VIP 配置：仅作为未来服务端计量身份关联的兼容边界；本次不修改认证、支付或配额执行逻辑。
