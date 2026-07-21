## ADDED Requirements

### Requirement: A 股 AI 数据看板入口
系统 SHALL 在 `/dashboard` 提供独立的 A 股 AI 数据看板，并在应用侧边栏提供可访问的导航入口。该页面 SHALL 不改变 `/watchlist`、`/agent` 或现有回测页面的行为。

#### Scenario: 从侧边栏进入看板
- **WHEN** 用户点击侧边栏中的数据看板入口
- **THEN** 系统导航到 `/dashboard`，并将该入口标记为当前活动路由

#### Scenario: 直接访问看板路由
- **WHEN** 用户直接访问 `/dashboard`
- **THEN** 系统懒加载看板页面并展示 A 股市场数据区域

### Requirement: 市场优先的信息层级
看板 SHALL 以市场概览为首要内容，展示上证、深证和创业板指数的最新值与涨跌幅；随后展示市场脉冲、AI 盘面摘要、自选股和选中标的详情。每个区域 SHALL 显示最近一次成功数据的时间及数据可用性状态。

#### Scenario: 行情加载成功
- **WHEN** 指数、板块或自选股行情成功加载
- **THEN** 对应区域展示规范化数据、最后行情时间和来源状态，且上涨/下跌颜色遵循 A 股红涨绿跌惯例

#### Scenario: 单一区域失败
- **WHEN** 指数、市场脉冲、自选股或个股详情中的任一区域加载失败
- **THEN** 系统仅在该区域展示错误或陈旧状态，并继续展示其他已成功加载的区域

### Requirement: 看板轮询与可见性控制
看板 SHALL 在页面可见时每 15 秒刷新行情数据；页面隐藏时 SHALL 暂停轮询，重新可见时 SHALL 立即刷新一次。AI 摘要缓存命中时，行情轮询 SHALL 不触发新的模型调用。

#### Scenario: 页面可见时轮询
- **WHEN** 用户停留在可见的 `/dashboard` 页面超过 15 秒
- **THEN** 系统刷新看板行情数据

#### Scenario: 页面隐藏后暂停
- **WHEN** `document.visibilityState` 变为 `hidden`
- **THEN** 系统停止后续行情轮询，直到页面再次可见

#### Scenario: 返回看板标签页
- **WHEN** 页面从隐藏状态恢复为可见
- **THEN** 系统立即刷新行情数据而不等待下一个轮询间隔

### Requirement: 自选股详情与 Agent 联动
看板 SHALL 读取现有自选股清单，并允许用户选择一个 A 股标的查看日 K 与技术指标。用户 SHALL 能从选中标的发起既有 Agent 分析预填工作流。

#### Scenario: 选择自选股
- **WHEN** 用户选择自选股列表中的一支股票
- **THEN** 系统展示该标的的日 K、可用技术指标和行情状态

#### Scenario: 发送选中标的到 Agent
- **WHEN** 用户对已选择的股票执行“发送到 Agent 分析”
- **THEN** 系统沿用既有预填格式导航到 `/agent`，且不自动提交消息

