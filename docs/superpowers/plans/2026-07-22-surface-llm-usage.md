---
change: surface-llm-usage
design-doc: docs/superpowers/specs/2026-07-22-surface-llm-usage-design.md
base-ref: 50a395aca6a6d98a639fed2d1f41a250d0021bd8
---

# LLM Token 用量可见化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复用 AgentLoop 已有的 `llm_usage.json`、`llm_usage` SSE 和运行详情读取路径，在 WebUI 显示每次运行的 Provider 报告 Token 用量及未来平台计量资格。

**Architecture:** AgentLoop 继续作为唯一的用量采集与规范化位置，以固定白名单读取供应商缓存字段，原子写入已有运行产物，并从同一安全摘要派生 SSE 事件。前端用一个纯函数归约增量、一个共享 `LLMUsagePanel` 呈现实时和持久化摘要；会话页在 SSE 与恢复/完成读取之间切换，Run Detail 直接复用该组件。不会引入 Tauri 命令、数据库、账户系统或第二条统计管线。

**Tech Stack:** Python 3.11+、FastAPI/Pydantic、pytest、React 19、TypeScript strict、Zustand、Tailwind CSS、i18next、Vitest/Testing Library。

## Global Constraints

- AgentLoop 是唯一 Token 与缓存 Token 的采集/规范化位置；前端不得估算、补零或由总量推导缓存数据。
- 只接受 Provider 明确报告的非负整数；缓存字段缺失必须保持缺失，显式 `0` 必须保留为 `0`。
- `llm_usage.json`、SSE、`GET /runs/{id}` 只能含 Provider、模型、迭代、计数、`metering_eligible` 和时间戳白名单字段，绝不含 API Key、登录 token、提示词、响应或账户标识。
- 仅规范化后的 `vip_server` 产生 `metering_eligible: true`；它只是未来服务端计量协议标签，当前不得实现登录关联、充值、余额、重置、结算、扣量或额度拦截。
- 自带 API Key 的使用仍本地可见，但 `metering_eligible` 为 `false`；旧 `llm_usage.json` 缺少新字段时仍必须可读。
- 复用现有 `GET /runs/{id}` 和 SSE 通道；不新增 Tauri 代理、数据库、API endpoint 或运行迁移。
- 所有显示文案同时写入 `frontend/src/i18n/locales/en.json` 与 `frontend/src/i18n/locales/zh-CN.json`，在窄屏使用稳定的单列/网格布局。

---

## 文件结构

- 修改 `agent/src/agent/loop.py`：白名单缓存字段规范化、`vip_server` 资格派生、摘要累计和 SSE 安全增量。
- 修改 `agent/tests/test_agent_loop_terminal_state.py`：覆盖缓存映射、显式零值、跨迭代累计、原子产物、SSE 与敏感字段排除。
- 修改 `agent/tests/test_run_card.py`：覆盖增强和旧版运行用量产物由运行详情原样返回。
- 修改 `frontend/src/lib/api.ts`：定义运行摘要、计数和 SSE 增量的共享 TypeScript 数据契约，并将 `RunData.llm_usage` 设为可选。
- 新增 `frontend/src/lib/llmUsage.ts`：验证并归约 SSE 增量为 UI 用的运行摘要，不触碰 React 状态。
- 新增 `frontend/src/lib/__tests__/llmUsage.test.ts`：测试纯归约器的有效、无效、可选缓存与重复事件语义。
- 新增 `frontend/src/components/chat/LLMUsagePanel.tsx`：唯一的 i18n 用量格式化和无障碍展示组件。
- 新增 `frontend/src/components/chat/__tests__/LLMUsagePanel.test.tsx`：覆盖完整、缺失、VIP 与本地标签的展示。
- 修改 `frontend/src/types/agent.ts`：新增 `llm_usage` 消息类型和可选用量负载。
- 修改 `frontend/src/components/chat/MessageBubble.tsx` 与其测试：将 `llm_usage` 消息交给共享面板。
- 修改 `frontend/src/pages/Agent.tsx`：维护当前尝试的 live 摘要，消费 SSE，完成/历史时改用运行详情持久化摘要。
- 修改 `frontend/src/pages/RunDetail.tsx`：在运行概览中复用面板。
- 修改 `frontend/src/hooks/__tests__/useSSE.test.ts`：确认已订阅的 `llm_usage` 会分派给专用处理器。
- 修改 `frontend/src/i18n/locales/en.json`、`frontend/src/i18n/locales/zh-CN.json` 与 `frontend/src/i18n/__tests__/locales.test.ts`：增加并校验两种语言的用量文案。

