---
change: investment-news-hub
design-doc: docs/superpowers/specs/2026-07-20-investment-news-hub-design.md
base-ref: 0110027377a045c029f8d67f37bfc96ae823b18c
---

# 投资资讯模块实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 FastAPI sidecar 与 React SPA 中交付免费的 `/news` 投资资讯模块，原生复用固定上游来源与项目现有 LLM 配置。

**Architecture:** 后端新增 `src.news` 领域包，按固定 catalog、受限网络采集、安全 feed 解析、赛道级合并、LLM enrich、原子快照与进程内协调器分层；FastAPI 仅适配三个 `/news-api` 端点。前端通过现有 API client、懒加载路由和页面内 hook 展示组合快照并仅在任务运行时轮询。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic v2、httpx、defusedxml、pytest；React 19、TypeScript、Vite、Vitest、Testing Library、Tailwind CSS、lucide-react。

## Global Constraints

- OpenSpec delta spec 是行为事实源；Design Doc 是实现事实源，不得新增 `llm.config.json`。
- 固定上游仓库 `https://github.com/simonlin1212/investment-news` 和提交 `d98aa603228f4839fb48859812c63a58ca10cead`。
- Catalog 必须保持 108 个唯一 `(track_id, url)` 映射、106 个唯一 feed endpoint、12 个稳定赛道；两个跨赛道复用 URL 每次刷新只请求一次。
- 每次手动刷新覆盖全部 12 赛道；只有至少一个赛道得到本次有效条目时才提交组合快照。
- 来源并发 16，连接/读取超时 5/15 秒，单 feed 2 MiB，重定向最多 3 跳，每 endpoint 最多 6 条，窗口 7 天，每赛道最多 100 条。
- 标题/摘要/单条 AI 要点上限 300/1000/300 字符；每个更新赛道最多 16 个 LLM 候选、一次结构化调用、3-5 条要点，总 LLM 并发最多 3；每刷新只调用一次 `build_llm()`。
- 磁盘只保存 `get_data_dir()/news/latest.json`；任务状态只驻进程内存，不保存历史、全文、搜索、收藏、定时任务或推送。
- 所有 API 复用 `require_auth`；日志、API、快照与任务状态不得包含凭据、原始 Provider 异常、提示词或完整带 query 的 URL。
- 常规测试不得访问真实 feed 或真实 LLM；每个实现任务遵循 RED-GREEN-REFACTOR，并使用 `git commit -s`。

---

## 文件结构

| 路径 | 责任 |
| --- | --- |
| `scripts/news/import_upstream.py` | 从固定 checkout 可重复生成 manifest、hash 与 MIT notice |
| `agent/src/news/catalog.py` | 加载 catalog、验证 108/106/12 不变量并生成 endpoint 分组 |
| `agent/src/news/models.py` | 领域、快照、API 与刷新状态 Pydantic 模型 |
| `agent/src/news/storage.py` | 最新快照读取、0600 原子提交与损坏隔离 |
| `agent/src/news/network.py` | 公网 IP 固定、Host/SNI 保留、重定向与有界响应读取 |
| `agent/src/news/feeds.py` | 延迟导入 defusedxml，解析 RSS/Atom 并纯文本化 |
| `agent/src/news/pipeline.py` | 过滤、去重、排序、时间窗口与赛道级新旧合并 |
| `agent/src/news/llm.py` | 每赛道一次结构化 enrich、校验与降级 |
| `agent/src/news/coordinator.py` | 单任务状态机与全刷新编排 |
| `agent/src/api/news_routes.py` | `/news-api` HTTP 适配与鉴权依赖 |
| `frontend/src/hooks/useNews.ts` | snapshot 初始化、运行期轮询和页面状态 |
| `frontend/src/pages/News.tsx` | 响应式赛道切换、AI 要点和资讯列表 |

## OpenSpec 进度映射

| 计划任务 | 完成后可勾选的 OpenSpec 项 |
| --- | --- |
| Task 1 | 1.1、1.2；1.3 等待 Task 12 的发布物验证 |
| Task 2 | 1.4、2.1、2.2、2.3 |
| Task 3 | 3.2 的网络部分，等待 Task 4 完成解析部分 |
| Task 4 | 3.1、3.2 |
| Task 5 | 3.3 的 pipeline 部分，等待 Task 7 完成协调器验证 |
| Task 6 | 4.1、4.2、4.3 |
| Task 7 | 3.3、5.1、5.2 |
| Task 8 | 5.3、5.4、5.5 |
| Task 9 | 6.1；6.5 等待 Task 11 完成页面验证 |
| Task 10 | 6.4 的轮询部分，等待 Task 11 完成视图状态 |
| Task 11 | 6.2、6.3、6.4、6.5 |
| Task 12 | 1.3、7.1、7.2、7.3、7.4；7.5 等待 Task 13 独立审查 |
| Task 13 | 7.5，仅由未参与实现的 reviewer/verifier 在审查通过后勾选 |

每个任务通过 GREEN、Comet build 阶段由用户选择的 `review_mode` gate 和定向验证后，立即更新上表中已完整覆盖的 `tasks.md` checkbox，并把 `tasks.md` 纳入该任务提交；标记“等待”的项不得提前勾选。无论 `review_mode` 取值为何，Task 13 的独立 spec 审查都是 7.5 的额外硬条件。

### Task 1: 固定上游 Catalog、Golden Hash 与 MIT Notice

**Files:**
- Create: `scripts/news/import_upstream.py`
- Create: `agent/src/news/__init__.py`
- Create: `agent/src/news/catalog.py`
- Create: `agent/src/news/upstream_manifest.json`
- Create: `agent/src/news/upstream_manifest.sha256`
- Create: `agent/src/news/THIRD_PARTY_NOTICES.md`
- Create: `agent/tests/news/test_catalog.py`
- Modify: `pyproject.toml`
- Modify: `MANIFEST.in`

**Interfaces:**
- Consumes: 上游 Git object database 中固定 commit 的 `sources.json` 与 `LICENSE`，禁止读取 checkout 工作树文件。
- Produces: `load_catalog() -> NewsCatalog`、`group_endpoints(catalog) -> tuple[FeedEndpoint, ...]`，供采集器与协调器使用。

- [x] **Task 1 Step 1: 写 catalog 不变量失败测试**

```python
def test_fixed_catalog_shape_and_known_reuse() -> None:
    catalog = load_catalog()
    assert len(catalog.tracks) == 12
    assert len(catalog.assignments) == 108
    endpoints = group_endpoints(catalog)
    assert len(endpoints) == 106
    reused = {item.url: len(item.assignments) for item in endpoints if len(item.assignments) > 1}
    assert reused == {
        "https://sspai.com/feed": 2,
        "https://www.engadget.com/rss.xml": 2,
    }
    assert len({(item.track_id, item.url) for item in catalog.assignments}) == 108

def test_import_ignores_dirty_worktree_files(tmp_path: Path) -> None:
    repository = make_upstream_git_fixture(tmp_path)
    fixture_commit = git_output(repository, "rev-parse", "HEAD")
    committed_sources = git_show(repository, f"{fixture_commit}:sources.json")
    (repository / "sources.json").write_text('{"sections": []}', encoding="utf-8")
    generated = read_upstream_commit(repository, fixture_commit)
    assert generated.raw_sources == committed_sources
```

