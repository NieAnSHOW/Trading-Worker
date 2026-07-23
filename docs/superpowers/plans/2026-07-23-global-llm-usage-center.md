---
change: surface-llm-usage
design-doc: docs/superpowers/specs/2026-07-22-surface-llm-usage-design.md
base-ref: 91a5de4
archived-with: 2026-07-23-surface-llm-usage
---

# 全局 LLM 用量中心实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 在不增加采集管线、本地账本或计费能力的前提下，将既有运行级 Provider 用量按 Agent 会话聚合为受认证保护的 API 和独立 /usage 页面，并让 Agent 聊天流不再展示单次用量卡片。

**Architecture:** 后端直接从 SessionStore 枚举全部会话、attempt 与历史助手消息，关联并去重 run ID，严格校验既有 llm_usage.json 后按需生成聚合结果，不写入索引或新产物。GET /usage/llm 只负责认证、查询校验与服务编排。前端仅呈现聚合响应；页面初次进入和用户明确操作时请求，聊天 SSE 仍兼容订阅但忽略 llm_usage。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic v2、zoneinfo、pytest、React 19、TypeScript strict、React Router、ECharts、i18next、Tailwind CSS、Vitest、Testing Library。

## 全局约束

- AgentLoop 保持唯一 Token 与缓存 Token 采集点；本变更仅读取已有运行级 llm_usage.json，前端不得估算或补齐计数。
- 不增加 Tauri 代理、数据库、全局索引、第二套统计管线、依赖、运行产物迁移或自动轮询。
- 只统计可关联到 Agent 会话的运行；attempt 优先于历史助手消息，同一个 run ID 全局只计一次，孤立运行排除。
- 聚合响应最多包含会话 ID、用户可见标题、运行 ID、时间、Provider、模型、计数与既有计量资格；绝不包含 API Key、登录 token、消息正文、attempt prompt、模型响应或账户身份。
- attempt 和消息中的运行引用只接受 `^[A-Za-z0-9_-]{1,128}$` 裸 run ID；所有用量文件都从显式传入的 runs_dir 下读取，禁止绝对路径或 `..` 穿越。
- attempt 和消息中的运行引用只接受 `^[A-Za-z0-9_-]{1,128}$` 裸 run ID；所有用量文件都从显式传入的 runs_dir 下读取，禁止绝对路径或 `..` 穿越。
- 缓存读写 Token 只累计 Provider 明确报告的字段；缺失不代表零，明确报告零必须保留。不得实现费用估算、余额、充值、重置、结算、扣量或额度拦截。
- metering_eligible 仅是 vip_server 的未来协议标记，不能进入账户、授权、支付或配额分支；旧摘要缺少它必须仍可读取。
- start_at、end_at 是可选 ISO 8601 带时区边界，采用 `[start_at, end_at)`；timezone 必须经 ZoneInfo 验证。总览、趋势和分布覆盖完整时间范围，不受 query、page、page_size 影响。
- 新增文案写入现有五个 locale（en、zh-CN、ja、ko、ar）；数字使用 Intl.NumberFormat。小屏将指标、图表与会话明细改为纵向可读布局。

archived-with: 2026-07-23-surface-llm-usage
---

## 文件结构

- 修改 agent/src/session/store.py：增加全量会话、attempt、消息读取，不受 Web 列表限制。
- 新增 agent/src/usage/__init__.py、agent/src/usage/models.py 和 agent/src/usage/llm_aggregation.py：严格聚合响应模型与按需会话用量聚合服务。
- 新增 agent/tests/test_llm_usage_aggregation.py：关联、去重、范围、时区、趋势、分布、缓存覆盖率和文件降级测试。
- 新增 agent/src/api/llm_usage_routes.py 并修改 agent/api_server.py：认证保护的 GET /usage/llm。
- 新增 agent/tests/test_llm_usage_routes.py：认证、参数、线格式、搜索/分页隔离、敏感字段测试。
- 修改 frontend/src/lib/api.ts：聚合类型与 getLLMUsage。
- 新增 frontend/src/pages/Usage.tsx、frontend/src/pages/__tests__/Usage.test.tsx：全局用量中心及其交互测试。
- 修改 frontend/src/router.tsx、frontend/src/components/layout/Layout.tsx、相关 router/layout 测试：懒加载路由与一级导航。
- 修改 frontend/src/i18n/locales/en.json、frontend/src/i18n/locales/zh-CN.json、frontend/src/i18n/locales/ja.json、frontend/src/i18n/locales/ko.json、frontend/src/i18n/locales/ar.json、frontend/src/i18n/__tests__/locales.test.ts：五种语言文案与完整性测试。
- 修改 frontend/src/pages/Agent.tsx 和 frontend/src/pages/__tests__/Agent.attempt-completion.test.tsx：移除聊天流单次用量状态与呈现，保留 RunDetail。

## 共享接口