## 数据契约

后端和前端必须共同使用以下 JSON 形状。可选缓存字段的“未出现”语义是供应商未提供，不能转换为零值。

```ts
export interface LLMUsageCounters {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
}

export interface LLMUsageIteration {
  iter: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
}

export interface LLMUsageSummary {
  provider: string;
  model: string | null;
  metering_eligible?: boolean;
  totals: LLMUsageCounters;
  per_iteration: LLMUsageIteration[];
  updated_at?: string;
}

export interface LLMUsageDelta extends Omit<LLMUsageIteration, "iter"> {
  iter: number;
  provider: string;
  model: string | null;
  metering_eligible: boolean;
}
```

### Task 1: 扩展后端用量规范化与持久化契约

**Files:**
- Modify: `agent/src/agent/loop.py:80-159,733-747`
- Modify: `agent/tests/test_agent_loop_terminal_state.py:31-57,223-250`

**Interfaces:**
- Consumes: Provider `usage_metadata: Mapping[str, Any] | dict[str, Any]`，以及 `LANGCHAIN_PROVIDER`、`LANGCHAIN_MODEL_NAME`。
- Produces: `_normalize_llm_usage(usage: Any) -> dict[str, int] | None`，其中缓存键可选；`_new_llm_usage_summary(llm: Any) -> dict[str, Any]`；`_record_llm_usage(...) -> dict[str, int] | None`。
- Produces: `llm_usage.json` 运行摘要和 `llm_usage` SSE 增量，均包含派生的 `provider`、`model`、`metering_eligible`，不含原始 usage。

- [x] **Step 1: 写入失败的后端测试，固定缓存和安全输出语义**

  在 `test_agent_loop_terminal_state.py` 添加带两次响应的 stub（第一次同时有 `input_token_details.cache_read` 和 `input_token_details.cache_creation`，第二次仅有 `prompt_tokens_details.cached_tokens`），并添加以下断言：

  ```python
  assert payload["metering_eligible"] is True
  assert payload["totals"] == {
      "input_tokens": 30,
      "output_tokens": 9,
      "total_tokens": 39,
      "calls": 2,
      "cache_read_tokens": 11,
      "cache_write_tokens": 3,
  }
  assert payload["per_iteration"] == [
      {"iter": 1, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
       "cache_read_tokens": 4, "cache_write_tokens": 3},
      {"iter": 2, "input_tokens": 20, "output_tokens": 4, "total_tokens": 24,
       "cache_read_tokens": 7},
  ]
  assert "api_key" not in json.dumps(payload)
  assert "secret-prompt" not in json.dumps(payload)
  ```

  为 `_normalize_llm_usage` 添加参数化测试：五个缓存读取别名、四个缓存写入别名、嵌套字典、显式 `0`、缺失字段、负数和非数值。期望：匹配的显式 `0` 保留；缺失键不出现；负数被规范化为 `0`；只有缓存而没有基础用量不能制造有效 usage 事件。

- [x] **Step 2: 运行测试确认当前实现缺少缓存和资格字段**

  Run: `pytest agent/tests/test_agent_loop_terminal_state.py -k "usage" -q`

  Expected: FAIL，断言会指出产物没有 `cache_read_tokens` / `cache_write_tokens` / `metering_eligible`，或 SSE 缺少派生元数据。