- [x] **Task 1 Step 2: 运行 RED**

Run: `pytest agent/tests/news/test_catalog.py -q`

Expected: FAIL，因为 `src.news.catalog` 与固定 manifest 尚不存在。

- [x] **Task 1 Step 3: 实现可重复导入与 catalog 校验**

`import_upstream.py` 必须接受 `--repository`、`--commit`、`--output-dir`，先拒绝任何不等于 `d98aa603228f4839fb48859812c63a58ca10cead` 的 commit，再用 `git cat-file -e "$commit^{commit}"` 验证对象存在，只通过 `git show "$commit:sources.json"` 与 `git show "$commit:LICENSE"` 读取固定对象；不得打开工作树中的同名文件。按稳定 key 排序写 JSON，复制完整 MIT 文本并写 manifest 与 notice SHA-256。测试同时覆盖非固定 commit 被拒绝。`catalog.py` 暴露不可变结构：

```python
@dataclass(frozen=True)
class SourceAssignment:
    id: str
    track_id: str
    name: str
    url: str
    filters: tuple[str, ...]

@dataclass(frozen=True)
class FeedEndpoint:
    id: str
    url: str
    assignments: tuple[SourceAssignment, ...]

def group_endpoints(catalog: NewsCatalog) -> tuple[FeedEndpoint, ...]:
    grouped: dict[str, list[SourceAssignment]] = {}
    for assignment in catalog.assignments:
        grouped.setdefault(assignment.url, []).append(assignment)
    return tuple(
        FeedEndpoint(
            id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
            url=url,
            assignments=tuple(sorted(items, key=lambda item: item.id)),
        )
        for url, items in sorted(grouped.items())
    )
```

- [x] **Task 1 Step 4: 生成固定产物并接入 Python 包**

Run:

```bash
tmp="$(mktemp -d)"
git clone --filter=blob:none https://github.com/simonlin1212/investment-news "$tmp/upstream"
git -C "$tmp/upstream" checkout d98aa603228f4839fb48859812c63a58ca10cead
python scripts/news/import_upstream.py --repository "$tmp/upstream" --commit d98aa603228f4839fb48859812c63a58ca10cead --output-dir agent/src/news
```

在 `pyproject.toml` 的 `src` package-data 加入 `news/*.json`、`news/*.sha256`、`news/*.md`；在 `MANIFEST.in` 加入 `recursive-include agent/src/news *.json *.sha256 *.md`；在 `[project.optional-dependencies].dev` 加入 `build>=1.2`，让干净开发环境可执行发布物验证。

- [x] **Task 1 Step 5: 运行 GREEN 与包内容检查**

Run: `pytest agent/tests/news/test_catalog.py -q`

Expected: PASS，显示 `108/106/12` 不变量与两个复用 URL 均通过。

- [x] **Task 1 Step 6: 提交**

```bash
git add scripts/news agent/src/news pyproject.toml MANIFEST.in agent/tests/news/test_catalog.py openspec/changes/investment-news-hub/tasks.md
git commit -s -m "feat(news): vendor fixed investment news catalog"
```

### Task 2: 快照模型与原子存储

**Files:**
- Create: `agent/src/news/models.py`
- Create: `agent/src/news/storage.py`
- Create: `agent/tests/news/test_models.py`
- Create: `agent/tests/news/test_storage.py`

**Interfaces:**
- Consumes: Task 1 的稳定 track/assignment ID。
- Produces: `NewsSnapshot`、`TrackSnapshot`、`RefreshStatus`、`SnapshotResponse`、`AtomicSnapshotStore.read()` 与 `AtomicSnapshotStore.write(snapshot)`。

- [x] **Task 2 Step 1: 写 schema 和存储失败测试**

```python
def test_unavailable_track_invariant() -> None:
    track = TrackSnapshot(
        id="ai",
        state="unavailable",
        generated_at=None,
        stale=False,
        partial=False,
        items=[],
        ai=TrackAIResult(available=False, generated_at=None, highlights=[], error_code=None),
        source_stats=SourceStats.empty(),
    )
    assert track.generated_at is None

def test_atomic_store_keeps_old_snapshot_on_replace_failure(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    store = AtomicSnapshotStore(tmp_path / "news" / "latest.json")
    old = make_snapshot(title="old")
    store.write(old)
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk")))
    with pytest.raises(SnapshotWriteError):
        store.write(make_snapshot(title="new"))
    assert store.read().tracks[0].items[0].title_original == "old"
```

- [x] **Task 2 Step 2: 运行 RED**

Run: `pytest agent/tests/news/test_models.py agent/tests/news/test_storage.py -q`

Expected: FAIL，因为模型与存储类尚不存在。

- [x] **Task 2 Step 3: 实现模型不变量**

模型使用 `extra="forbid"`，UTC datetime，条目标题/摘要 `max_length=300/1000`，赛道条目 `max_length=100`。`TrackSnapshot` model validator 强制 `fresh/stale/unavailable` 与 `generated_at/stale/items` 一致；`NewsSnapshot` model validator 强制赛道 ID 和顺序精确等于 catalog 的 `ai/semi/robot/auto/energy/bio/space/security/tech/consumer/macro/science`，不能只验证数量和唯一性。

```python
class SnapshotResponse(BaseModel):
    available: bool
    stale: bool
    snapshot: NewsSnapshot | None
    refresh: RefreshStatus
    error: PublicError | None = None

class RefreshAcceptedResponse(BaseModel):
    task_id: UUID
    reused: bool
    status: RefreshStatus
```

- [x] **Task 2 Step 4: 实现原子写入**

`AtomicSnapshotStore` 默认路径为 `get_data_dir() / "news" / "latest.json"`。创建/重申 `news/` 目录权限 `0700`；用同目录 `mkstemp`、`fchmod(0600)`、稳定 JSON、flush/fsync、重新 `model_validate_json`、`os.replace`、`chmod(0600)` 和 best-effort 目录 fsync；异常删除临时文件并抛稳定领域异常。

- [x] **Task 2 Step 5: 运行 GREEN、权限与损坏文件测试**

Run: `pytest agent/tests/news/test_models.py agent/tests/news/test_storage.py -q`

Expected: PASS，覆盖 0600、损坏 schema、文件过大、原子替换与旧快照保留。

- [x] **Task 2 Step 6: 提交**

