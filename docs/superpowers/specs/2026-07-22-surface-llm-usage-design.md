---
comet_change: surface-llm-usage
role: technical-design
canonical_spec: openspec
---

# LLM Token 用量可见化设计

## 目标

在不引入 Tauri 代理、数据库或第二套统计管线的前提下，将 AgentLoop 已采集的模型 Token 用量呈现在 WebUI。用户在运行期间能看到当前调用的累计值，在会话历史和运行详情中能看到同一运行的持久化结果。

缓存读取和写入只显示供应商明确报告的值。`vip_server` 调用携带未来平台计量资格标记；该标记不产生扣费、余额、充值、重置、结算或额度拦截行为。

## 范围与不变量

- 本地 `llm_usage.json`、SSE 与运行详情 API 继续是同一份运行观测数据的三个读取面。
- AgentLoop 是唯一采集和规范化位置。前端仅呈现或累加服务端发送的数值，绝不自行估算 Token 或缓存。
- `llm_usage.json`、SSE 和 API 只包含白名单中的 Provider、模型、迭代、计数和资格字段；不得复制 usage 原始对象，因而不含 API Key、登录凭据、提示词、响应或账户标识。
- 缓存字段缺失表示“供应商未提供”，不是 `0`。明确报告 `0` 时保留为 `0`。
- 历史 `llm_usage.json` 缺少新增字段时仍然可读；前端按可选字段降级。
- 本地 `metering_eligible` 仅是展示和未来协议边界，不能作为任何账户扣量或授权依据。未来平台网关必须以认证账户和服务端观测数据独立裁决。

## 数据契约

### 持久化运行摘要

现有顶层字段保持不变。扩展后的摘要形状如下；方括号表示可选字段。

```ts
interface LLMUsageCounters {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
}

interface LLMUsageIteration {
  iter: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
}

interface LLMUsageSummary {
  provider: string;
  model: string | null;
  metering_eligible: boolean;
  totals: LLMUsageCounters;
  per_iteration: LLMUsageIteration[];
  updated_at?: string;
}
```

`calls` 始终是有效 Provider usage 调用数。缓存字段仅在至少一次调用显式报告相应值时出现，累计值只求和已报告数值，不补齐未报告调用。UI 将它们标为“供应商报告的缓存 Token”，避免将部分报告误读为计费结论。

`metering_eligible` 由运行使用的 Provider 标识生成：仅规范化后的 `vip_server` 为 `true`，其他 Provider 为 `false`。此判断不读取、比较或传输任何密钥。现阶段 `vip_server` 是桌面登录配置的平台托管模型通道；未来服务端计量仍必须重新验证调用来源与账户身份。

### 规范化与缓存字段映射

`_normalize_llm_usage` 保持对 `input_tokens`、`output_tokens`、`total_tokens` 的现有处理方式。新增一个只接受非负整数的嵌套字段读取器，明确映射已知 Provider/LangChain 形状：

- 缓存读取：`input_token_details.cache_read`、`input_token_details.cached`、`prompt_tokens_details.cached_tokens`、`cache_read_input_tokens`、`cache_read_tokens`。
- 缓存写入：`input_token_details.cache_creation`、`cache_creation_input_tokens`、`cache_creation_tokens`、`cache_write_tokens`。

匹配到字段即使值为零也保留；未匹配则完全省略对应键。映射是固定白名单，不递归搜索 usage 对象，也不依据总输入或其他计数推导缓存量。

`_record_llm_usage` 将每次规范化增量写入 `per_iteration`，并只为该字段已报告的计数增加 `totals`。文件仍使用临时文件替换方式原子写入。`GET /runs/{run_id}` 继续原样反序列化该文件，因此不需要新端点或迁移。

### SSE 增量

现有 `llm_usage` 事件扩展为安全的增量契约：

```ts
interface LLMUsageDelta {
  iter: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  provider: string;
  model: string | null;
  metering_eligible: boolean;
}
```

后端从已构造的运行摘要派生 Provider、模型和资格字段，而非从请求或配置原文中透传任意值。事件仍沿用现有发送频率和事件通道，不新增网络请求。

## WebUI 设计

### 共享展示组件

新增 `LLMUsagePanel`，接收 `LLMUsageSummary | null` 与紧凑展示变体。它是唯一的用量格式化与状态呈现位置，供 Agent 时间线、流式区域和 Run Detail 共用。

