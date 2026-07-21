## 1. 后端存储层与数据模型

- [x] 1.1 新建 `agent/src/api/watchlist_routes.py`，定义 Pydantic 请求/响应模型和 `APIRouter(prefix="/watchlist")`
- [x] 1.2 实现 SQLite 初始化与连接管理：创建 `~/.vibe-trading/watchlist.db` 和 `watchlist` 表，确保幂等和跨平台路径兼容
- [x] 1.3 实现 `GET /watchlist/stocks`、`POST /watchlist/stocks`、`DELETE /watchlist/stocks/{code}` CRUD 端点，覆盖校验、重复添加和 404 场景
- [x] 1.4 添加后端存储层单元测试：首次建库、幂等初始化、CRUD、重复添加、无效代码、删除不存在记录

## 2. 行情 Provider 与查询接口

- [x] 2.1 定义 `QuoteProvider` Protocol 和 `TencentQuoteProvider` 实现，复用 `tencent_quote` 并统一行情响应结构
- [x] 2.2 实现 `GET /watchlist/quotes?codes=...` 端点：批量查询、空参数校验、部分失败降级、未知市场错误
- [x] 2.3 实现行情内存缓存与 stale 降级：数据源超时/失败时返回最后缓存值，无缓存时返回错误条目且不抛 HTTP 500
- [x] 2.4 在 `agent/api_server.py` 注册 watchlist router，并更新前端 Vite proxy 配置加入 `/watchlist`
- [x] 2.5 添加 QuoteProvider 与行情 API 单元测试：正常批量查询、部分失败、超时 stale、未知市场、空 codes
- [x] 2.6 修复 verify 回归：在 `agent/api_server.py` 的 startup 事件中调用 watchlist `init_db` 幂等建表（满足 plan「DB 初始化须在 startup 建表，不在首次请求中建」约束），确保 `GET/POST/DELETE /watchlist/stocks` 在空库下不再 500；补充 startup 建表的集成测试（断言启动后 watchlist 表存在 / 接口可用）

## 3. 前端 API、类型与状态管理

- [x] 3.1 在 `frontend/src/lib/api.ts` 新增 watchlist 类型定义和 CRUD/quotes API 函数
- [x] 3.2 新增 watchlist Zustand store，管理股票列表、行情数据、选中状态、loading/error，并提供 refresh/add/remove/toggleSelection actions
- [x] 3.3 为 API 函数与 store 添加 Vitest 测试，覆盖成功、错误和状态更新场景

## 4. 自选股盯盘页面

- [x] 4.1 新建 `frontend/src/pages/Watchlist.tsx`，实现空状态、添加股票表单、6 位 A 股代码校验和重复添加提示
- [x] 4.2 实现行情表格：代码、名称、当前价、涨跌额、涨跌幅，使用 A 股红涨绿跌颜色和格式化显示
- [x] 4.3 实现 3s 自动轮询和 Page Visibility 暂停/恢复逻辑，组件卸载时正确清理 interval
- [x] 4.4 实现删除二次确认、行勾选、多选和「发给 Agent 分析」按钮状态
- [x] 4.5 为 Watchlist 页面添加组件测试：空状态、添加校验、行情渲染、涨跌颜色、轮询暂停/恢复、删除确认、多选

## 5. 路由、导航与 Agent 联动

- [x] 5.1 在 `frontend/src/router.tsx` 注册懒加载 `/watchlist` 路由，在 `Layout.tsx` 侧边栏 Agent 后新增「自选股」导航项并补充 i18n 文案
- [x] 5.2 实现选股后通过 `prefill` query param 跳转到 `/agent`，单选和多选生成对应分析请求文本
- [x] 5.3 修改 Agent 页面读取 `prefill` 参数并预填输入框，不自动提交且不影响无参数的现有行为
- [x] 5.4 添加路由导航和 Agent prefill 集成测试

## 6. 集成验证与文档

- [x] 6.1 运行后端 watchlist 窄测试、相关现有测试和 Python 语法检查，修复所有失败
- [x] 6.2 运行前端 Vitest 全量测试和 `npm run build`，修复所有失败和 TypeScript 错误
- [x] 6.3 手工验证核心流程：添加股票→3s 行情刷新→SQLite 持久化→删除→选中发给 Agent→输入框预填
- [x] 6.4 更新 `docs/desktop/README.md` 或相关用户文档，说明自选股功能、数据存储位置和当前仅支持 A 股的限制