```bash
git add agent/src/news/models.py agent/src/news/storage.py agent/tests/news/test_models.py agent/tests/news/test_storage.py openspec/changes/investment-news-hub/tasks.md
git commit -s -m "feat(news): add validated atomic snapshot storage"
```

### Task 3: 公网 IP 固定与有界 HTTP 采集

**Files:**
- Create: `agent/src/news/network.py`
- Create: `agent/tests/news/test_network.py`

**Interfaces:**
- Consumes: Task 1 `FeedEndpoint`。
- Produces: `PublicFeedClient(resolver: Resolver, transport: httpx.AsyncBaseTransport | None = None)` 与 `PublicFeedClient.fetch(endpoint: FeedEndpoint) -> EndpointFetchResult`，结果只含 endpoint ID、状态、受限 bytes 与稳定错误码。

- [x] **Task 3 Step 1: 写 SSRF、IP 固定和资源上限失败测试**

```python
def test_request_connects_to_validated_ip_with_original_host_and_sni() -> None:
    async def run_case() -> None:
        resolver = FakeResolver({"feeds.example.com": ["93.184.216.34"]})
        transport = CapturingTransport(b"<rss version='2.0'><channel/></rss>")
        client = PublicFeedClient(resolver=resolver, transport=transport)
        result = await client.fetch(make_endpoint("https://feeds.example.com/rss"))
        request = transport.requests[0]
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "feeds.example.com"
        assert request.extensions["sni_hostname"] == "feeds.example.com"
        assert result.ok is True

    asyncio.run(run_case())
```

再覆盖 loopback/private/link-local/reserved、URL credentials、`javascript:`、环境 proxy 不生效、第四跳重定向、2 MiB+1 字节、5/15 timeout 和并发峰值 16。

- [x] **Task 3 Step 2: 运行 RED**

Run: `pytest agent/tests/news/test_network.py -q`

Expected: FAIL，因为安全 transport 尚不存在。

- [x] **Task 3 Step 3: 实现解析、验证与固定连接**

```python
def is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return ip.is_global and not any((ip.is_loopback, ip.is_private, ip.is_link_local, ip.is_multicast, ip.is_reserved))

def pinned_request(method: str, logical_url: httpx.URL, ip: str) -> httpx.Request:
    port = logical_url.port
    host_header = logical_url.host if port is None else f"{logical_url.host}:{port}"
    target = logical_url.copy_with(host=ip)
    return httpx.Request(
        method,
        target,
        headers={"Host": host_header, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
        extensions={"sni_hostname": logical_url.host},
    )
```

每跳重新解析 logical URL；`AsyncClient` 使用 `trust_env=False`、`follow_redirects=False`、`Limits(max_connections=16, max_keepalive_connections=0)` 和 `Timeout(connect=5, read=15, write=5, pool=5)`。

- [x] **Task 3 Step 4: 实现流式读取与 endpoint 隔离**

用 64 KiB chunk 累计，超过 2 MiB 立即关闭响应并返回 `response_too_large`。所有异常映射为固定错误码，只记录 endpoint ID 与异常类型，不记录异常 message。Task 3 只验证单 endpoint fetch；106 endpoint 去重编排由 Task 7 测试证明。

- [x] **Task 3 Step 5: 运行 GREEN**

Run: `pytest agent/tests/news/test_network.py -q`

Expected: PASS，并证明 transport 实际连接已验证 IP、重定向与大小限制均生效。

- [x] **Task 3 Step 6: 提交**

```bash
git add agent/src/news/network.py agent/tests/news/test_network.py
git commit -s -m "feat(news): add bounded public feed transport"
```

### Task 4: 安全 RSS/Atom 解析与字段规范化

**Files:**
- Create: `agent/src/news/feeds.py`
- Create: `agent/tests/news/fixtures/rss.xml`
- Create: `agent/tests/news/fixtures/atom.xml`
- Create: `agent/tests/news/fixtures/malicious_dtd.xml`
- Create: `agent/tests/news/test_feeds.py`

**Interfaces:**
- Consumes: Task 3 的受限 feed bytes 与 SourceAssignment。
- Produces: `parse_feed(content: bytes, assignment: SourceAssignment) -> tuple[RawFeedItem, ...]`；导入模块时不要求 `defusedxml` 已安装。

- [x] **Task 4 Step 1: 写 RSS/Atom 与恶意 XML 失败测试**

```python
def test_parse_atom_keeps_missing_optional_fields_as_none() -> None:
    items = parse_feed(load_fixture("atom.xml"), make_assignment("tech"))
    assert items[0].title
    assert items[0].summary is None
    assert items[0].published_at is None

def test_rejects_dtd_and_entities() -> None:
    with pytest.raises(FeedParseError, match="invalid_xml"):
        parse_feed(load_fixture("malicious_dtd.xml"), make_assignment("security"))
```

- [x] **Task 4 Step 2: 运行 RED**

Run: `pytest agent/tests/news/test_feeds.py -q`

Expected: FAIL，因为 parser 与 fixtures 尚不存在。

- [x] **Task 4 Step 3: 实现延迟导入和结构解析**

`defusedxml.ElementTree` 只能在 `parse_feed()` 内导入；`ImportError` 转为 `FeedParseError("parser_unavailable")`。支持 RSS `item` 与 Atom namespaced `entry`，不执行外部实体或 XSLT。

- [x] **Task 4 Step 4: 实现纯文本化与字段边界**

用 `HTMLParser` 子类收集文本节点、`html.unescape`、Unicode NFKC 和空白折叠；标题截断 300、摘要截断 1000。只接受非空标题与 HTTP(S) 原文 URL，缺失摘要/时间保持 `None`。

- [x] **Task 4 Step 5: 运行 GREEN 与 Tier 0 导入证明**

Run:

```bash
pytest agent/tests/news/test_feeds.py -q
python -c "import sys; sys.modules['defusedxml'] = None; import src.news.feeds"
```

Expected: pytest PASS；模块导入命令退出 0，证明装配期不加载 parser。

- [x] **Task 4 Step 6: 提交**

```bash
git add agent/src/news/feeds.py agent/tests/news/fixtures agent/tests/news/test_feeds.py openspec/changes/investment-news-hub/tasks.md
git commit -s -m "feat(news): parse untrusted rss and atom feeds"
```

### Task 5: 过滤、去重与赛道级新旧合并

**Files:**
- Create: `agent/src/news/pipeline.py`
- Create: `agent/tests/news/test_pipeline.py`

**Interfaces:**
- Consumes: Task 4 `RawFeedItem`、Task 2 旧 `NewsSnapshot`、Task 1 catalog。
- Produces: `build_track_candidates(catalog: NewsCatalog, current: Sequence[AssignmentItems], previous: NewsSnapshot | None, now: datetime) -> TrackBuildResult`，其中 `TrackBuildResult.tracks` 恰好 12 个，`updated_track_ids: frozenset[str]` 决定是否进入 LLM。