- [x] **Step 3: 实现固定白名单缓存读取器和安全摘要派生**

  在 `loop.py` 增加一个只走指定路径的读取器，不递归遍历 usage：

  ```python
  def _read_usage_alias(usage: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> int | None:
      for path in paths:
          current: Any = usage
          for key in path:
              if not isinstance(current, dict) or key not in current:
                  break
              current = current[key]
          else:
              try:
                  return max(0, int(current))
              except (TypeError, ValueError):
                  return None
      return None
  ```

  调用它时只使用这些路径：读取 `("input_token_details", "cache_read")`、`("input_token_details", "cached")`、`("prompt_tokens_details", "cached_tokens")`、`("cache_read_input_tokens",)`、`("cache_read_tokens",)`；写入 `("input_token_details", "cache_creation")`、`("cache_creation_input_tokens",)`、`("cache_creation_tokens",)`、`("cache_write_tokens",)`。仅返回有效基础 usage 时，才在 normalized dict 中按需加入缓存键。

  在 `_new_llm_usage_summary` 中使用 `provider.strip().lower() == "vip_server"` 生成布尔 `metering_eligible`。在 `_record_llm_usage` 中只对本次 normalized dict 出现的缓存键更新 `totals`，再将同一 dict 追加到 `per_iteration`。保留现有 `.tmp` 后缀写入和 `replace()` 原子替换方式。

- [x] **Step 4: 从摘要派生 SSE 的白名单增量**

  将发射载荷改为仅取 summary 的已知字段，而不是合并任何请求或 Provider 对象：

  ```python
  self._emit("llm_usage", {
      **usage_delta,
      "iter": current_iter,
      "provider": llm_usage_summary["provider"],
      "model": llm_usage_summary["model"],
      "metering_eligible": llm_usage_summary["metering_eligible"],
  })
  ```

  不改变 `GoalStore` 使用的 `total_tokens` 逻辑，不把 `metering_eligible` 接入任何额度、账户或授权分支。

- [x] **Step 5: 运行后端定向测试并检查产物内容**

  Run: `pytest agent/tests/test_agent_loop_terminal_state.py -k "usage" -q`

  Expected: PASS；覆盖现有持久化回归测试、缓存别名、显式零值、两次调用累计、VIP 与非 VIP 资格、原子文件和敏感字段排除。

- [x] **Step 6: 提交此独立后端契约变更**

  ```bash
  git add agent/src/agent/loop.py agent/tests/test_agent_loop_terminal_state.py
  git commit -m "feat: enrich persisted LLM usage metadata"
  ```

### Task 2: 保持运行详情 API 的增强和旧版产物兼容

<!-- 2026-07-22: 用户确认此纯既有兼容性回归任务可直接通过；不构成对全局 tdd_mode 的修改。 -->

**Files:**
- Modify: `agent/tests/test_run_card.py:232-249`
- Inspect only: `agent/src/api/models.py:71-103`, `agent/src/api/runs_routes.py:157-165`

**Interfaces:**
- Consumes: 运行目录中的任意合法 `llm_usage.json`。
- Produces: `RunResponse.llm_usage: Optional[Dict[str, Any]]`，原样传递增强摘要与旧版摘要；不改变 endpoint 或创建迁移。

- [x] **Step 1: 写入运行详情增强/旧版回归测试**

  将现有 `test_api_run_response_includes_llm_usage` 的 fixture 增补缓存和资格字段，并添加一个旧版 fixture：

  ```python
  enhanced = {
      "provider": "vip_server", "model": "hosted-model", "metering_eligible": True,
      "totals": {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125,
                 "calls": 1, "cache_read_tokens": 12},
      "per_iteration": [{"iter": 1, "input_tokens": 100, "output_tokens": 25,
                         "total_tokens": 125, "cache_read_tokens": 12}],
  }
  legacy = {
      "provider": "openai", "model": "legacy-model",
      "totals": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "calls": 1},
      "per_iteration": [{"iter": 1, "input_tokens": 10, "output_tokens": 2, "total_tokens": 12}],
  }
  assert response.llm_usage == enhanced
  assert legacy_response.llm_usage == legacy
  ```

  额外序列化断言使用含 `api_key`、`login_token`、`prompt` 和 `response` 的哨兵输入。断言 API 不会自行构造或添加这些字段，即 `response.llm_usage` 只等于文件的安全白名单 fixture。

- [x] **Step 2: 运行测试验证现有读取路径已能承载新字段**

  Run: `pytest agent/tests/test_run_card.py -k "llm_usage" -q`

  Expected: PASS；若失败，只修正测试夹具或现有宽松 `Dict[str, Any]` 响应模型的兼容性，禁止添加新 API。

- [x] **Step 3: 保持 API 读取逻辑不做范围外扩张**

  确认 `RunResponse.llm_usage` 仍是 optional 字典，`_build_response_from_run_dir` 继续只 `json.loads(llm_usage.json)`。不要在 API 层重算 Token、校验账户、读取凭据或引入 Pydantic 严格模型，因为该层必须兼容历史文件并只作为现有产物的读取面。

