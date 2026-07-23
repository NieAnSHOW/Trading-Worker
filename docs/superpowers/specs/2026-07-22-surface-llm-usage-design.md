---
comet_change: surface-llm-usage
role: technical-design
canonical_spec: openspec
archived-with: 2026-07-23-surface-llm-usage
status: final
---

# 全局 LLM 用量中心设计

## 目标

在不引入 Tauri 代理、数据库或第二套统计管线的前提下，将 AgentLoop 已采集的运行级 Token 用量按全部 Agent 会话汇总，并通过独立的 WebUI 用量中心呈现。用户可以查看全部历史或指定时间范围的总量、趋势、Provider/模型分布和会话明细，再从明细追溯到单次运行。

缓存读取和写入只显示供应商明确报告的值。`vip_server` 调用携带未来平台计量资格标记；该标记不产生扣费、余额、充值、重置、结算或额度拦截行为。

## 范围与不变量

- 本地 `llm_usage.json` 是聚合服务和运行详情共用的运行级原始观测数据；SSE 契约继续保留，但 Agent 聊天流不消费它来展示用量。
- AgentLoop 是唯一采集和规范化位置。前端仅呈现服务端聚合或返回的数值，绝不自行估算 Token 或缓存。
- `llm_usage.json`、SSE 和运行级用量 API 只包含白名单中的 Provider、模型、迭代、计数和资格字段；不得复制 usage 原始对象，因而不含 API Key、登录凭据、提示词、响应或账户标识。全局聚合会话行仅额外复用既有会话 ID 和用户可见标题，不返回消息正文、运行 prompt 或模型响应。
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

### 全局聚合响应

新增 `GET /usage/llm`，使用以下查询参数：

- `start_at`、`end_at`：可选 ISO 8601 带时区边界，采用 `[start_at, end_at)` 半开区间；全部历史时省略。前端的近 7 天、近 30 天、本月和自定义时间最终都转换为这两个参数，自定义结束日转换为次日当地 `00:00:00`。
- `timezone`：浏览器的 IANA 时区，例如 `Asia/Shanghai`；无效值返回 400，前端无法取得浏览器时区时使用 `UTC`。
- `query`：可选会话标题或 ID 搜索，只影响会话明细，不影响总览、趋势和分布。
- `page`、`page_size`：会话明细分页参数；总览仍覆盖完整时间范围。

响应由五部分组成：

```ts
interface LLMUsageAggregateResponse {
  generated_at: string;
  timezone: string;
  period: { start_at: string | null; end_at: string | null };
  totals: {
    sessions: number;
    runs: number;
    calls: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cache_read_tokens?: number;
    cache_write_tokens?: number;
    cache_read_reported_runs: number;
    cache_write_reported_runs: number;
    missing_usage_runs: number;
    invalid_usage_runs: number;
  };
  trend: LLMUsageDailyBucket[];
  breakdown: LLMUsageModelBucket[];
  sessions: {
    items: LLMUsageSessionRow[];
    page: number;
    page_size: number;
    total_items: number;
    total_pages: number;
  };
}
```

趋势桶包含本地日期和同一组计数；分布按规范化后的 `(provider, model)` 分组；会话行包含会话 ID、现有用户可见标题、最后运行时间、聚合计数和可展开的运行行。运行行只携带 run ID、时间、Provider/模型和计数，不复制提示词或模型响应。

缓存累计仅在至少一个运行显式报告相应字段时出现。`*_reported_runs` 表达覆盖率，`missing_usage_runs` 表达关联运行没有 Provider 用量产物，`invalid_usage_runs` 表达文件存在但无法通过严格模型校验的数量。缺失或损坏单个文件不会使整个请求失败。

### 会话与运行关联

聚合服务直接使用 SessionStore，不调用受列表上限约束的 HTTP API：

1. 遍历全部会话；从该会话的 attempts 读取非空 `run_dir`。
2. 对历史关联不完整的会话，从助手消息 `metadata.run_id` 补充运行 ID。
3. 在单个会话内按 run ID 去重；若同一 run ID 异常出现在多个会话，以 attempt 关联优先并只计入一次。
4. 读取对应 `llm_usage.json` 并用现有严格 `LLMUsageSummary` 模型校验。
5. 运行时间优先使用 attempt 的 `created_at`；消息 fallback 使用对应助手消息的 `created_at`；两者都不可用时退回受约束运行目录的修改时间。运行 ID 中的本地无时区时间不参与日期筛选。

无法关联到任何会话的孤立运行不属于“所有会话”，不会进入聚合结果。实现封装为独立聚合服务，使未来在历史规模明显增长时可替换为索引，而不改变 API 或页面。

### 规范化与缓存字段映射

`_normalize_llm_usage` 保持对 `input_tokens`、`output_tokens`、`total_tokens` 的现有处理方式。新增一个只接受非负整数的嵌套字段读取器，明确映射已知 Provider/LangChain 形状：

- 缓存读取：`input_token_details.cache_read`、`input_token_details.cached`、`prompt_tokens_details.cached_tokens`、`cache_read_input_tokens`、`cache_read_tokens`。
- 缓存写入：`input_token_details.cache_creation`、`cache_creation_input_tokens`、`cache_creation_tokens`、`cache_write_tokens`。

匹配到字段即使值为零也保留；未匹配则完全省略对应键。映射是固定白名单，不递归搜索 usage 对象，也不依据总输入或其他计数推导缓存量。

