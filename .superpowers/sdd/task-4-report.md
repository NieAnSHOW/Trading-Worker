# Task 4 Report: 安全 RSS/Atom 解析与字段规范化

## 状态

DONE

## 恢复性审计

既有实现来自 `aa3d401` 和 `63a3409`，但当时的 RED/GREEN 证据未持久化。本次新增了一个真实边界回归：合法 RSS CDATA 文本中出现 `<!DOCTYPE ...>` 字样时，不能被误当作 XML 声明拒绝。

代码提交：`873660d6e72d229957ce808b73a766bfbdf1aace`（`fix(news): preserve literal XML markers in CDATA`，DCO 已签名）。

## 改动文件

- `agent/src/news/feeds.py`
- `agent/tests/news/test_feeds.py`
- `.superpowers/sdd/task-4-report.md`

未改动既有 fixtures；已通过 `rss.xml`、`atom.xml` 和 `malicious_dtd.xml` 进行验证。

## RED 证据

命令：

```bash
pytest agent/tests/news/test_feeds.py -q
```

结果：`1 failed, 11 passed`。新增的 `test_valid_cdata_that_mentions_dtd_is_not_treated_as_a_declaration` 以 `FeedParseError: invalid_xml` 失败，确认原始字节预过滤错误地将 CDATA 内容视为 DTD 标记。

## GREEN 证据

命令：

```bash
pytest agent/tests/news/test_feeds.py -q
python -c "import sys; sys.modules['defusedxml'] = None; import src.news.feeds"
```

结果：`12 passed`；延迟导入命令退出 `0`。

## 约束核对

- `defusedxml.ElementTree` 仅在 `parse_feed()` 内延迟导入；缺失时稳定地报 `parser_unavailable`。
- RSS `item` 与命名空间 Atom `entry` 均有回归覆盖。
- DTD/外部实体由预过滤和 `defusedxml` 拒绝；XSLT 处理指令拒绝。
- HTML 文本经 `HTMLParser`、实体解码、NFKC、空白折叠和标题 300/摘要 1000 字符截断。
- 非空标题与 HTTP(S) URL 是必需字段；摘要和时间缺失时为 `None`。

## 风险信号与顾虑

风险信号命中：是，输入为不可信外部 XML。未访问链接内容、未执行实体或 XSLT。此次修复仅跳过 CDATA/注释中的标记文本，实际 XML 声明仍会被拒绝；无未解决顾虑。