- [x] **Step 4: 提交 API 兼容性回归测试**

  ```bash
  git add agent/tests/test_run_card.py
  git commit -m "test: cover LLM usage run detail compatibility"
  ```

### Task 3: 建立前端安全类型和可测试的实时归约器

<!-- 2026-07-22: 审查通过。接受两项 Minor：未单独覆盖省略 provider/iter，以及敏感字段未直接在 parser delta 上断言；当前实现和归约输出均已严格白名单，后续重构时可补充。 -->

**Files:**
- Modify: `frontend/src/lib/api.ts:833-865`
- Create: `frontend/src/lib/llmUsage.ts`
- Create: `frontend/src/lib/__tests__/llmUsage.test.ts`
- Modify: `frontend/src/types/agent.ts:1-56`

**Interfaces:**
- Consumes: `Record<string, unknown>` SSE 数据、`RunData.llm_usage` 和上述“数据契约”类型。
- Produces: `parseLLMUsageDelta(data: Record<string, unknown>): LLMUsageDelta | null`、`accumulateLLMUsage(current: LLMUsageSummary | null, delta: LLMUsageDelta): LLMUsageSummary`。
- Produces: `AgentMessage.llmUsage?: LLMUsageSummary` 与消息类型 `"llm_usage"`。

- [x] **Step 1: 写入归约器的失败测试**

  在 `llmUsage.test.ts` 测试以下完整序列：

  ```ts
  const first = parseLLMUsageDelta({
    iter: 1, input_tokens: 10, output_tokens: 5, total_tokens: 15,
    cache_read_tokens: 4, provider: "vip_server", model: "vip-v1", metering_eligible: true,
  });
  const second = parseLLMUsageDelta({
    iter: 2, input_tokens: 20, output_tokens: 4, total_tokens: 24,
    provider: "vip_server", model: "vip-v1", metering_eligible: true,
  });
  expect(accumulateLLMUsage(accumulateLLMUsage(null, first!), second!)).toMatchObject({
    totals: { input_tokens: 30, output_tokens: 9, total_tokens: 39, calls: 2, cache_read_tokens: 4 },
    per_iteration: [expect.objectContaining({ iter: 1 }), expect.objectContaining({ iter: 2 })],
  });
  ```

  还要验证：缓存缺失时 totals 没有缓存键；显式 `0` 被保留；负数、NaN、非数字、缺少 provider、缺少迭代或缺少基础 token 时 `parseLLMUsageDelta` 返回 `null`；归约器不保留传入对象的 `api_key`、`prompt` 或 `response` 属性。

- [x] **Step 2: 运行测试确认辅助模块尚不存在**

  Run: `cd frontend && npx vitest run src/lib/__tests__/llmUsage.test.ts`

  Expected: FAIL，提示无法解析 `@/lib/llmUsage` 或导出不存在。

- [x] **Step 3: 在 API 层定义共享契约，在归约器中严格验证**

  在 `api.ts` 于 `RunData` 前加入本计划“数据契约”中的四个 interface，并给 `RunData` 增加：

  ```ts
  llm_usage?: LLMUsageSummary;
  ```

  `llmUsage.ts` 只复制白名单字段，并使用如下规则：

  ```ts
  const asNonNegativeInt = (value: unknown): number | null =>
    typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;

  const optionalCounter = (value: unknown): number | undefined => {
    const normalized = asNonNegativeInt(value);
    return normalized === null ? undefined : normalized;
  };
  ```

  `parseLLMUsageDelta` 必须要求 `iter` 为正整数、三个基础计数为非负整数、provider 为非空 string，model 为 string 或 `null`，资格为 boolean；不要接收任意 `Record` 扩展字段。`accumulateLLMUsage` 初始化 `calls: 0` 和空 `per_iteration`，每个成功 delta 的 calls 增一，只在 delta 自己有缓存键时累加该缓存键，并只输出 `provider`、`model`、`metering_eligible`、`totals`、`per_iteration`。

  在 `AgentMessageType` union 加入 `"llm_usage"`，并在 `AgentMessage` 加入：

  ```ts
  llmUsage?: LLMUsageSummary;
  ```

