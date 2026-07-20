---
change: investment-news-hub
reviewed_commit_range: 0110027377a045c029f8d67f37bfc96ae823b18c..de13f3863da610ce05434cc99d8e7bbbd2935440
reviewer_lane: task-13-independent-verifier
reviewed_at: 2026-07-21
result: FAIL
---

# 投资资讯模块独立构建审查

## 结论

本 change 尚未达到 OpenSpec 验收条件。当前仍有 1 个 P1 和 4 个 P2 未关闭，因此 OpenSpec 7.5 必须保持未勾选。后端、前端、生产构建和拦截式浏览器目标测试均通过，但这些测试没有覆盖下述 TLS 连接复用和中文输出校验阻塞项。

本次审查未运行真实 feed、真实 LLM、live trading、broker-write、支付或钱包流程。

## P0

未发现 P0。

## P1

### P1-1：IP 固定后的重定向可跨 hostname 复用 TLS 连接

证据：

- 技术设计要求关闭 keep-alive，避免解析到同一 IP 的不同 hostname 复用错误的 TLS 连接：`docs/superpowers/specs/2026-07-20-investment-news-hub-design.md:154`。
- `agent/src/news/network.py:83` 为整个重定向循环创建同一个 `httpx.AsyncClient`，但没有设置 `httpx.Limits(max_keepalive_connections=0)` 或等价的连接关闭策略。
- 每个请求 URL 都被改写为选定 IP（`agent/src/news/network.py:96-103`），httpcore 会按改写后的 IP origin 建立连接池。如果 `feed-a.example` 重定向到 `feed-b.example`，且两者解析到同一 IP/端口，第二个请求可复用第一个 TLS 连接；复用连接时，第二个请求的 `sni_hostname` extension 不会触发新的握手。
- 现有 `FakeTransport` 测试只断言 request extension，没有执行连接池复用或 TLS 握手。

影响：跨 hostname 重定向可能绕过预期的逐跳 hostname 证书校验，削弱设计中的 SSRF/IP pinning 边界，并可能接受只为前一个 hostname 完成身份验证的连接所返回的内容。

必须修复：为该客户端关闭 keep-alive，或采用其他按逻辑 hostname 隔离/关闭连接的方案；补充“不同 hostname、相同 IP”重定向必须建立不同连接的回归测试，再申请独立复审。

## P2

### P2-1：标记为中文的模型输出没有验证中文字符

证据：

- canonical delta spec 要求 `title_zh` 包含中文字符，并要求 3-5 条中文要点：`openspec/changes/investment-news-hub/specs/investment-news-hub/spec.md:52-68`。
- `LocalizedTitle.title_zh` 与 `TrackLLMOutput.highlights` 只校验长度和数量（`agent/src/news/llm.py:23-38`）。`_enrich_track()` 随后直接持久化这些字符串并将 AI 标记为可用（`agent/src/news/llm.py:67-81`）。
- 纯本地 FakeLLM 复现返回 `title_zh='English only'`、`highlights=['one', 'two', 'three']`，结果仍为 `ai_available=True`。
- `agent/tests/news/test_llm_enrichment.py:220-229` 明确把 300 个 ASCII `x` 组成的要点视为有效，当前测试契约与 spec 相反。

影响：无效本地化结果可以覆盖原始标题，非中文要点也会以成功 AI 结果进入快照，而不是回退或将该赛道 AI 标记为不可用。

必须修复：校验本地化标题和每条要点包含中文字符；增加纯 ASCII 输出的拒绝测试；保持原始标题回退语义。

### P2-2：运行时加载清单时没有强制校验固定 manifest 与 golden hash

证据：

- 设计要求在启动/测试时校验 repository、完整 commit SHA、108 mappings、106 endpoints、12 tracks、ID、URL scheme 和 golden hash：`docs/superpowers/specs/2026-07-20-investment-news-hub-design.md:88`。
- `agent/src/news/catalog.py:43-62` 解析 `upstream_manifest.json` 后直接信任其中所有值，没有读取 `upstream_manifest.sha256`、对比编译进代码的 golden digest，或校验固定 repository/commit/count/ID/track/URL 不变量。
- digest 检查只存在于 `agent/tests/news/test_catalog.py:62-74`，打包后的运行时不会调用该检查。