- [x] **Task 5 Step 1: 写确定性处理和回退失败测试**

```python
def test_track_without_current_items_reuses_old_track_as_stale() -> None:
    old = make_track("semi", state="fresh", generated_at=OLD_TIME, title="old")
    result = build_track_candidates(catalog(), [], make_snapshot_with_tracks([old]), NOW)
    semi = next(item for item in result.tracks if item.id == "semi")
    assert semi.state == "stale"
    assert semi.generated_at == OLD_TIME
    assert semi.items[0].title_original == "old"

def test_first_run_missing_track_is_unavailable() -> None:
    result = build_track_candidates(catalog(), current_items_for("ai"), None, NOW)
    science = next(item for item in result.tracks if item.id == "science")
    assert science.state == "unavailable"
    assert science.items == []
```

再覆盖 7 天窗口、无时间条目、每 endpoint 6 条、赛道 100 条、过滤词、URL 去 fragment/default port、赛道内去重、partial 标记和 12 赛道均无更新。

- [x] **Task 5 Step 2: 运行 RED**

Run: `pytest agent/tests/news/test_pipeline.py -q`

Expected: FAIL，因为 pipeline 尚不存在。

- [x] **Task 5 Step 3: 实现确定性 pipeline**

处理顺序固定为：字段规范化、assignment 过滤、7 天窗口、endpoint 内去重、赛道内去重、发布时间降序、完整度/source ID/stable ID 决胜、数量裁剪。无时间条目排在有时间条目之后。

- [x] **Task 5 Step 4: 实现三态合并与提交判定**

本次赛道至少一个有效条目时设 `fresh`，与旧条目按 ID 合并至 100 条；本次无有效条目且有旧数据时原样复用并设 `stale`；没有旧数据时设 `unavailable`。pipeline 始终返回 12 个赛道及 `updated_track_ids`；当该集合为空时，由 Task 7 的协调器以 `no_track_updated` 结束刷新，在调用 LLM 或存储前拒绝提交。

- [x] **Task 5 Step 5: 运行 GREEN**

Run: `pytest agent/tests/news/test_pipeline.py -q`

Expected: PASS，且输出顺序在重复执行时字节级稳定。

- [x] **Task 5 Step 6: 提交**

```bash
git add agent/src/news/pipeline.py agent/tests/news/test_pipeline.py
git commit -s -m "feat(news): add deterministic track merge pipeline"
```

### Task 6: 单实例、每赛道一次的 LLM Enrichment

**Files:**
- Create: `agent/src/news/llm.py`
- Create: `agent/tests/news/test_llm_enrichment.py`

**Interfaces:**
- Consumes: Task 5 的更新赛道和 `build_llm` factory。
- Produces: `enrich_tracks(tracks: Sequence[TrackSnapshot], updated_track_ids: frozenset[str], llm_factory: Callable[[], Any]) -> tuple[TrackSnapshot, ...]`；只修改更新赛道的 `title_zh` 与 `ai`。

- [x] **Task 6 Step 1: 写调用次数、输入上限和降级失败测试**

```python
def test_one_llm_instance_and_one_call_per_updated_track() -> None:
    async def run_case() -> None:
        factory = FakeLLMFactory()
        tracks = [make_candidate("ai", 20), make_candidate("tech", 4)]
        result = await enrich_tracks(tracks, frozenset({"ai", "tech"}), factory)
        assert factory.build_count == 1
        assert factory.llm.call_count == 2
        assert max(len(call.items) for call in factory.llm.calls) == 16
        assert {item.id for item in result} == {"ai", "tech"}

    asyncio.run(run_case())
```

再用 barrier fake 断言并发峰值不超过 3，并覆盖原中文标题、未知 ID、重复 ID、畸形 JSON、少于 3/多于 5 要点、超长标题、提示注入和单赛道异常。

- [x] **Task 6 Step 2: 运行 RED**

Run: `pytest agent/tests/news/test_llm_enrichment.py -q`

Expected: FAIL，因为 enrich 管线尚不存在。

- [x] **Task 6 Step 3: 实现严格输出模型和提示词边界**

```python
class TrackLLMOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    titles: list[LocalizedTitle]
    highlights: list[str] = Field(min_length=3, max_length=5)

async def invoke_model(llm: Any, messages: list[dict[str, Any]]) -> Any:
    if callable(getattr(llm, "ainvoke", None)):
        return await llm.ainvoke(messages)
    return await asyncio.to_thread(llm.invoke, messages)
```

feed 文本放入明确 data 区并声明其指令不可信；消息统一使用包含 `role` 与 `content` 的 dict，兼容 ChatOpenAI 与现有 `OpenAICodexLLM` 的 `.get()` 读取方式。响应只从 message content 提取 JSON，再用 Pydantic 校验；增加直接传入 Codex-compatible fake 的回归测试。每条 highlight 上限固定为 300 字符，并覆盖刚好 300 与 301 字符拒绝。

- [x] **Task 6 Step 4: 实现一次 build 与并发 3**

确认至少一个更新赛道后调用一次 `llm_factory()`；每个更新赛道截取最多 16 条，使用一个 `asyncio.Semaphore(3)`。`build_llm()` 或任一赛道失败时只写固定 AI error code，原始资讯仍可提交。

- [x] **Task 6 Step 5: 运行 GREEN**

Run: `pytest agent/tests/news/test_llm_enrichment.py -q`

Expected: PASS，断言 build count 1、调用数等于更新赛道数、并发峰值 3。

- [x] **Task 6 Step 6: 提交**

```bash
git add agent/src/news/llm.py agent/tests/news/test_llm_enrichment.py openspec/changes/investment-news-hub/tasks.md
git commit -s -m "feat(news): enrich tracks with project llm"
```

### Task 7: 进程内单刷新协调器

**Files:**
- Create: `agent/src/news/coordinator.py`
- Create: `agent/tests/news/test_coordinator.py`

**Interfaces:**
- Consumes: Tasks 1-6 的 catalog、fetcher、parser、pipeline、enricher、store。
- Produces: `start() -> RefreshAcceptedResponse`、`status() -> RefreshStatus`、`snapshot_response() -> SnapshotResponse`、`close() -> None`，以及不启动网络工作的惰性进程单例 `get_news_coordinator() -> NewsRefreshCoordinator`。

- [x] **Task 7 Step 1: 写单任务和全流程失败测试**

```python
def test_duplicate_start_reuses_running_task() -> None:
    async def run_case() -> None:
        gate = asyncio.Event()
        coordinator = make_coordinator(fetch_gate=gate)
        first = await coordinator.start()
        second = await coordinator.start()
        assert first.task_id == second.task_id
        assert second.reused is True
        gate.set()
        await coordinator.wait_current()

    asyncio.run(run_case())
```