- [x] **Step 4: 运行归约器测试与 TypeScript 检查**

  Run: `cd frontend && npx vitest run src/lib/__tests__/llmUsage.test.ts && npx tsc -b --noEmit`

  Expected: PASS；无 `any` 绕过、无未使用导入，且缓存缺失与零值的断言均通过。

- [x] **Step 5: 提交前端数据边界**

  ```bash
  git add frontend/src/lib/api.ts frontend/src/lib/llmUsage.ts frontend/src/lib/__tests__/llmUsage.test.ts frontend/src/types/agent.ts
  git commit -m "feat: add frontend LLM usage data contract"
  ```

### Task 4: 创建共享、国际化的用量展示组件

<!-- 2026-07-22: 审查通过。接受 Minor 组件测试缺口：未单测单 cache/显式 0 展示，中文未单测 futureEligible/noUsage；实现使用 undefined 判定且 reducer/locale 测试已覆盖主要契约。 -->

**Files:**
- Create: `frontend/src/components/chat/LLMUsagePanel.tsx`
- Create: `frontend/src/components/chat/__tests__/LLMUsagePanel.test.tsx`
- Modify: `frontend/src/components/chat/MessageBubble.tsx:1-111`
- Modify: `frontend/src/components/chat/__tests__/MessageBubble.test.tsx:1-100`
- Modify: `frontend/src/i18n/locales/en.json`
- Modify: `frontend/src/i18n/locales/zh-CN.json`
- Modify: `frontend/src/i18n/__tests__/locales.test.ts`

**Interfaces:**
- Consumes: `LLMUsageSummary | null` 和可选 `compact?: boolean`。
- Produces: `LLMUsagePanel({ usage, compact }: { usage: LLMUsageSummary | null; compact?: boolean }): JSX.Element`。
- Produces: `MessageBubble` 对 `msg.type === "llm_usage"` 的共享组件渲染。

- [x] **Step 1: 为组件及消息气泡写入失败测试**

  新组件测试覆盖四类可访问文本：

  ```tsx
  render(<LLMUsagePanel usage={completeUsage} />);
  expect(screen.getByText("vip_server")).toBeInTheDocument();
  expect(screen.getByText(/39/)).toBeInTheDocument();
  expect(screen.getByText(/Eligible for future platform metering/)).toBeInTheDocument();

  render(<LLMUsagePanel usage={usageWithoutCache} />);
  expect(screen.getByText("Cache usage not provided by provider")).toBeInTheDocument();

  render(<LLMUsagePanel usage={null} />);
  expect(screen.getByText("No provider-reported usage for this run")).toBeInTheDocument();
  ```

  以 `i18n.changeLanguage("zh-CN")` 断言相同状态出现中文文案。`MessageBubble.test.tsx` mock `LLMUsagePanel`，再断言带 `type: "llm_usage"` 的消息将完整 `llmUsage` 传入，且没有落入通用 fallback。

- [x] **Step 2: 运行测试确认组件尚不存在**

  Run: `cd frontend && npx vitest run src/components/chat/__tests__/LLMUsagePanel.test.tsx src/components/chat/__tests__/MessageBubble.test.tsx src/i18n/__tests__/locales.test.ts`

  Expected: FAIL，提示 `LLMUsagePanel` 或 locale key 不存在。

- [x] **Step 3: 实现无障碍、稳定尺寸的共享面板和双语文案**

  `LLMUsagePanel.tsx` 用 `useTranslation()`，以 `section aria-label={t("llmUsage.title")}` 呈现以下白名单内容：Provider、模型、calls、input、output、total、可选 cache read/write、资格标签。数字通过 `new Intl.NumberFormat(i18n.language).format(value)` 格式化，避免把 `undefined` 当作 `0`。

  根布局使用不嵌套卡片的紧凑边框区域，例如：

  ```tsx
  <section className={cn("border border-border/70 bg-muted/20 p-3 text-xs", compact ? "max-w-xl" : "w-full")} aria-label={t("llmUsage.title")}>
    <div className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
      {/* 固定标签和值单元；窄屏自然折成两列 */}
    </div>
  </section>
  ```

  `usage === null` 显示 `llmUsage.noUsage`；两个缓存字段都 `undefined` 才显示 `llmUsage.cacheUnavailable`；若有一个缓存字段，仅显示已报告的项目。资格使用 `usage.metering_eligible === true` 时的 `llmUsage.futureEligible`，否则（包括旧产物的 `undefined`）显示 `llmUsage.localOnly`。这些文案不得称为余额、购买、扣量或账单。

  在两个 JSON locale 根级新增 `llmUsage` 对象，含键 `title`、`provider`、`model`、`calls`、`inputTokens`、`outputTokens`、`totalTokens`、`cacheReadTokens`、`cacheWriteTokens`、`cacheUnavailable`、`futureEligible`、`localOnly`、`noUsage`。在 locale 测试的 `requiredPaths` 中为 en/zh-CN 两者逐项加入这些路径。