影响：损坏或被意外替换的打包 manifest 仍可构造 coordinator，静默改变声称固定的来源目录。源码测试能证明本次审查的文件内容，但不能落实设计声明的运行时边界。

必须修复：让 `load_catalog()` 对 digest 和目录不变量不匹配进行 fail closed，并增加针对打包资源的 mutation 测试。

### P2-3：change 删除了 40 个无关的规范与审查产物

证据：

- `git diff --name-only --diff-filter=D 0110027..de13f38` 返回 40 个删除文件。
- 删除范围包括既有 watchlist、market dashboard、indices 的 OpenSpec archive/main specs、Superpowers plan/design/report，以及根目录 `task.md`。
- 这些删除由 `367e7e68d4aa665a85e31573469a9d403e3f618d` 引入，但资讯 proposal、design、migration 和 rollback 均未把它们声明为 `investment-news-hub` 的组成部分。

影响：合并资讯功能会同时抹除无关项目历史和 canonical capability specs，未经过对应 change contract 即扩大了审查及回滚范围。

必须修复：恢复无关产物，或将删除操作移到单独 proposal 和独立审查的 change。

### P2-4：审查区间内有一个提交缺少项目要求的 DCO trailer

证据：

- 项目说明要求 community commit 带 `Signed-off-by:` trailer。
- `git show -s --format='%B' 367e7e68d4aa665a85e31573469a9d403e3f618d` 没有 trailer；审查区间内其余提交均包含 trailer。

影响：当前分支不满足仓库声明的贡献政策。

必须修复：处理 P2-3 时一并修复 unsigned commit 历史，并对完整审查区间重跑 DCO 扫描。

## P3

### P3-1：进入 XML 解析前没有校验 feed 响应类型

设计要求只有类型和内容可作为 XML/feed 处理的响应才进入 parser（`docs/superpowers/specs/2026-07-20-investment-news-hub-design.md:158`）。客户端虽然发送 XML `Accept` header，但会把任意 2xx body 交给 parser，不检查 `Content-Type`（`agent/src/news/network.py:95-125`）。2 MiB 限制与安全 parser 降低了主要风险，但仍应补充明确的 content policy 和测试以匹配设计。

### P3-2：viewport evidence 生成器没有持久化原始宽度值

`frontend/e2e/news-responsive.spec.ts:216-219` 使用 `scrollWidth > innerWidth` 断言布尔结果；生成的 evidence 只记录 PASS，没有保存 `scrollWidth` 或 `clientWidth`。本次独立审查已在下方补录实际值，但建议 evidence 生成器今后直接持久化这些值。

### P3-3：全仓测试基线不是 green

Task 12 在 `.superpowers/sdd/task-12-report.md` 中记录了 collection blockers 和无关失败。资讯目标测试通过，但该提交区间没有 fresh 的全仓 green 信号。这是残余集成风险，本报告不将其归因为资讯模块回归。

## Spec 覆盖