后端、API 与前端统一以下命名。缓存总数只有至少一个运行明确报告时才出现。

~~~ts
export interface LLMUsageCounters {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
}
export interface LLMUsageAggregateTotals extends LLMUsageCounters {
  sessions: number;
  runs: number;
  cache_read_reported_runs: number;
  cache_write_reported_runs: number;
  missing_usage_runs: number;
  invalid_usage_runs: number;
}
export interface LLMUsageRunRow {
  run_id: string;
  occurred_at: string;
  provider: string;
  model: string | null;
  totals: LLMUsageCounters;
}
export interface LLMUsageDailyBucket { date: string; totals: LLMUsageCounters; }
export interface LLMUsageModelBucket { provider: string; model: string | null; totals: LLMUsageCounters; }
export interface LLMUsageSessionRow {
  session_id: string;
  title: string;
  last_run_at: string;
  totals: LLMUsageCounters;
  runs: LLMUsageRunRow[];
}
export interface LLMUsageAggregateResponse {
  generated_at: string;
  timezone: string;
  period: { start_at: string | null; end_at: string | null };
  totals: LLMUsageAggregateTotals;
  trend: LLMUsageDailyBucket[];
  breakdown: LLMUsageModelBucket[];
  sessions: { items: LLMUsageSessionRow[]; page: number; page_size: number; total_items: number; total_pages: number };
}
~~~

~~~python
class LLMUsageAggregationService:
    def __init__(self, store: SessionStore, runs_dir: Path) -> None: ...

    def aggregate(
        self, *, start_at: datetime | None, end_at: datetime | None,
        timezone_name: str, query: str | None, page: int, page_size: int,
    ) -> LLMUsageAggregateResponse: ...
~~~

### Task 1: 后端会话用量聚合服务

- [x] 6.1 以 TDD 扩展 SessionStore 和后端聚合服务，按 attempt 与历史消息关联/去重运行，覆盖时间范围、IANA 时区、总览、趋势、Provider/模型分布、缓存覆盖率及缺失/损坏文件降级。

**Files:**

- Modify: agent/src/session/store.py:118-214
- Create: agent/src/usage/__init__.py
- Create: agent/src/usage/models.py
- Create: agent/src/usage/llm_aggregation.py
- Create: agent/tests/test_llm_usage_aggregation.py

**Interfaces:**

- Consumes: SessionStore.list_all_sessions() -> list[Session]、list_attempts(session_id: str) -> list[Attempt]、get_all_messages(session_id: str) -> list[Message]。
- Consumes: Attempt.run_dir、Attempt.created_at、助手 Message.metadata 中的 run_id、运行目录 llm_usage.json 与现有严格 LLMUsageSummary。
- Produces: src.usage.models 中 extra="forbid"、strict=True 的聚合 response model，以及 LLMUsageAggregationService(store, runs_dir).aggregate()；会话/运行按最后运行时间降序，趋势按本地日期升序，分布按总 Token 降序再以 provider/model 稳定排序。total_pages 使用 NonNegativeInt，允许空结果为 0；page/page_size 使用 PositiveInt。

- [x] **Step 1: 写失败的 SessionStore 与聚合测试**

  在 agent/tests/test_llm_usage_aggregation.py 用真实 SessionStore(tmp_path / "sessions") 创建两个会话。写入：run-a 同时由一个 attempt 和助手 metadata.run_id 关联、run-b 仅由助手消息关联、run-c 仅由 attempt 关联但没有 llm_usage.json、run-bad 由 attempt 关联且文件违反 LLMUsageSummary、orphan 有文件但不属于任何会话。run-a 与 run-b 使用：

~~~python
def usage(provider: str, model: str | None, total: int, cache_read: int | None = None) -> dict:
    totals = {"input_tokens": total - 2, "output_tokens": 2, "total_tokens": total, "calls": 1}
    iteration = {"iter": 1, "input_tokens": total - 2, "output_tokens": 2, "total_tokens": total}
    if cache_read is not None:
        totals["cache_read_tokens"] = cache_read
        iteration["cache_read_tokens"] = cache_read
    return {"provider": provider, "model": model, "metering_eligible": provider == "vip_server",
            "totals": totals, "per_iteration": [iteration]}

def test_aggregate_links_attempt_and_message_once_and_excludes_orphan(store):
    result = LLMUsageAggregationService(store, runs_dir).aggregate(
        start_at=None, end_at=None, timezone_name="Asia/Shanghai",
        query=None, page=1, page_size=50,
    )
    assert result.totals.total_tokens == 30
    assert result.totals.runs == 2
    assert result.totals.missing_usage_runs == 1
    assert result.totals.invalid_usage_runs == 1
    assert result.totals.cache_read_tokens == 3
    assert result.totals.cache_read_reported_runs == 1
    assert "orphan" not in {run.run_id for row in result.sessions.items for run in row.runs}