- [x] **Step 4: 将聊天用量消息交给共享组件**

  在 `MessageBubble.tsx` 导入 `LLMUsagePanel`，并在 `run_complete` 判断前添加：

  ```tsx
  if (msg.type === "llm_usage") {
    return (
      <div className="flex gap-3">
        <AgentAvatar />
        <LLMUsagePanel usage={msg.llmUsage ?? null} compact />
      </div>
    );
  }
  ```

  禁止将 `msg.content` 或任意原始 SSE 字段传给组件。

- [x] **Step 5: 运行组件、消息和本地化回归测试**

  Run: `cd frontend && npx vitest run src/components/chat/__tests__/LLMUsagePanel.test.tsx src/components/chat/__tests__/MessageBubble.test.tsx src/i18n/__tests__/locales.test.ts`

  Expected: PASS；完整、缓存未提供、空状态、VIP、普通/旧摘要以及中英文均通过。

- [x] **Step 6: 提交共享呈现层**

  ```bash
  git add frontend/src/components/chat/LLMUsagePanel.tsx frontend/src/components/chat/__tests__/LLMUsagePanel.test.tsx frontend/src/components/chat/MessageBubble.tsx frontend/src/components/chat/__tests__/MessageBubble.test.tsx frontend/src/i18n/locales/en.json frontend/src/i18n/locales/zh-CN.json frontend/src/i18n/__tests__/locales.test.ts
  git commit -m "feat: display provider reported LLM usage"
  ```

### Task 5: 在会话流、历史恢复和运行详情接入同一摘要

**Files:**
- Modify: `frontend/src/pages/Agent.tsx:390-424,454-690,1218-1240`
- Modify: `frontend/src/pages/RunDetail.tsx:21-29,250-264`
- Modify: `frontend/src/hooks/__tests__/useSSE.test.ts:182-272`

**Interfaces:**
- Consumes: `parseLLMUsageDelta`、`accumulateLLMUsage`、`api.getRun(runId): Promise<RunData>`、`LLMUsagePanel`。
- Produces: 当前尝试的 `liveLlmUsage: LLMUsageSummary | null`，以及带持久化 `llmUsage` 的 `AgentMessage`。
- Produces: Run Detail 运行概览中的 `<LLMUsagePanel usage={run.llm_usage ?? null} />`。

- [x] **Step 1: 增加 SSE 专用分派测试**

  在 `useSSE.test.ts` 的 event handling describe 块加入：

  ```ts
  it("dispatches llm usage events to the dedicated handler", async () => {
    const events: unknown[] = [];
    const { result } = renderHook(() => useSSE());
    act(() => result.current.connect("http://test/events", { llm_usage: (data) => events.push(data) }));
    await flushTicketRequest();
    act(() => MockEventSource.latest.emit("llm_usage", { iter: 1, input_tokens: 1 }, "usage-1"));
    expect(events).toEqual([{ iter: 1, input_tokens: 1 }]);
  });
  ```

  同时保留现有 `Last-Event-ID` 去重测试，证明同 ID 的重放不会二次调用 handler，进而不会双计。

- [x] **Step 2: 运行 hook 测试确认事件通道行为**

  Run: `cd frontend && npx vitest run src/hooks/__tests__/useSSE.test.ts`

  Expected: PASS；`llm_usage` 已在 `knownTypes`，此测试固定其不被未来重构移除。