- 有摘要时，以稳定的紧凑网格显示 Provider、模型、调用次数、输入、输出和总 Token。
- 缓存读取或写入只在对应可选字段存在时显示；两者都缺失时显示本地化的“缓存用量未提供”。
- `metering_eligible: true` 显示“具备未来平台计量资格”；其他或历史摘要中缺失该字段时显示“本地统计，不计入平台额度”。这不是余额或扣费状态。
- `null` 摘要显示“该运行未收到供应商报告的用量”。
- 组件不接受或渲染原始 usage、提示词、响应、用户 ID、API Key 或登录 token。

文案加入 `en.json` 和 `zh-CN.json` 的既有命名空间。数字使用现有格式化惯例，布局在窄屏时从多列折为单列，避免随内容改变卡片尺寸。

### Agent 运行中与完成后

Agent 页面维护仅用于当前尝试的 `liveLlmUsage` 状态：

1. 收到 `attempt.created` 时清空旧状态。
2. 收到 `llm_usage` 时验证并累加服务端 delta；每个事件使调用数加一，缓存字段只有事件提供时才创建或累加。SSE 现有按事件 ID 去重机制避免重放双计。
3. 在现有流式区域的工具/文本状态下方渲染紧凑 `LLMUsagePanel`，让用户在运行中直接看到累计值。
4. 收到 `attempt.completed` 后，从 `run_dir` 得到 run ID 并调用既有 `api.getRun`。若返回 `llm_usage`，向时间线添加新的 `llm_usage` 消息，使用持久化摘要替换临时累计值；若运行存在但没有摘要，添加空状态消息；网络读取失败时保留本次 live 快照但不将其误称为已持久化结果。
5. 会话恢复时，对每个关联 run ID 使用现有运行详情读取路径补充相同的 `llm_usage` 时间线消息。普通聊天和回测运行都遵循这一规则，不依赖是否存在回测指标。

新增 `AgentMessageType` 的 `llm_usage` 分支和可选 `llmUsage` 负载；`MessageBubble` 将其交给共享面板。聊天导出只输出安全的摘要计数和 Provider/模型标识，或省略该消息，绝不输出额外运行内容。

### Run Detail

给 `RunData` 添加可选 `llm_usage?: LLMUsageSummary` 类型。在 Run Detail 的运行概览中渲染同一 `LLMUsagePanel`，使从回测报告链接直接打开的用户也能查看持久化用量。旧运行和未报告 usage 的运行显示清晰空状态，不影响既有图表、交易、验证或代码 Tab。

## 错误与兼容处理

- Provider 返回无效或全零且无有效 usage 的数据：维持当前行为，不创建伪造 SSE 事件或文件。
- Provider 只报告部分缓存字段：保留已报告字段；未报告字段单独显示未提供，永不通过总 Token 推算。
- 旧产物：API 原样返回，前端用可选字段解析，不因缺少 `metering_eligible` 或缓存字段报错。
- SSE 解析失败：忽略该事件并保留此前显示值；完成后仍以运行详情为准。
- 完成时运行详情读取失败：保留当前会话的实时面板，不写入持久化声称；下一次历史加载可重新读取。

## 测试策略

### 后端

- 扩展 AgentLoop 单元测试，覆盖每个缓存别名、嵌套字段、显式零值、缺失字段、跨迭代累计、原子产物和 SSE 负载。
- 验证 `vip_server` 为 `metering_eligible: true`，其他 Provider 为 false；序列化结果只包含允许字段且不包含哨兵凭据、提示词或响应。
- 扩展运行详情测试，覆盖增强摘要和旧版 `llm_usage.json` 的无损读取。

### 前端

- 为用量归约/验证辅助函数测试增量累积、可选缓存与事件重放安全性。
- 为 `LLMUsagePanel` 测试完整、缓存未提供、无 usage、VIP 与本地统计标签，以及中英文文本。
- 扩展 SSE hook 测试以确认 `llm_usage` 分派；为 Agent 的恢复/完成路径和 Run Detail 加入针对持久化摘要的测试。
- 运行 locale 完整性测试、Vitest、前端生产构建，以及定向 pytest。

## 不做的事情

本变更不实现账户实体、身份关联 API、充值、支付、余额、每日重置、月度结算、价格换算、服务端扣费、额度拦截或跨运行账本。未来这些能力必须以服务端网关为唯一权威，并把其独立返回的账户摘要接入现有用量面板的预留位置。