~~~

  添加独立测试：固定 attempt 时间 2026-07-01T16:30:00Z 与 message-only 时间 2026-07-02T00:30:00Z；Asia/Shanghai 应把两项放在 2026-07-02 桶，UTC 应为两个桶。使用 `[2026-07-02T00:00:00Z, 2026-07-03T00:00:00Z)` 排除前者并包含后者，恰好等于 end_at 的运行必须排除。断言相同 provider/model 合并、不同组合按 total_tokens 降序；同一 run ID 出现在两个会话时 attempt 所属会话保留，消息 fallback 会话不重复。再写一个 runs_dir 外的哨兵 llm_usage.json，并用 `/absolute/path`、`../escape` 和超长/非法字符 run ID 引用它；断言这些引用被忽略且哨兵内容不进入结果。

- [x] **Step 2: 运行 RED 测试**

  Run: cd agent && pytest tests/test_llm_usage_aggregation.py -q

  Expected: FAIL，提示 src.usage.llm_aggregation 或 SessionStore 全量读取方法不存在。

- [x] **Step 3: 实现严格聚合模型和不受列表上限影响的读取接口**

  在 agent/src/usage/models.py 定义 AggregateTotals、RunRow、DailyBucket、ModelBucket、SessionRow、SessionPage、Period、AggregateResponse。聚合模型使用 ConfigDict(extra="forbid", strict=True)，所有计数使用 NonNegativeInt；page/page_size 使用 PositiveInt，total_items/total_pages 使用 NonNegativeInt，model/start_at/end_at 允许 None。RunRow、DailyBucket、ModelBucket、SessionRow 的 totals 复用现有 LLMUsageTotals；全局 totals 使用 LLMUsageAggregateTotals。序列化时省略未报告缓存字段的 null，不复制原始 artifact 或实体字典。

  在 agent/src/session/store.py 加入并保留原有 list_sessions(limit=50)、get_messages(limit=100) 语义：

~~~python
def list_all_sessions(self) -> List[Session]:
    return self.list_sessions(limit=2**31 - 1)

def list_attempts(self, session_id: str) -> List[Attempt]:
    attempts_dir = self._session_dir(session_id) / "attempts"
    if not attempts_dir.exists():
        return []
    attempts = []
    for attempt_dir in attempts_dir.iterdir():
        if attempt_dir.is_dir():
            data = self._read_json(attempt_dir / "attempt.json")
            if data is not None:
                attempts.append(Attempt.from_dict(data))
    return sorted(attempts, key=lambda item: item.created_at)

def get_all_messages(self, session_id: str) -> List[Message]:
    return self.get_messages(session_id, limit=2**31 - 1)
~~~

- [x] **Step 4: 实现按需聚合与严格降级**

  创建 agent/src/usage/llm_aggregation.py，构造函数保存显式传入的 SessionStore 与 runs_dir；attempt 使用自身 run_dir，消息 fallback 使用 runs_dir / run_id。使用现有 src.api.models.LLMUsageSummary.model_validate_json 严格读取；绝不将原始 dict 加入响应。实现以下关键函数：

~~~python
def _attempt_run_id(attempt: Attempt) -> str | None:
    return _safe_run_id(Path(attempt.run_dir).name) if attempt.run_dir else None

def _safe_run_id(value: str) -> str | None:
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None