再覆盖全部 12 赛道、106 endpoint 只抓一次、108 assignment 计数、阶段转换、`no_track_updated` 不调用 LLM/不写盘、LLM 全失败仍提交 feed、commit 失败保留旧快照和 close 取消。

- [x] **Task 7 Step 2: 运行 RED**

Run: `pytest agent/tests/news/test_coordinator.py -q`

Expected: FAIL，因为协调器尚不存在。

- [x] **Task 7 Step 3: 实现状态锁与后台任务**

`_lock` 只保护任务创建和不可变状态替换；`start()` 在锁内创建 UUID 与 `asyncio.create_task` 后立即返回。状态阶段严格为 `idle/fetching/normalizing/enriching/committing/succeeded/failed/cancelled`。

- [x] **Task 7 Step 4: 编排 12 赛道并脱敏错误**

抓取 endpoint 后向 assignment 分发，更新 endpoint/assignment/track 计数；网络错误按 endpoint ID 去重，关联的 assignment/track ID 有上限。所有磁盘写入只发生在通过完整 `NewsSnapshot` 校验之后。

- [x] **Task 7 Step 5: 实现 snapshot 包络和 shutdown**

最近内存任务失败且磁盘快照存在时 `SnapshotResponse.stale=True`；重启初始化为 idle。`close()` 取消并 await 任务，关闭 client，不持久化任务状态。

模块级 `_news_coordinator: NewsRefreshCoordinator | None = None`；`get_news_coordinator()` 首次调用时构造依赖，后续返回同一实例。HTTP client 仍在刷新开始时惰性创建，getter 本身不得访问网络或 LLM。

- [x] **Task 7 Step 6: 运行 GREEN**

Run: `pytest agent/tests/news/test_coordinator.py -q`

Expected: PASS，无遗留 asyncio task warning。

- [x] **Task 7 Step 7: 提交**

```bash
git add agent/src/news/coordinator.py agent/tests/news/test_coordinator.py openspec/changes/investment-news-hub/tasks.md
git commit -s -m "feat(news): coordinate single background refresh"
```

### Task 8: FastAPI 路由、鉴权与生命周期

**Files:**
- Create: `agent/src/api/news_routes.py`
- Create: `agent/tests/test_news_routes.py`
- Modify: `agent/api_server.py`
- Modify: `agent/src/api/scheduled_routes.py` (FastAPI 0.115+ 的既有 204 无响应体兼容性前提，确保 API server 可加载并执行本任务的集成测试)
- Modify: `agent/src/api/security.py` and `agent/tests/test_sse_ticket_and_headers.py` (对非法 `/news-api` query 的 Uvicorn access-log 全量脱敏，防止拒绝请求中的任意 canary 泄露)
- Modify: `agent/tests/test_security_auth_api.py`
- Modify: `agent/tests/test_spa_fallback.py`

**Interfaces:**
- Consumes: Task 7 进程级 coordinator 与 Task 2 API 模型。
- Produces: `create_news_router(coordinator: NewsRefreshCoordinator) -> APIRouter` 与 `register_news_routes(app, require_auth, coordinator)`，暴露 `GET /news-api/snapshot`、`POST /news-api/refresh`、`GET /news-api/refresh/status`。

- [x] **Task 8 Step 1: 写 API 契约失败测试**

```python
def test_refresh_returns_202_and_reuses_task(client: TestClient) -> None:
    first = client.post("/news-api/refresh")
    second = client.post("/news-api/refresh")
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["reused"] is True
    assert second.json()["task_id"] == first.json()["task_id"]
```

fixture 使用带 `asyncio.Event` gate 的 `FakeNewsCoordinator`，确保第二次 POST 发生时第一任务仍为 running，且绝不构造真实 collector。覆盖无快照 200 包络、损坏快照、远程未授权 401/403、跨站 POST、防止 `/news-api` HTML fallback、响应/日志 canary 脱敏和 shutdown close。

- [x] **Task 8 Step 2: 运行 RED**

Run: `pytest agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -q`

Expected: FAIL，三个路由尚未注册。

- [x] **Task 8 Step 3: 实现薄路由与依赖注入**

```python
def create_news_router(coordinator: NewsRefreshCoordinator) -> APIRouter:
    router = APIRouter(prefix="/news-api", tags=["news"])
    return router

def register_news_routes(
    app: FastAPI,
    require_auth: Callable[..., Awaitable[None]],
    coordinator: NewsRefreshCoordinator,
) -> None:
    app.include_router(create_news_router(coordinator), dependencies=[Depends(require_auth)])
```

handler 仅调用 coordinator，不接受 refresh body、track ID、feed URL 或 LLM 参数；POST 设置 `status_code=202`。

- [x] **Task 8 Step 4: 注册路由与 shutdown**

在 `agent/api_server.py` 的领域路由装配区、根静态 mount 前执行 `news_coordinator = get_news_coordinator()`，再调用 `register_news_routes(app, require_auth=require_auth, coordinator=news_coordinator)`；新增 shutdown hook 调用 `await news_coordinator.close()`。注册和 shutdown 必须使用同一实例，路由模块不得反向导入 `api_server`。

对于所有不安全请求，现有 CSRF 检查只接受同源或精确属于 `CORS_ORIGINS` 的带 `Origin` 调用；默认集合补充 `http://localhost:5899` 与 `http://127.0.0.1:5899`。该检查必须以应用级 middleware 在路由执行前运行，不能只依赖 `require_auth`，从而覆盖 `/watchlist/stocks` 等未挂鉴权依赖的写路由。其他 loopback Origin 必须在协调器开始前被拒绝；无 `Origin` 的非浏览器客户端保持既有鉴权语义。

- [x] **Task 8 Step 5: 运行 GREEN 与语法检查**

Run:

```bash
pytest agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -q
python -m py_compile agent/api_server.py agent/src/api/news_routes.py
```

Expected: 全部 PASS；API 错误保持 JSON，`/news` 仍由普通 SPA catch-all 处理。

- [x] **Task 8 Step 6: 提交**

```bash
git add agent/src/api/news_routes.py agent/api_server.py agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py openspec/changes/investment-news-hub/tasks.md
git commit -s -m "feat(news): expose authenticated refresh api"
```