- [x] **Step 3: 在 Agent 页面维护实时摘要，并在完成/恢复时采用持久化值**

  在组件状态新增：

  ```tsx
  const [liveLlmUsage, setLiveLlmUsage] = useState<LLMUsageSummary | null>(null);
  ```

  在 `attempt.created` handler 的开头执行 `setLiveLlmUsage(null)`；新增 `llm_usage` handler：调用 `parseLLMUsageDelta(d)`，`null` 时直接返回，否则 `setLiveLlmUsage((current) => accumulateLLMUsage(current, delta))`，然后 `touch()` 与 `scrollToBottom()`。不要把 raw `d` 保存到 Zustand、聊天导出或消息内容。

  将当前流式区域的工具/文本状态容器条件扩展为 `liveLlmUsage != null`，并在工具状态下方渲染：

  ```tsx
  {liveLlmUsage && <LLMUsagePanel usage={liveLlmUsage} compact />}
  ```

  在 `attempt.completed` 已取得 `runId` 后，用同一次 `api.getRun(runId)` 的结果读取 `runData.llm_usage`。若该字段存在，追加 `{ type: "llm_usage", content: "", llmUsage: runData.llm_usage }`；若运行详情成功但无字段，追加同类消息且 `llmUsage: undefined`，展示明确空状态；读取失败时保留 live panel，且不要增加声称已持久化的消息。完成成功或失败时清空 `liveLlmUsage`，避免下一尝试泄漏旧数值。

  在 `loadSessionMessages` 遍历含 `runId` 的历史 assistant message 时，复用既有 `api.getRun(runId)` 调用：无论是否 `isReportWorthyRun`，只要读取成功，就在该运行的 answer/run card 后追加一个 `llm_usage` 消息，将 `llmUsage` 赋为 `runData.llm_usage`。普通聊天、回测和旧运行遵守同一规则；读取失败不影响现有 answer/run card 回退。

- [x] **Step 4: 在 Run Detail 概览复用面板**

  导入 `LLMUsagePanel`，在 `run.metrics` 之后添加：

  ```tsx
  <LLMUsagePanel usage={run.llm_usage ?? null} />
  ```

  该位置使回测报告链接打开的用户也能看到持久化摘要。不要改变 Tabs、图表加载、交易/验证/代码区域或下载功能。

- [x] **Step 5: 运行会话与前端回归测试**

  Run: `cd frontend && npx vitest run src/hooks/__tests__/useSSE.test.ts src/lib/__tests__/llmUsage.test.ts src/components/chat/__tests__/LLMUsagePanel.test.tsx src/components/chat/__tests__/MessageBubble.test.tsx src/i18n/__tests__/locales.test.ts`

  Expected: PASS；确保 SSE 是增量归约、同 Event ID 不会双计，历史/完成读取以 `RunData.llm_usage` 为准，Run Detail 能安全渲染 `undefined` 旧摘要。

- [x] **Step 6: 提交页面集成**

  ```bash
  git add frontend/src/pages/Agent.tsx frontend/src/pages/RunDetail.tsx frontend/src/hooks/__tests__/useSSE.test.ts
  git commit -m "feat: surface live and persisted LLM usage"
  ```

### Task 6: 执行跨层验证与人工安全检查

**Files:**
- Inspect: `agent/src/agent/loop.py`, `agent/tests/test_agent_loop_terminal_state.py`, `agent/tests/test_run_card.py`
- Inspect: `frontend/src/lib/llmUsage.ts`, `frontend/src/components/chat/LLMUsagePanel.tsx`, `frontend/src/pages/Agent.tsx`, `frontend/src/pages/RunDetail.tsx`

**Interfaces:**
- Consumes: 已完成的后端安全摘要与前端展示。
- Produces: 可复现的测试、构建和人工验收证据；不产生新产品代码。

- [x] **Step 1: 运行后端定向回归**

  Run: `pytest agent/tests/test_agent_loop_terminal_state.py agent/tests/test_run_card.py -q`

  Expected: PASS；产物、SSE 和运行详情均覆盖新字段，历史摘要保持可读。

- [x] **Step 2: 运行前端相关测试和完整生产构建**

  Run: `cd frontend && npx vitest run src/hooks/__tests__/useSSE.test.ts src/lib/__tests__/llmUsage.test.ts src/components/chat/__tests__/LLMUsagePanel.test.tsx src/components/chat/__tests__/MessageBubble.test.tsx src/i18n/__tests__/locales.test.ts && npm run build`

  Expected: 所有 Vitest 用例 PASS，随后 `tsc -b && vite build` 成功完成且没有未使用变量、类型错误或 locale JSON 解析错误。

