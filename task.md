- [ ]  P0 新增 AI 选股，使用 [https://github.com/ZhuLinsen/alphasift](https://github.com/ZhuLinsen/alphasift) 选股引擎
- [ ]  P0【付费功能】新增新闻资讯专栏 使用这个开源项目 [https://github.com/sansan0/TrendRadar](https://github.com/sansan0/TrendRadar)

- [ ] P0 Skills 管理
安装源：
内置 Skills ，即 agent/src/skills 目录下的所有skills
在线 skills，接入 [https://www.skillhub.cn](https://www.skillhub.cn)
自定义 skills ，用户可以自行导入自己的skills，即导入至用户的运行时目录 .vibe-trading/runtime/agent/src/skills 成为专属于用户的能力

文件管理：
skills 文件的管理，提供完整的删除卸载 skills 的管理能力

skills 的使用方式
当用户在输入框 frontend/src/pages/Agent.tsx 中 以 “$” 开头时，则展示上拉列表，展示当前所有已被加载skills，当用户继续输入时，按用户输入内容对已展开的上拉skills 列表进行模糊查询，直到用户找到对应的 skills ，当用户点击选择此 skills 时，即关闭此上拉列表。

【可参考部分】Skills 商店，接入 [https://www.skillhub.cn/](https://www.skillhub.cn/) ，也可以选择将此 skills 上传到云端供用户下载，需要搞定 skills 的分发渠道，不能由我们自己的服务进行托管，最好是能接入类似于 skillshub 的仓库，完成用户上传到发布到分发全链路的分享，将 Vico 自用的 SKILLS 作为内置 skills 进行集成
通过列表管控的形式来提醒用户 skills 不可用？具体方案如下：

1. 前端展示与标记：把 Agent 下的所有 skills 列成一个接口在前端进行展示，并标记该 skills 为“过时 skills”，不建议继续使用。
2. 提供卸载功能：同时提供 skills 的卸载操作。如果用户点击卸载，即认为用户允许删除，同步的操作就是将该 skills 进行完整的删除。

- [ ] P1【付费功能】新增监控中心，关联信号页面，逻辑页面参考 /Users/niean/Documents/project/tickflow-stock-panel/frontend/src/pages/Monitor.tsx
- [ ] P1 新增 Token 用量统计（此功能需要联动登录）
- [ ] P2 策略商店，用户可以将自己的开发策略上传
- [ ] P2 【付费功能】新增定时任务，获取当前的股市信息，发送至消息渠道（如果是对接的情况下，直接推送）
- [ ] P2 做一个可以挂载桌面的盯盘卡片

- 【已完成】新增 VIP Server 服务商的模型列表下拉选择，当用户填写key后，主动拉取模型列表供用户自由选择
- 【已完成】新增指数页面，参考项目 /Users/niean/Documents/project/tickflow-stock-panel/frontend/src/pages/Indices.tsx 的页面
- 【已完成】新增 AI 数据看板，使用 stock-sdk 数据源，也可以考虑是否使用 stock-sdk 的数据源用于替换当前的免费数据源
- 【已完成】frontend/src/pages/Indices.tsx 与 frontend/src/pages/Watchlist.tsx 同样需要移除页面最大宽度的限制，另外 Watchlist 的布局改为左右布局，自选股列表在右，k 线在左，k 线的默认时间范围选在 3M