| 审查项 | 结果 | 证据 |
| --- | --- | --- |
| 免费 `/news` 范围，无产品登录/付费门控 | PASS | lazy `/news` 路由与侧栏入口存在；没有资讯专用账户或权益门控 |
| 108 mappings / 106 endpoints / 12 tracks | PASS | manifest 静态查询返回 108 / 106 / 12；catalog 测试覆盖唯一 ID 与两个复用 URL |
| 固定上游 SHA 与 golden hash | FAIL | tracked SHA 为 `d98aa603...`，文件 digest 为 `5471c28b...`，但运行时不校验二者（P2-2） |
| 源码/wheel/sdist/desktop 中的完整 MIT 条款 | PASS | `agent/src/news/THIRD_PARTY_NOTICES.md` 含完整 MIT 文本；Task 12 archive 和 desktop 检查通过 |
| 单 FastAPI sidecar | PASS | 路由注册到 `agent/api_server.py`；未新增第二个 server/process |
| 现有 `require_auth` 边界 | PASS | 三个端点均通过 `Depends(require_auth)` 注册；remote/loopback 测试通过 |
| SSRF、redirect 校验、DNS/IP pinning | FAIL | 公网 IP 与逐跳 redirect 检查存在，但逻辑 hostname 的 TLS 隔离不完整（P1-1） |
| DTD/XXE/entity 处理 | PASS | 延迟导入 `defusedxml` 并拒绝声明；fixture 测试通过 |
| 仅允许 HTTP(S) article URL | PASS | backend model/pipeline 与 frontend parser/rendering 均拒绝其他 scheme |
| 凭据与错误脱敏边界 | PASS | 稳定 public error、URL 凭据拒绝、access-log 脱敏，且不传播原始 Provider 异常 |
| 赛道级 fresh/stale/unavailable | PASS | pipeline/coordinator 和端到端降级测试覆盖三种状态与 no-update 保留 |
| 每刷新一个 LLM 实例 | PASS | `enrich_tracks()` 只构造一次并共享；FakeLLM 测试断言一次 factory call |
| 每更新赛道一次调用 | PASS | updated-track indexes 为每个更新赛道创建一次 `enrich_one` |
| 最多 16 候选 / LLM 并发 3 | PASS | 常量与测试固定 16，测试观测 peak concurrency 为 3 |
| 中文标题/要点校验 | FAIL | 纯 ASCII 输出会被接受为 available（P2-1） |
| 无原文全文抓取 / 无历史 | PASS | 仅固定 catalog feed endpoint 被请求；storage 只有一个 `news/latest.json`，不存在历史 UI/API |
| 两个 frontend viewport | PASS | intercepted Chromium 在 390x844 和 1440x900 通过；截图已人工检查 |

## 验证证据

后端目标测试排除了唯一会 clone 上游 Git 仓库的用例，使本次审查保持离线：

```text
pytest agent/tests/news agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -W error::RuntimeWarning -q -k 'not importer_reads_git_objects'
205 passed, 2 skipped, 1 deselected, 21 warnings in 15.06s
```

前端目标测试与生产构建：

```text
npx vitest run src/lib/__tests__/api.test.ts src/hooks/__tests__/useNews.test.tsx src/pages/__tests__/News.test.tsx src/pages/__tests__/News.layout.test.tsx src/components/layout/__tests__/Layout.test.tsx src/i18n/__tests__/newsLocales.test.ts src/__tests__/viteProxy.test.ts
7 files passed; 67 tests passed

npm run build
tsc -b && vite build: exit 0（仅有既有 chunk-size warning）
```

拦截式 Chromium viewport 验证：

```text
npx playwright test e2e/news-responsive.spec.ts
2 passed (4.0s)

mobile 390x844: scrollWidth=390, clientWidth=390, innerWidth=390
desktop 1440x900: scrollWidth=1440, clientWidth=1440, innerWidth=1440
```

截图：

- `docs/superpowers/reviews/assets/investment-news-mobile.png`（390x844，64,868 bytes）
- `docs/superpowers/reviews/assets/investment-news-desktop.png`（1440x900，83,365 bytes）

静态检查：

```text
manifest: 12 tracks, 108 source mappings, 106 unique URLs
sha256(upstream_manifest.json): 5471c28bd8ad6fe13af7a6d335073fb8dad90ddb316269e092071412c9d8d3f6
git diff --check 0110027..de13f38: exit 0
```

## 验收门禁

- 阻塞发现：1 个 P1，4 个 P2。
- OpenSpec 7.5 保持未勾选。
- 所有 P1/P2 关闭后，必须在新的独立 reviewer/verifier lane 重新执行 Task 13。