- [x] **Step 3: 做本地人工验收，验证实时和历史的一致性**

  <!-- 2026-07-22: 用户授权跳过。仓库没有可安全调用的无凭据测试 Provider；以聚焦单元测试、页面竞态测试、生产构建和敏感信息负向检查作为替代验证证据。 -->

  使用一个返回 usage 的非生产/测试 Provider 运行一次会话，确认流式区域在每个 `llm_usage` SSE 后累计 calls、input、output、total 与显式缓存；完成后刷新会话并打开 `/runs/<runId>`。三个位置（实时面板、会话历史、Run Detail）必须与该运行的 `llm_usage.json` totals 一致。再检查一个无 usage 或旧产物运行，确认显示“未收到 Provider 报告的用量”或“缓存用量未提供”，而非 `0`。

- [x] **Step 4: 做敏感信息负向检查**

  Run: `rg -n 'api_key|login_token|authorization|secret-prompt|model response' runs/*/llm_usage.json frontend/src/lib/llmUsage.ts frontend/src/components/chat/LLMUsagePanel.tsx 2>/dev/null || true`

  Expected: 实际运行产物和新前端数据/组件代码中没有任何凭据、登录 token、提示词、响应或账户标识。若 `runs/` 下不存在测试运行，记录该前提并以 Task 1 的序列化单元测试作为证据；不得为检查而使用真实凭据。

- [x] **Step 5: 提交验证完成的全部变更并记录范围边界**

  ```bash
  git status --short
  git add agent/src/agent/loop.py agent/tests/test_agent_loop_terminal_state.py agent/tests/test_run_card.py frontend/src/lib/api.ts frontend/src/lib/llmUsage.ts frontend/src/lib/__tests__/llmUsage.test.ts frontend/src/types/agent.ts frontend/src/components/chat/LLMUsagePanel.tsx frontend/src/components/chat/__tests__/LLMUsagePanel.test.tsx frontend/src/components/chat/MessageBubble.tsx frontend/src/components/chat/__tests__/MessageBubble.test.tsx frontend/src/pages/Agent.tsx frontend/src/pages/RunDetail.tsx frontend/src/hooks/__tests__/useSSE.test.ts frontend/src/i18n/locales/en.json frontend/src/i18n/locales/zh-CN.json frontend/src/i18n/__tests__/locales.test.ts
  git commit -m "feat: surface LLM usage in WebUI"
  ```

  最终变更说明必须明确：该功能仅展示本地运行统计和未来资格标签，未实现用户登录联动、购买 200M Token、日重置、月付费、余额、扣量或额度限制；这些行为将来必须由认证的服务端计量网关独立裁决。

## 依赖与执行顺序

1. Task 1 是唯一的后端数据源变更，必须首先完成；Task 2 只验证既有 API 读取面。
2. Task 3 定义 Task 4 和 Task 5 使用的 TypeScript 契约与归约接口。
3. Task 4 建立共享组件后，Task 5 才能把实时、历史和 Run Detail 接到相同视图。
4. Task 6 只在全部实现任务完成后执行；若任一测试失败，回到拥有该接口的任务修复，再完整重跑该任务和 Task 6。

## 规格覆盖自检

- 运行级持久化、SSE 和运行详情读取：Task 1、Task 2、Task 5。
- Provider 报告缓存字段、零值、缺失语义和旧产物兼容：Task 1、Task 2、Task 3、Task 4。
- `vip_server` 未来计量资格与自带 Key 隔离：Task 1、Task 3、Task 4。
- 不泄露 API Key、登录 token、提示词、响应或账户标识：Task 1、Task 3、Task 6。
- 运行中、完成后、历史会话和 Run Detail 共用可见化：Task 4、Task 5、Task 6。
- 明确不实现充值、余额、日/月周期、扣量或拦截：Global Constraints 与 Task 6。

已执行的自检：计划使用的数据契约只定义一次；后续任务引用的 `parseLLMUsageDelta`、`accumulateLLMUsage`、`LLMUsagePanel` 与 `AgentMessage.llmUsage` 均在前序任务中定义；全文没有未解析的占位步骤。