### Task 9: 前端 API 类型、代理与 i18n 契约

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/__tests__/api.test.ts`
- Modify: `frontend/vite.config.ts`
- Modify: `frontend/src/__tests__/viteProxy.test.ts`
- Modify: `frontend/src/i18n/locales/zh-CN.json`
- Modify: `frontend/src/i18n/locales/en.json`
- Modify: `frontend/src/i18n/locales/ja.json`
- Modify: `frontend/src/i18n/locales/ko.json`
- Modify: `frontend/src/i18n/locales/ar.json`
- Create: `frontend/src/i18n/__tests__/newsLocales.test.ts`

**Interfaces:**
- Consumes: Task 8 JSON 模型。
- Produces: `api.getNewsSnapshot(signal)`、`api.startNewsRefresh(signal)`、`api.getNewsRefreshStatus(signal)`、`parseNewsSnapshotResponse(value)` 和五语言 `news.*`/`layout.news` keys。

- [x] **Task 9 Step 1: 写 API 与 locale 失败测试**

```typescript
it("starts news refresh through the authenticated json client", async () => {
  mockFetchJson({ task_id: "00000000-0000-4000-8000-000000000001", reused: false, status: runningStatus });
  await api.startNewsRefresh();
  expect(fetch).toHaveBeenCalledWith("/news-api/refresh", expect.objectContaining({ method: "POST" }));
});
```

locale 测试遍历 `zh-CN/en/ja/ko/ar`，断言 `news.title`、12 个 `news.tracks.*`、fresh/stale/unavailable/partial/AI/error 文案均为非空字符串。

- [x] **Task 9 Step 2: 运行 RED**

Run: `cd frontend && npx vitest run src/lib/__tests__/api.test.ts src/i18n/__tests__/newsLocales.test.ts src/__tests__/viteProxy.test.ts`

Expected: FAIL，因为方法、proxy 与 locale keys 尚不存在。

- [x] **Task 9 Step 3: 增加精确 TypeScript 模型与 API 方法**

```typescript
export type NewsTrackState = "fresh" | "stale" | "unavailable";
export type NewsRefreshPhase = "idle" | "fetching" | "normalizing" | "enriching" | "committing" | "succeeded" | "failed" | "cancelled";

getNewsSnapshot: (signal?: AbortSignal) => request<NewsSnapshotResponse>("/news-api/snapshot", { signal }),
startNewsRefresh: (signal?: AbortSignal) => request<NewsRefreshAccepted>("/news-api/refresh", { method: "POST", signal }),
getNewsRefreshStatus: (signal?: AbortSignal) => request<NewsRefreshStatus>("/news-api/refresh/status", { signal }),
```

资讯方法先以 `unknown` 接收 JSON，再经运行时 parser 检查 object、固定 12 track IDs、状态 enum、数组字段、字符串长度和 HTTP(S) article URL；无效响应抛现有 `ApiError`，不能仅用 TypeScript cast。API 测试加入缺失 tracks、未知 phase、非 HTTP(S) URL 和超长 title 的拒绝用例。

- [x] **Task 9 Step 4: 增加 `/news-api` proxy 和五语言文案**

把 `"/news-api"` 加入 `PROXY_PATHS`；五个 locale 使用相同 key 结构，赛道 ID 固定为 `ai/semi/robot/auto/energy/bio/space/security/tech/consumer/macro/science`，并同时增加 `layout.news`。locale 测试必须断言 `layout.news` 和全部 `news.*` key。

- [x] **Task 9 Step 5: 运行 GREEN**

Run: `cd frontend && npx vitest run src/lib/__tests__/api.test.ts src/i18n/__tests__/newsLocales.test.ts src/__tests__/viteProxy.test.ts`

Expected: PASS，且 API 请求继续携带现有 Bearer auth header。

- [x] **Task 9 Step 6: 提交**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/__tests__/api.test.ts frontend/vite.config.ts frontend/src/__tests__/viteProxy.test.ts frontend/src/i18n openspec/changes/investment-news-hub/tasks.md
git commit -s -m "feat(news): add frontend api and translations"
```

Acceptance note (2026-07-20): the user chose frontend compatibility with the backend URL parser. The client intentionally preserves the backend's permissive handling of malformed percent escapes and authority port text, while still allowing only HTTP(S) URLs for article links.

### Task 10: 页面状态 Hook 与轮询生命周期

**Files:**
- Create: `frontend/src/hooks/useNews.ts`
- Create: `frontend/src/hooks/__tests__/useNews.test.tsx`

**Interfaces:**
- Consumes: Task 9 API 方法与类型。
- Produces: `useNews(): NewsPageState & NewsPageActions`，供页面渲染，不修改全局 Agent store。

- [x] **Task 10 Step 1: 写首次单请求和轮询失败测试**

```typescript
it("loads snapshot once and polls status only while running", async () => {
  api.getNewsSnapshot.mockResolvedValue(snapshotWithRunningRefresh);
  api.getNewsRefreshStatus.mockResolvedValue(succeededStatus);
  const { result } = renderHook(() => useNews());
  await waitFor(() => expect(api.getNewsSnapshot).toHaveBeenCalledTimes(1));
  expect(api.getNewsRefreshStatus).not.toHaveBeenCalled();
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(api.getNewsRefreshStatus).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(api.getNewsSnapshot).toHaveBeenCalledTimes(2));
});
```

再覆盖 POST 新建/复用、failed 保留旧列表、unmount 清 timer/AbortController、迟到响应不写状态和非重叠 `setTimeout`。

- [x] **Task 10 Step 2: 运行 RED**

Run: `cd frontend && npx vitest run src/hooks/__tests__/useNews.test.tsx`

Expected: FAIL，因为 hook 尚不存在。

- [x] **Task 10 Step 3: 实现 reducer 与初始化**

首次只调用 `getNewsSnapshot()`，选中赛道默认为第一个可用赛道，否则 catalog 顺序第一个；使用 snapshot 包络内 refresh 状态决定是否启动轮询。

- [x] **Task 10 Step 4: 实现串行轮询与清理**

每次 status 请求结束后才安排下一次 1000 ms `setTimeout`；终态立即停止。succeeded 后重取 snapshot，failed/cancelled 保留现有 snapshot。为 snapshot、refresh POST 和 status 分别创建 AbortController，cleanup abort 所有在途请求并清 timer；测试断言 `startNewsRefresh(signal)` 收到的 signal 在 unmount 后为 aborted。

- [x] **Task 10 Step 5: 运行 GREEN**

Run: `cd frontend && npx vitest run src/hooks/__tests__/useNews.test.tsx`

Expected: PASS，无 fake timer 或 act warning。

- [x] **Task 10 Step 6: 提交**

```bash
git add frontend/src/hooks/useNews.ts frontend/src/hooks/__tests__/useNews.test.tsx
git commit -s -m "feat(news): manage snapshot refresh lifecycle"
```

### Task 11: `/news` 页面、响应式赛道控件与安全外链

**Files:**
- Create: `frontend/src/pages/News.tsx`
- Create: `frontend/src/pages/__tests__/News.test.tsx`
- Create: `frontend/src/pages/__tests__/News.layout.test.tsx`
- Create: `frontend/e2e/news-responsive.spec.ts`
- Create: `frontend/playwright.config.ts`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Create: `docs/superpowers/reviews/2026-07-20-investment-news-hub-viewport-evidence.md`
- Create: `docs/superpowers/reviews/assets/investment-news-mobile.png`
- Create: `docs/superpowers/reviews/assets/investment-news-desktop.png`
- Modify: `frontend/src/router.tsx`
- Modify: `frontend/src/components/layout/Layout.tsx`
- Modify: `frontend/src/components/layout/__tests__/Layout.test.tsx`