`_record_llm_usage` 将每次规范化增量写入 `per_iteration`，并只为该字段已报告的计数增加 `totals`。文件仍使用临时文件替换方式原子写入。`GET /runs/{run_id}` 继续原样反序列化该文件，因此运行级契约不需要迁移；全局读取由独立的 `/usage/llm` 聚合端点提供。

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

`LLMUsagePanel` 接收 `LLMUsageSummary | null`，作为 Run Detail 的运行级用量展示组件。全局页面使用独立的聚合组件，因为它需要呈现跨会话趋势、分布和明细，而不是复用运行卡片布局。

- 有摘要时，以稳定的紧凑网格显示 Provider、模型、调用次数、输入、输出和总 Token。
- 缓存读取或写入只在对应可选字段存在时显示；两者都缺失时显示本地化的“缓存用量未提供”。
- `metering_eligible: true` 显示“具备未来平台计量资格”；其他或历史摘要中缺失该字段时显示“本地统计，不计入平台额度”。这不是余额或扣费状态。
- `null` 摘要显示“该运行未收到供应商报告的用量”。
- 组件不接受或渲染原始 usage、提示词、响应、用户 ID、API Key 或登录 token。

文案加入现有五个 locale（`en`、`zh-CN`、`ja`、`ko`、`ar`）的既有命名空间。数字使用现有格式化惯例，布局在窄屏时从多列折为单列，避免随内容改变卡片尺寸。

### 顶层用量中心

新增懒加载路由 `/usage`，并在现有侧栏中加入一级“用量”入口。页面采用已经确认的工作台布局：

1. 顶部是标题、上次成功刷新时间和手动刷新按钮。首次进入自动加载；后续不轮询。刷新期间保留旧快照并显示更新状态。
2. 使用分段控件切换全部历史、近 7 天、近 30 天和本月；自定义时间使用起止日期控件。默认选择全部历史。
3. 总览展示总 Token、输入/输出、模型调用数和有用量的会话数；缓存读写与报告覆盖率在总览中作为次级指标明确展示。
4. 趋势图按用户时区展示每日 Token；Provider/模型分布按总 Token 降序，使用可区分的颜色而非单一色系。
5. 会话明细按最后运行时间倒序，可按标题或 ID 搜索。每行显示运行数、总 Token 和调用数；展开后列出运行并链接 `/runs/{run_id}`。

总览、趋势和 Provider/模型分布不随会话搜索或明细分页重新计算。窄屏将指标与图表改为纵向布局，会话表使用可阅读的移动端行布局，不允许文本或数值覆盖。

### Agent 聊天流

Agent 页面不再维护 `liveLlmUsage`，不在流式区域或历史消息中插入用量卡片，也不为了补充用量而额外读取运行详情。后端 `llm_usage` SSE 事件继续保留为兼容契约，但该页面忽略它。既有对话、工具状态和运行卡片行为保持不变。

### Run Detail

给 `RunData` 添加可选 `llm_usage?: LLMUsageSummary` 类型。在 Run Detail 的运行概览中渲染同一 `LLMUsagePanel`，使从回测报告链接直接打开的用户也能查看持久化用量。旧运行和未报告 usage 的运行显示清晰空状态，不影响既有图表、交易、验证或代码 Tab。

## 错误与兼容处理

- Provider 返回无效或全零且无有效 usage 的数据：维持当前行为，不创建伪造 SSE 事件或文件。
- Provider 只报告部分缓存字段：保留已报告字段；未报告字段单独显示未提供，永不通过总 Token 推算。
- 旧产物：API 原样返回，前端用可选字段解析，不因缺少 `metering_eligible` 或缓存字段报错。
- 聚合中的文件缺失或校验失败：跳过单个运行并增加覆盖率计数；页面显示“部分运行数据不可用”，其他结果继续可用。
- 聚合请求失败：若页面已有成功快照则保留并标记刷新失败；否则显示错误状态和重试按钮。
- 聚合结果为空：显示没有 Provider 报告用量的空状态，不把空数据渲染为真实零消耗结论。

## 测试策略

### 后端

- 扩展 AgentLoop 单元测试，覆盖每个缓存别名、嵌套字段、显式零值、缺失字段、跨迭代累计、原子产物和 SSE 负载。
- 验证 `vip_server` 为 `metering_eligible: true`，其他 Provider 为 false；序列化结果只包含允许字段且不包含哨兵凭据、提示词或响应。
- 扩展运行详情测试，覆盖增强摘要和旧版 `llm_usage.json` 的无损读取。
- 为聚合服务和 API 添加测试，覆盖 attempt 关联、消息 fallback、跨来源去重、全部历史、日期边界、IANA 时区分桶、Provider/模型分组、缓存覆盖率、缺失/损坏文件、搜索和分页不影响总览。

### 前端

- 为 `LLMUsagePanel` 测试完整、缓存未提供、无 usage、VIP 与本地统计标签，以及中英文文本。
- 为 `/usage` 页面测试默认全部历史、日期筛选、浏览器时区、手动刷新保留快照、会话搜索与展开、运行链接、空状态、部分数据警告和错误重试。
- 扩展布局和路由测试确认侧栏一级入口；更新 Agent 测试确认聊天流不再渲染用量，同时保留 Run Detail 的运行级摘要测试。
- 运行 locale 完整性测试、Vitest、前端生产构建，以及定向 pytest。

## 不做的事情

本变更不实现账户实体、身份关联 API、充值、支付、余额、每日重置、月度结算、价格换算、服务端扣费、额度拦截或跨运行账本。未来这些能力必须以服务端网关为唯一权威，并把其独立返回的账户摘要接入现有用量面板的预留位置。