def _message_run_refs(messages: Sequence[Message]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for message in messages:
        raw = message.metadata.get("run_id") if message.role == "assistant" and isinstance(message.metadata, dict) else None
        safe = _safe_run_id(raw) if isinstance(raw, str) else None
        if safe is not None:
            refs.setdefault(safe, message.created_at)
    return refs
~~~

  每个会话先读取 attempt run ID 与 created_at，仅把没有 attempt 的 message ref 加入 fallback；两个来源都先经过 `_safe_run_id`。使用全局 claimed_run_ids，先声明全部 attempt 关联，再声明 fallback，确保跨会话 attempt 优先。无论 attempt 原始 run_dir 写了什么，实际文件路径始终重新构造成 `self.runs_dir / safe_run_id / "llm_usage.json"`，不得直接打开持久化绝对路径。时间依次使用可解析 attempt.created_at、对应助手 message.created_at、受约束 run_dir.stat().st_mtime 的 UTC；不得解析 run ID 中的本地无时区时间。先按 `start_at <= occurred_at < end_at` 过滤，再读取和分类用量文件，保证范围外的缺失/损坏运行不进入覆盖率计数。范围内缺少文件使 missing_usage_runs 加一；OSError、JSONDecodeError、ValidationError 使 invalid_usage_runs 加一；其他运行继续。有效摘要累加基本计数到全局、会话、运行、趋势和分布；可选缓存字段只在存在时累计并增加各自 reported_runs。用 ZoneInfo(timezone_name) 得到 date。最后才在会话明细应用 casefold 的 title/session_id query、降序排序与 page slice，不能重新计算 totals、trend 或 breakdown。

- [x] **Step 5: 运行 GREEN 测试**

  Run: cd agent && pytest tests/test_llm_usage_aggregation.py -q

  Expected: PASS；关联、去重、孤立排除、范围、时区、趋势、分布、缓存覆盖率与单个文件降级均有断言证据。

- [x] **Step 6: 提交此独立聚合改动**

~~~bash
git add agent/src/session/store.py agent/src/usage/__init__.py agent/src/usage/models.py agent/src/usage/llm_aggregation.py agent/tests/test_llm_usage_aggregation.py
git commit -s -m "feat: aggregate LLM usage across sessions"
~~~

### Task 2: 聚合 API 契约与认证

- [x] 6.2 新增受认证保护的 `GET /usage/llm` 契约与 API 测试，支持会话搜索和分页，确保搜索/分页不改变完整时间范围的总览统计。

**Files:**

- Create: agent/src/api/llm_usage_routes.py
- Modify: agent/api_server.py:266-300
- Create: agent/tests/test_llm_usage_routes.py

**Interfaces:**

- Consumes: Task 1 LLMUsageAggregationService.aggregate() 和 host api_server.require_auth。
- Produces: GET /usage/llm，查询为 start_at、end_at、timezone、query、page、page_size；response_model 使用 Task 1 的 src.usage.models.LLMUsageAggregateResponse，dependencies=[Depends(require_auth)]。
- Produces: 无效 IANA、无效/无时区 ISO、反向范围、page 小于 1、page_size 不在 1..100 时返回 400。

- [x] **Step 1: 写失败的受认证 API 测试**

  在 agent/tests/test_llm_usage_routes.py 用 fixture 临时替换 api_server.SESSIONS_DIR/RUNS_DIR，同时将 api_server._session_service 与 src.api.state._session_service 都重置为 None，并由 monkeypatch 自动恢复，防止单例跨测试串扰。设置 API_AUTH_KEY 和 api_server._API_KEY；按照 agent/tests/test_security_auth_api.py 的 TestClient 签名兼容模式，在本测试文件定义 `_local_client()` 与 `_remote_client()` helper。写一个会话/attempt；llm_usage.json fixture 包含合法摘要以及 api_key、login_token、prompt、response 哨兵；严格模型应丢弃 extra 字段。断言：

~~~python
response = client.get("/usage/llm", params={
    "timezone": "UTC", "query": "no-match", "page": 1, "page_size": 1,
})
assert response.status_code == 200
body = response.json()
assert body["totals"]["total_tokens"] == 10
assert body["sessions"]["items"] == []
assert "never-return" not in response.text
~~~

  验证 remote client 无凭据和错误 Bearer key 都被拒绝，携带正确 Bearer key 可以成功访问同一端点。对不同 query、page=2 断言 totals、trend、breakdown 相等而 sessions.items/total_items 可变。参数化 timezone=Not/AZone、start_at=not-a-date、无 offset 时间、start_at 大于或等于 end_at、page=0、page_size=101 为 400。成功 response 顶层必须恰为 generated_at、timezone、period、totals、trend、breakdown、sessions。

- [x] **Step 2: 运行 RED 测试**

  Run: cd agent && pytest tests/test_llm_usage_routes.py -q

  Expected: FAIL，GET /usage/llm 为 404，或 response model/注册函数不存在。

- [x] **Step 3: 实现路由与注册**

  创建 agent/src/api/llm_usage_routes.py，沿用 register_runs_routes 的 host 解析模式：

~~~python
@app.get("/usage/llm", response_model=LLMUsageAggregateResponse,
         dependencies=[Depends(require_auth)])
async def get_llm_usage(
    start_at: str | None = Query(None), end_at: str | None = Query(None),
    timezone: str = Query("UTC", min_length=1),
    query: str | None = Query(None, max_length=500),
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
) -> LLMUsageAggregateResponse:
    parsed_start = _parse_iso8601(start_at, "start_at")
    parsed_end = _parse_iso8601(end_at, "end_at")
    if parsed_start and parsed_end and parsed_start >= parsed_end:
        raise HTTPException(status_code=400, detail="start_at must be before end_at")
    _validate_timezone(timezone)
    service = _get_session_service()
    if service is None:
        raise HTTPException(status_code=501, detail="Session runtime not enabled")
    return LLMUsageAggregationService(service.store, service.runs_dir).aggregate(
        start_at=parsed_start, end_at=parsed_end, timezone_name=timezone,
        query=query, page=page, page_size=page_size,
    )
~~~

  _parse_iso8601 只接受 datetime.fromisoformat(value.replace("Z", "+00:00")) 的带时区值并转换 UTC；无效则抛 HTTPException 400。_validate_timezone 使用 ZoneInfo 并将 ZoneInfoNotFoundError 转为 400。将 register_llm_usage_routes(app) 加入 agent/api_server.py 的 register_sessions_routes(app) 后、SPA catch-all 前。

- [x] **Step 4: 运行 GREEN 测试**

  Run: cd agent && pytest tests/test_llm_usage_routes.py -q

  Expected: PASS；认证、参数校验、搜索/分页隔离和结构化敏感字段排除通过。

- [x] **Step 5: 提交此独立 API 改动**

~~~bash
git add agent/src/api/llm_usage_routes.py agent/api_server.py agent/tests/test_llm_usage_routes.py
git commit -s -m "feat: expose aggregated LLM usage API"
~~~

### Task 3: 全局用量中心页面

- [x] 7.1 以 TDD 新增 `/usage` 懒加载页面、侧栏一级入口、聚合 API 类型和国际化界面，实现全部历史/日期筛选、手动刷新、总览、趋势、Provider/模型分布及可展开会话运行明细。

**Files:**

- Modify: frontend/src/lib/api.ts:1-40,420-500
- Create: frontend/src/pages/Usage.tsx
- Create: frontend/src/pages/__tests__/Usage.test.tsx
- Modify: frontend/src/router.tsx:1-70
- Modify: frontend/src/components/layout/Layout.tsx:1-30,300-390
- Modify: frontend/src/components/layout/__tests__/Layout.test.tsx
- Modify: frontend/src/__tests__/router.test.tsx
- Modify: frontend/src/i18n/locales/en.json
- Modify: frontend/src/i18n/locales/zh-CN.json
- Modify: frontend/src/i18n/__tests__/locales.test.ts

**Interfaces:**

- Consumes: api.getLLMUsage(params: { start_at?: string; end_at?: string; timezone: string; query?: string; page?: number; page_size?: number }): Promise<LLMUsageAggregateResponse>。
- Produces: 懒加载路由 /usage，Layout 中使用 ChartNoAxesCombined 的一级 /usage 链接和 layout.usage。
- Produces: Usage 维护 snapshot、refreshError、isLoading、lastSuccessfulAt、range、query、page、expandedSessionId，并通过现有 useDarkMode 让 ECharts 随主题重建；不创建 interval、轮询 timeout 或 SSE。

- [x] **Step 1: 写失败的页面、导航、路由和 locale 测试**

  在 frontend/src/pages/__tests__/Usage.test.tsx mock api.getLLMUsage，返回两天 trend、两个 breakdown、两个 session，首个 runs 含 run-a。固定 Intl.DateTimeFormat().resolvedOptions().timeZone 为 Asia/Shanghai，断言：

~~~tsx
render(<MemoryRouter><Usage /></MemoryRouter>);
await waitFor(() => expect(api.getLLMUsage).toHaveBeenCalledWith({
  timezone: "Asia/Shanghai", page: 1, page_size: 25,
}));
await user.click(screen.getByRole("button", { name: /会话 A/i }));
expect(screen.getByRole("link", { name: /run-a/i })).toHaveAttribute("href", "/runs/run-a");
~~~

  测试近 7 天、近 30 天、本月、自定义日期会发送正确 ISO 边界；搜索与翻页只请求明细，已显示 snapshot.totals 保持不变；手动刷新期间旧总 Token 仍显示，成功后更新时间。测试 runs=0 空态、missing_usage_runs 或 invalid_usage_runs 的局部数据提示、首次失败重试、已有快照后的刷新失败。mock ECharts，仅检查可访问图表容器与传入数据。

  扩展 Layout.test.tsx 检查 /usage href、active class 和 workspace 一级位置；router.test.tsx mock Usage 并导航到 /usage；locale test 断言 en、zh-CN、ja、ko、ar 都有相同 usageCenter 叶子键。

- [x] **Step 2: 运行 RED 测试**

  Run: cd frontend && npx vitest run src/pages/__tests__/Usage.test.tsx src/components/layout/__tests__/Layout.test.tsx src/__tests__/router.test.tsx src/i18n/__tests__/locales.test.ts

  Expected: FAIL，缺少 Usage、getLLMUsage、聚合类型或 usageCenter locale。

- [x] **Step 3: 实现 API 类型与请求方法**

  在 frontend/src/lib/api.ts 定义共享接口中全部 TypeScript 类型。实现：

~~~ts
export type LLMUsageQuery = {
  timezone: string; start_at?: string; end_at?: string;
  query?: string; page?: number; page_size?: number;
};
getLLMUsage: (params: LLMUsageQuery) => {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  return request<LLMUsageAggregateResponse>("/usage/llm?" + query.toString());
},
~~~

  不从运行行、趋势或 breakdown 在浏览器端推算总量、缓存、趋势和分布。

- [x] **Step 4: 实现一次性加载的 Usage 工作台**

  创建 frontend/src/pages/Usage.tsx。mount 时 useEffect 只调用一次 loadUsage(false)。筛选提交、搜索提交、分页、刷新通过用户事件调用同一方法：

~~~tsx
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const loadUsage = async (preserveSnapshot: boolean) => {
  setIsLoading(true); setRefreshError(null);
  try {
    const next = await api.getLLMUsage(buildQuery());
    setSnapshot(next); setLastSuccessfulAt(new Date());
  } catch (error) {
    setRefreshError(error instanceof Error ? error.message : t("usageCenter.loadFailed"));
    if (!preserveSnapshot) setSnapshot(null);
  } finally { setIsLoading(false); }
};
~~~

  buildQuery 默认仅返回 timezone、page=1、page_size=25。all 不含边界；last7/last30 用当前时刻减 7/30 天；month 使用当前本地月首到当前时刻；custom 将开始日当地 `T00:00:00` 作为 start_at，将结束日的次日当地 `T00:00:00` 作为 exclusive end_at，再调用 toISOString。筛选或搜索变动重置 page=1，并为恰好落在 end_at 的运行添加排除测试。

  使用现有 frontend/src/lib/echarts 和 useDarkMode：trend 为 date/total_tokens line series，breakdown 为已排序 provider/model horizontal bar series；图表 effect 依赖 dark，每次 cleanup dispose chart 并移除 ResizeObserver。使用 grid grid-cols-1 gap-3 xl:grid-cols-2；指标使用 minmax(0, 1fr)，图表 min-h-[18rem]，会话小屏垂直布局。展开按钮设置 aria-expanded；仅从 API runs 创建 Link("/runs/" + run.run_id)。totals.runs=0 显示明确无 Provider 报告用量，不把空数据描述为零消耗。缓存以 undefined 判定；出现时展示 reported_runs / totals.runs，缺失时显示未提供。

- [x] **Step 5: 添加路由、导航和同构双语文案**

  在 router.tsx 加入：

~~~tsx
const Usage = lazy(() => import("@/pages/Usage").then((m) => ({ default: m.Usage })));
// Layout children:
{ path: "/usage", element: wrap(Usage) },
~~~

  在 Layout.tsx 导入 ChartNoAxesCombined，在 Agent 附近增加 NavLink，to="/usage"、icon={ChartNoAxesCombined}、label={t("layout.usage")}、isActive={isActive("/usage")}。

  五种 locale 都增加 layout.usage 与 usageCenter：title、refresh、refreshing、lastUpdated、allTime、last7Days、last30Days、thisMonth、customRange、startDate、endDate、totalTokens、inputTokens、outputTokens、calls、sessions、cacheRead、cacheWrite、cacheCoverage、trend、breakdown、sessionDetails、searchSessions、runs、expandSession、collapseSession、empty、partialData、missingUsageRuns、invalidUsageRuns、loadFailed、retry。中文使用“供应商报告的缓存 Token”“部分运行数据不可用”；其他语言使用对应本地化且保持非计费表述。

- [x] **Step 6: 运行 GREEN 测试**

  Run: cd frontend && npx vitest run src/pages/__tests__/Usage.test.tsx src/components/layout/__tests__/Layout.test.tsx src/__tests__/router.test.tsx src/i18n/__tests__/locales.test.ts

  Expected: PASS；证据包括默认全部历史、浏览器时区、日期筛选、刷新保留快照、搜索/分页、可展开安全运行行、运行链接、空态、局部数据提示、错误重试、导航、路由、locale。

- [x] **Step 7: 提交此独立前端改动**

~~~bash
git add frontend/src/lib/api.ts frontend/src/pages/Usage.tsx frontend/src/pages/__tests__/Usage.test.tsx frontend/src/router.tsx frontend/src/components/layout/Layout.tsx frontend/src/components/layout/__tests__/Layout.test.tsx frontend/src/__tests__/router.test.tsx frontend/src/i18n/locales/en.json frontend/src/i18n/locales/zh-CN.json frontend/src/i18n/locales/ja.json frontend/src/i18n/locales/ko.json frontend/src/i18n/locales/ar.json frontend/src/i18n/__tests__/locales.test.ts
git commit -s -m "feat: add global LLM usage center"
~~~

### Task 4: 移除 Agent 聊天流单次用量

- [x] 7.2 从 Agent 实时区域和历史消息中移除单次用量卡片及其页面状态，保留 Run Detail 运行级用量，并更新相关回归测试。

**Files:**

- Modify: frontend/src/pages/Agent.tsx:1-30,250-280,410-450,610-640,700-730,1350-1395
- Modify: frontend/src/pages/__tests__/Agent.attempt-completion.test.tsx:1-280
- Inspect only: frontend/src/pages/RunDetail.tsx:1-280
- Inspect only: frontend/src/components/chat/LLMUsagePanel.tsx:1-90

**Interfaces:**

- Consumes: SSE handler map 中 llm_usage: (data: Record<string, unknown>) => void。
- Produces: Agent 不导入 LLMUsagePanel、LLMUsageSummary、parseLLMUsageDelta、accumulateLLMUsage；不含 liveLlmUsage、setLiveLlmUsage 或 type="llm_usage" 的新增聊天消息。
- Preserves: RunDetail 继续使用 LLMUsagePanel usage={run.llm_usage ?? null}；不改 SSE 后端、MessageBubble 兼容分支或运行详情契约。

- [x] **Step 1: 写失败的 Agent 回归测试**

  删除现有 LLMUsagePanel mock 和所有 live-usage 正向断言。增加：

~~~tsx
it("ignores llm_usage events without a chat message or live card", async () => {
  renderAgent();
  await waitFor(() => expect(connectMock).toHaveBeenCalled());
  await act(async () => {
    handlersRef.current.llm_usage({
      iter: 1, provider: "vip_server", model: "hosted", metering_eligible: true,
      input_tokens: 8, output_tokens: 2, total_tokens: 10,
    });
  });
  expect(useAgentStore.getState().messages.some((message) => message.type === "llm_usage")).toBe(false);
  expect(screen.queryByTestId("live-usage")).not.toBeInTheDocument();
  expect(apiMock.getRun).not.toHaveBeenCalled();
});
~~~

  加一条恢复历史助手消息的测试：即使 getRun 返回 llm_usage，store 中也不出现 type="llm_usage" 的消息；既有 answer/run_complete 是否出现仍按原逻辑判断。保留并改写既有 completion 竞态测试，只验证早晚 completion 维持当前对话与工具状态；验证 llm_usage 后仍能处理 attempt.completed、tool_start、assistant 文本。

- [x] **Step 2: 运行 RED 测试**

  Run: cd frontend && npx vitest run src/pages/__tests__/Agent.attempt-completion.test.tsx

  Expected: FAIL，当前实现会解析事件、更新 liveLlmUsage、插入 llm_usage 消息或渲染 live panel。

- [x] **Step 3: 删除聊天流用量状态和渲染**

  从 Agent.tsx 删除 LLMUsagePanel、llmUsage helper、LLMUsageSummary imports 和：

~~~tsx
const [liveLlmUsage, setLiveLlmUsage] = useState<LLMUsageSummary | null>(null);
~~~

  删除创建/取消/完成/失败/session 切换中的 setLiveLlmUsage；删除恢复和 completion 中 getRun 后把 llm_usage 转成 store addMessage 的分支。保留 metrics、run completion、普通 assistant 消息与工具状态逻辑。SSE handler 显式保留：

~~~tsx
llm_usage: (_data) => {
  // The protocol remains subscribed for backward compatibility; Agent chat deliberately ignores usage.
},
~~~

  从 streaming placeholder 与进度条件移除 liveLlmUsage 判断，删除 panel JSX。不要删除 MessageBubble 的旧兼容分支，也不要改 RunDetail。

- [x] **Step 4: 运行 GREEN 测试**

  Run: cd frontend && npx vitest run src/pages/__tests__/Agent.attempt-completion.test.tsx

  Expected: PASS；llm_usage 无害忽略，Agent 没有单次用量卡片，完成事件和会话流程保持正常，RunDetail 仍是运行级追溯入口。

- [x] **Step 5: 提交此独立呈现删除改动**

~~~bash
git add frontend/src/pages/Agent.tsx frontend/src/pages/__tests__/Agent.attempt-completion.test.tsx
git commit -s -m "refactor: remove LLM usage from agent chat"
~~~

### Task 5: 修订后的完整验证

- [x] 8.1 运行新增后端与前端定向测试、locale 完整性测试和前端生产构建，并验证聚合端点不暴露凭据、消息正文、运行 prompt 或模型响应。

**Files:**

- Modify if verification exposes a defect: only production/test files owned by Tasks 1-4.
- Inspect: agent/tests/test_llm_usage_aggregation.py、agent/tests/test_llm_usage_routes.py、frontend/src/pages/__tests__/Usage.test.tsx、frontend/src/pages/__tests__/Agent.attempt-completion.test.tsx、frontend/src/i18n/__tests__/locales.test.ts、frontend/src/pages/RunDetail.tsx。

**Interfaces:**

- Consumes: Task 1 聚合服务、Task 2 路由/严格模型、Task 3 页面/API 类型、Task 4 Agent no-op。
- Produces: 可重复的验证证据，证明不存在自动轮询、账户/费用功能或敏感数据泄露。

此任务只验证 Tasks 1-4 已实现的行为，不新增生产能力。TDD 的 RED/GREEN 实现循环已经在前四个任务完成；本任务以现有测试全部通过和安全白名单断言为验收证据，不为制造 RED 而故意破坏正确实现。

- [x] **Step 1: 增加端到端安全白名单断言**

  在 test_llm_usage_routes.py 的成功 fixture 中分别写入 api-key-sentinel、login-token-sentinel、message-body-sentinel、attempt-prompt-sentinel、model-response-sentinel。断言：

~~~python
for secret in (
    "api-key-sentinel", "login-token-sentinel", "message-body-sentinel",
    "attempt-prompt-sentinel", "model-response-sentinel",
):
    assert secret not in response.text
row = response.json()["sessions"]["items"][0]
assert set(row) == {"session_id", "title", "last_run_at", "totals", "runs"}
assert set(row["runs"][0]) == {"run_id", "occurred_at", "provider", "model", "totals"}
~~~

  标题单独用用户可见 title-sentinel 断言可返回，证明仅标题可见。Usage.test.tsx 断言页面不显示 fixture 中未定义 prompt/response。

- [x] **Step 2: 运行安全 RED 或现有通过证据**

  Run: cd agent && pytest tests/test_llm_usage_routes.py -q

  Expected: 任何 response model、服务或路由泄露 raw artifact、消息、attempt 字段即 FAIL；若 Task 2 已正确完成则 PASS，作为现有实现满足安全契约的证据。

- [x] **Step 3: 只在失败时进行最小白名单修正**

  若 Step 2 失败，保持 agent/src/usage/models.py 聚合模型 extra="forbid"，在 llm_aggregation.py 仅构造 SessionRow 的 session_id、title、last_run_at、totals、runs 和 RunRow 的 run_id、occurred_at、provider、model、totals。不得使用 Message.to_dict、Attempt.to_dict、原始 artifact 合并或字符串过滤。完成后重跑：

~~~bash
cd agent && pytest tests/test_llm_usage_routes.py -q
~~~

  Expected: PASS，所有秘密哨兵不在 response 中，仅用户可见 title 可见。

- [x] **Step 4: 运行全部定向验证**

~~~bash
cd agent && pytest tests/test_llm_usage_aggregation.py tests/test_llm_usage_routes.py -q
cd frontend && npx vitest run src/pages/__tests__/Usage.test.tsx src/pages/__tests__/Agent.attempt-completion.test.tsx src/components/layout/__tests__/Layout.test.tsx src/__tests__/router.test.tsx src/i18n/__tests__/locales.test.ts
cd frontend && npm run build
~~~

  Expected: 三个命令全部 exit 0；pytest 覆盖聚合、认证、范围和安全，Vitest 覆盖页面/导航/聊天回归/locales，构建完成 tsc -b 与 vite build 且无 strict TypeScript 错误。

- [x] **Step 5: 进行最终非网络行为检查**

  确认 api.getLLMUsage 仅由 Usage 的 mount 与用户事件调用；Usage.tsx 不含 setInterval、重复定时 setTimeout 或 SSE hook；没有新增 Tauri 命令、数据库迁移、费用/余额/支付/配额字段；Agent.tsx 不含 liveLlmUsage、setLiveLlmUsage、addMessage({ type: "llm_usage"；RunDetail.tsx 仍含 LLMUsagePanel usage={run.llm_usage ?? null}。将结果写入实施 PR 描述或执行日志，不创建产品文件。

- [x] **Step 6: 只在验证修复产生改动时提交**

~~~bash
git add agent/src/usage/models.py agent/src/api/llm_usage_routes.py agent/src/usage/llm_aggregation.py agent/tests/test_llm_usage_routes.py frontend/src/lib/api.ts frontend/src/pages/Usage.tsx frontend/src/pages/__tests__/Usage.test.tsx frontend/src/pages/Agent.tsx frontend/src/pages/__tests__/Agent.attempt-completion.test.tsx
git commit -s -m "test: verify global LLM usage center"
~~~

  若所有验证直接通过且验证本身没有修改工作树，不执行 commit；Tasks 1-4 的提交已构成完整实现历史。

## 自检

- **Spec coverage:** Task 1 覆盖关联、attempt 优先、消息 fallback、去重、孤立排除、范围、时区、总览、趋势、分布、缓存覆盖率和文件降级。Task 2 覆盖认证、查询契约、搜索/分页隔离。Task 3 覆盖页面、导航、筛选、刷新、图表、明细、空态/失败态、国际化和不轮询。Task 4 删除聊天流用量并保留 RunDetail。Task 5 覆盖定向测试、locale、生产构建和敏感字段白名单。费用估算、Swarm、自动轮询、数据库、Tauri 代理和账户能力均不在范围内。
- **Placeholder scan:** 已复查所有文件路径、接口、命令、预期结果和实现步骤；没有未决占位或依赖读者猜测的步骤。
- **Type consistency:** 运行、趋势、分布、会话使用 LLMUsageCounters；仅全局 totals 使用 LLMUsageAggregateTotals。服务、路由、前端查询固定使用 start_at、end_at、timezone、query、page、page_size；Provider、模型、缓存和时间字段与既有运行级契约一致。

计划已保存到 docs/superpowers/plans/2026-07-23-global-llm-usage-center.md。执行时可选择：

1. Subagent-Driven：每个 Task 使用独立执行者并在提交前复审。
2. Inline Execution：在当前会话按任务顺序执行并在每个提交后复核。