**Interfaces:**
- Consumes: Task 10 `useNews()` 与 Task 9 locale keys。
- Produces: 懒加载 `/news` 页面、侧栏“投资资讯”入口和响应式可访问 UI。

- [x] **Task 11 Step 1: 写路由、赛道和安全链接失败测试**

```typescript
it("switches tracks without changing refresh scope", async () => {
  render(<News />);
  await user.click(screen.getByRole("tab", { name: "半导体" }));
  expect(screen.getByRole("heading", { name: "芯片新闻" })).toBeInTheDocument();
  expect(api.startNewsRefresh).not.toHaveBeenCalled();
});

it("does not render non-http article urls as links", () => {
  mockNewsItem({ url: "javascript:alert(1)" });
  render(<News />);
  expect(screen.queryByRole("link", { name: /原文/ })).not.toBeInTheDocument();
});
```

覆盖 12 desktop tabs、mobile select、3-5 要点、中文标题优先、缺失摘要/时间、stale/unavailable/partial/AI unavailable、空快照、刷新进度和整体失败。`News.layout.test.tsx` 断言根内容 `min-w-0`、tablist `overflow-x-auto`、长标题 `break-words`、desktop/mobile 控件互斥 class，作为静态布局契约。用 `npm install --save-dev @playwright/test` 写入 package/lock，并创建只针对 `/news` 的 Chromium 配置；E2E 通过 `page.route("**/news-api/**")` 返回固定 snapshot/status fixture，禁止访问真实后端、feed 或 LLM。

- [x] **Task 11 Step 2: 运行 RED**

Run: `cd frontend && npx vitest run src/pages/__tests__/News.test.tsx src/pages/__tests__/News.layout.test.tsx src/components/layout/__tests__/Layout.test.tsx`

Expected: FAIL，因为页面、路由与导航尚不存在。

- [x] **Task 11 Step 3: 实现稳定响应式布局**

桌面使用固定高度、横向滚动的 `role="tablist"`；窄屏使用 `<select>`。刷新按钮使用 Lucide `RefreshCw`、固定尺寸和 tooltip/accessible label；列表为 unframed 工作台布局，不嵌套卡片。

- [x] **Task 11 Step 4: 实现安全内容和外链**

React 仅渲染文本。显示标题时使用 `title_zh || title_original`；URL 通过 `new URL()` 且 protocol 为 `http:`/`https:` 才渲染 `<a target="_blank" rel="noopener noreferrer">`。

- [x] **Task 11 Step 5: 接入 router 与 Layout**

在 `router.tsx` 增加 lazy `News` 与 `{ path: "/news", element: wrap(News) }`；在 Layout 工作区导航增加 Lucide `Newspaper`、`t("layout.news")` 与 active 判定。不得修改 Agent store。

- [x] **Task 11 Step 6: 运行 GREEN 与前端构建**

Run:

```bash
cd frontend
npx vitest run src/pages/__tests__/News.test.tsx src/pages/__tests__/News.layout.test.tsx src/components/layout/__tests__/Layout.test.tsx
npm run build
```

Expected: tests PASS；TypeScript 与 Vite build 退出 0，无文本溢出类型错误。

- [x] **Task 11 Step 7: 在真实浏览器验证两个 viewport**

`playwright.config.ts` 明确设置 `testDir: "./e2e"`、`use.baseURL: "http://127.0.0.1:5899"`，以及 `webServer: { command: "npm run dev -- --host 127.0.0.1", url: "http://127.0.0.1:5899", reuseExistingServer: !process.env.CI }`。`news-responsive.spec.ts` 对 `390x844` 和 `1440x900` 参数化运行：断言 `document.documentElement.scrollWidth <= window.innerWidth`；用 `locator.boundingBox()` 检查赛道控件、刷新按钮、要点和第一条资讯两两不相交。所有 evidence 路径使用 `path.resolve(process.cwd(), "../docs/superpowers/reviews")` 从 `frontend` cwd 定位仓库根；测试启动时先运行 `mkdirSync(path.join(evidenceRoot, "assets"), { recursive: true })`，再把截图写入 `assets/investment-news-mobile.png` 与 `assets/investment-news-desktop.png`，afterAll 写入 `2026-07-20-investment-news-hub-viewport-evidence.md`。

Run:

```bash
cd frontend
npx playwright install chromium
npx playwright test e2e/news-responsive.spec.ts
```

Expected: 两个 viewport PASS，生成两张非空截图与一份 evidence Markdown；任一失败先修复再提交。

- [x] **Task 11 Step 8: 提交**

```bash
git add frontend/src/pages/News.tsx frontend/src/pages/__tests__/News.test.tsx frontend/src/pages/__tests__/News.layout.test.tsx frontend/e2e/news-responsive.spec.ts frontend/playwright.config.ts frontend/package.json frontend/package-lock.json frontend/src/router.tsx frontend/src/components/layout/Layout.tsx frontend/src/components/layout/__tests__/Layout.test.tsx openspec/changes/investment-news-hub/tasks.md
git add -f docs/superpowers/reviews/2026-07-20-investment-news-hub-viewport-evidence.md docs/superpowers/reviews/assets/investment-news-mobile.png docs/superpowers/reviews/assets/investment-news-desktop.png
git commit -s -m "feat(news): add investment news workspace"
```

### Task 12: 发布物、降级路径与完整验证

**Files:**
- Create: `agent/tests/news/test_distribution.py`
- Create: `agent/tests/news/test_end_to_end.py`
- Modify: `scripts/desktop/smoke_tier0.py`
- Modify: `openspec/changes/investment-news-hub/tasks.md`

**Interfaces:**
- Consumes: Tasks 1-11 的完整模块。
- Produces: 可审计的 wheel/sdist/desktop notice、无真实网络的端到端降级证据和已勾选 OpenSpec tasks。

- [x] **Task 12 Step 1: 写发布物与端到端失败测试**

`test_distribution.py` 在 `NEWS_DIST_DIR` 缺失时用 `pytest.skip("NEWS_DIST_DIR is required for archive verification")`，从而不破坏普通目标/全量测试；变量存在时用 `zipfile` 检查 wheel、`tarfile` 检查 sdist，分别断言 `upstream_manifest.json`、`.sha256`、`THIRD_PARTY_NOTICES.md` 存在，并将 archive 内 notice 与仓库 notice 的完整 UTF-8 内容和 SHA-256 比较，证明完整 MIT 条款而非仅版权行进入发布物。另写 `test_archives_from_env_rejects_empty_directory()` 直接调用 helper，证明显式空目录会失败。`test_end_to_end.py` 使用临时 data dir、MockTransport 与 FakeLLM 完成：首次部分赛道 unavailable、第二次失败赛道 stale、LLM 不可用仍提交、12 赛道无更新不改文件、重建 coordinator 后 status 为 idle。

- [x] **Task 12 Step 2: 运行 RED**

Run: `NEWS_DIST_DIR="$(mktemp -d)" pytest agent/tests/news/test_distribution.py agent/tests/news/test_end_to_end.py -q`

Expected: FAIL，直到发布断言和完整依赖注入路径补齐。

- [x] **Task 12 Step 3: 补齐分发与 Tier 0 smoke 契约**

在 `smoke_tier0.py` 导入 `api_server.app` 后断言 `/news-api/snapshot` 路由存在，但不调用 parser；保持 Tier 0 requirements 不新增 defusedxml。`test_distribution.py` 的 `archives_from_env()` 必须验证目录存在、恰好找到至少一个 `.whl` 和一个 `.tar.gz`，否则用明确 assertion 失败，不自动猜测 `dist/`。

- [x] **Task 12 Step 4: 运行后端目标测试**

Run:

```bash
pytest agent/tests/news agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -W error::RuntimeWarning -q
python -m compileall -q agent/src/news agent/src/api
python -m py_compile agent/api_server.py
```

Expected: 全部 PASS；任何未 await coroutine 产生的 `RuntimeWarning` 会被提升为失败。

- [x] **Task 12 Step 5: 运行前端测试与生产构建**

Run:

```bash
cd frontend
npx vitest run src/lib/__tests__/api.test.ts src/hooks/__tests__/useNews.test.tsx src/pages/__tests__/News.test.tsx src/pages/__tests__/News.layout.test.tsx src/components/layout/__tests__/Layout.test.tsx src/i18n/__tests__/newsLocales.test.ts src/__tests__/viteProxy.test.ts
npm run build
```

Expected: Vitest 全部 PASS；`tsc -b && vite build` 退出 0。

- [x] **Task 12 Step 6: 验证 sdist/wheel 与桌面 staging**

Run:

```bash
dist_dir="$(mktemp -d)"
python -m build --outdir "$dist_dir"
NEWS_DIST_DIR="$dist_dir" python -m pytest agent/tests/news/test_distribution.py -q
bash scripts/desktop/assemble.sh
test -f .desktop-build/agent/src/news/THIRD_PARTY_NOTICES.md
.desktop-build/python-runtime/bin/python3 scripts/desktop/smoke_tier0.py
```

Expected: sdist/wheel 与 `.desktop-build/agent` 都包含 notice；Tier 0 smoke PASS。若桌面 runtime 尚未组装，只记录该环境前置缺失，不伪造通过，并在 verify 阶段补做。

- [x] **Task 12 Step 7: 安全回归与全量适度测试**

Run:

```bash
pytest --ignore=agent/tests/e2e_backtest --ignore=agent/tests/test_e2e_harness_v2.py --tb=short -q
```

Expected: 全套非 live/e2e 测试 PASS；不得运行 broker-write 或 live trading 流程。

- [x] **Task 12 Step 8: 定向勾选非独立审查项并提交**

逐项核对 1.1-7.4 的命令证据后，把已验证项从 `- [ ]` 改为 `- [x]`；7.5 必须保持未勾选并交给 Task 13 的独立 reviewer/verifier。没有证据的项保持未勾选并继续修复，不能批量假定完成。

```bash
git add agent/tests/news scripts/desktop/smoke_tier0.py openspec/changes/investment-news-hub/tasks.md
git commit -s -m "test(news): verify packaging and degradation paths"
```

### Task 13: 独立 Spec 与质量审查

**Files:**
- Create: `docs/superpowers/reviews/2026-07-20-investment-news-hub-build-review.md`
- Modify: `openspec/changes/investment-news-hub/tasks.md`

**Interfaces:**
- Consumes: Task 12 完成后的完整 change diff、测试输出、Task 11 viewport evidence 与截图、Design Doc 和 canonical delta spec。
- Produces: 独立 reviewer/verifier 报告；只有报告结论为 PASS 且所有阻塞发现关闭时才勾选 OpenSpec 7.5。

- [x] **Task 13 Step 1: 派发独立 reviewer/verifier**

该任务必须由未参与 Tasks 1-12 实现的独立 agent 执行。审查 prompt 明确要求对照 delta spec 逐项检查：免费范围、108 mapping/106 endpoint/12 track、固定 SHA 与 golden hash、MIT 完整条款、单 sidecar、`require_auth`、SSRF/IP pinning、DTD/XXE、HTTP(S) article URL、密钥边界、赛道级 freshness、单刷新单 LLM 实例、每更新赛道一次调用、16 候选、并发 3、无全文/历史，以及前端两个 viewport。

- [x] **Task 13 Step 2: 记录带证据的审查报告**

报告 frontmatter 记录 change、reviewed commit range、reviewer lane 和结论；正文按 P0/P1/P2/P3 列发现，并附测试命令、viewport screenshot 路径与 `scrollWidth/clientWidth`。如果存在 P0/P1/P2，结论必须为 FAIL，返回相应实现任务修复后重新派发独立复审。

- [x] **Task 13 Step 3: 仅在 PASS 后勾选 7.5**

Run: `rg -n '^result: PASS$' docs/superpowers/reviews/2026-07-20-investment-news-hub-build-review.md`

Expected: 恰好一行 PASS；随后才把 `tasks.md` 的 7.5 改为 `[x]`，并运行 `test -z "$(rg '\- \[ \]' openspec/changes/investment-news-hub/tasks.md || true)"` 确认没有未勾选项。

- [x] **Task 13 Step 4: 提交独立审查证据**

```bash
git add -f docs/superpowers/reviews/2026-07-20-investment-news-hub-build-review.md
git add openspec/changes/investment-news-hub/tasks.md
git commit -s -m "docs(news): record independent build review"
```

---

## 计划完成检查

- Spec coverage：Tasks 1-12 实现并验证 OpenSpec 1.1-7.4；Task 13 以独立 reviewer lane 验证并勾选 7.5，覆盖免费范围、108/106/12、单服务、鉴权、XML/URL 安全、密钥边界、每赛道要点、无全文/无历史和 MIT notice。
- Placeholder scan：计划不包含待补实现标记；所有 RED/GREEN 命令、预期结果、接口与 commit 均已明确。
- Type consistency：`FeedEndpoint -> EndpointFetchResult -> RawFeedItem -> TrackCandidate -> TrackSnapshot -> NewsSnapshot` 数据链与 API/TypeScript 模型保持一致。
- 执行边界：真实 feed/LLM 仅作为用户显式授权的人工冒烟，不是自动测试通过条件；live trading 与 broker 写路径不在本变更范围。
