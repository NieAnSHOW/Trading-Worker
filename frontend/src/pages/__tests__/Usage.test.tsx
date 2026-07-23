import { StrictMode } from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api } from "@/lib/api";
import { echarts } from "@/lib/echarts";
import i18n from "@/i18n";
import { Usage } from "../Usage";

const setOption = vi.fn();
const dispose = vi.fn();
const resize = vi.fn();

vi.mock("@/lib/echarts", () => ({
  echarts: {
    init: vi.fn(() => ({ setOption, dispose, resize })),
  },
}));

vi.mock("@/hooks/useDarkMode", () => ({
  useDarkMode: () => ({ dark: false }),
}));

vi.mock("@/lib/api", () => ({
  api: { getLLMUsage: vi.fn() },
}));

const usageResponse = {
  generated_at: "2026-07-23T04:00:00Z",
  timezone: "Asia/Shanghai",
  period: { start_at: null, end_at: null },
  totals: {
    input_tokens: 700,
    output_tokens: 300,
    total_tokens: 1000,
    calls: 4,
    runs: 2,
    sessions: 2,
    missing_usage_runs: 0,
    invalid_usage_runs: 0,
    cache_read_tokens: 80,
    cache_write_tokens: 20,
    cache_read_reported_runs: 1,
    cache_write_reported_runs: 1,
  },
  trend: [
    { date: "2026-07-22", totals: { input_tokens: 300, output_tokens: 100, total_tokens: 400, calls: 1 } },
    { date: "2026-07-23", totals: { input_tokens: 400, output_tokens: 200, total_tokens: 600, calls: 3 } },
  ],
  breakdown: [
    { provider: "openai", model: "gpt-5", totals: { input_tokens: 400, output_tokens: 200, total_tokens: 600, calls: 3 } },
    { provider: "anthropic", model: "claude", totals: { input_tokens: 300, output_tokens: 100, total_tokens: 400, calls: 1 } },
  ],
  sessions: {
    items: [
      {
        session_id: "session-a",
        title: "会话 A",
        last_run_at: "2026-07-23T03:00:00Z",
        totals: { input_tokens: 400, output_tokens: 200, total_tokens: 600, calls: 3 },
        runs: [{
          run_id: "run-a",
          occurred_at: "2026-07-23T02:00:00Z",
          provider: "openai",
          model: "gpt-5",
          metering_eligible: true,
          prompt: "attempt-prompt-sentinel",
          response: "model-response-sentinel",
          totals: { input_tokens: 400, output_tokens: 200, total_tokens: 600, calls: 3 },
        }],
      },
      {
        session_id: "session-b",
        title: "会话 B",
        last_run_at: "2026-07-22T03:00:00Z",
        totals: { input_tokens: 300, output_tokens: 100, total_tokens: 400, calls: 1 },
        runs: [],
      },
    ],
    page: 1,
    page_size: 25,
    total_items: 26,
    total_pages: 2,
  },
};

const getLLMUsage = vi.mocked(api.getLLMUsage);
const init = vi.mocked(echarts.init);
const originalTimeZone = process.env.TZ;

function renderUsage() {
  return render(<MemoryRouter><Usage /></MemoryRouter>);
}

describe("Usage", () => {
  beforeEach(async () => {
    process.env.TZ = "Asia/Shanghai";
    await i18n.changeLanguage("zh-CN");
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-07-23T04:00:00Z"));
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      timeZone: "Asia/Shanghai",
    } as Intl.ResolvedDateTimeFormatOptions);
    getLLMUsage.mockReset();
    getLLMUsage.mockResolvedValue(structuredClone(usageResponse));
    setOption.mockClear();
    dispose.mockClear();
    resize.mockClear();
    init.mockClear();
    document.documentElement.classList.remove("dark");
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  afterAll(() => {
    if (originalTimeZone === undefined) delete process.env.TZ;
    else process.env.TZ = originalTimeZone;
  });

  it("loads all-time usage once in the browser timezone and expands safe run links", async () => {
    renderUsage();

    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith({
      timezone: "Asia/Shanghai", page: 1, page_size: 25,
    }));
    expect(getLLMUsage).toHaveBeenCalledTimes(1);
    expect(screen.getByText("1,000")).toBeInTheDocument();

    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(
      screen.getByRole("button", { name: /会话 A/i }),
    );
    expect(screen.getByRole("link", { name: /run-a/i })).toHaveAttribute("href", "/runs/run-a");
    expect(screen.queryByText("attempt-prompt-sentinel")).not.toBeInTheDocument();
    expect(screen.queryByText("model-response-sentinel")).not.toBeInTheDocument();
  });

  it("deduplicates the initial aggregate request under StrictMode", async () => {
    render(<StrictMode><MemoryRouter><Usage /></MemoryRouter></StrictMode>);

    await waitFor(() => expect(getLLMUsage).toHaveBeenCalled());
    expect(getLLMUsage).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["近 7 天", "2026-07-16T04:00:00.000Z", "2026-07-23T04:00:00.000Z"],
    ["近 30 天", "2026-06-23T04:00:00.000Z", "2026-07-23T04:00:00.000Z"],
    ["本月", "2026-06-30T16:00:00.000Z", "2026-07-23T04:00:00.000Z"],
  ])("submits the %s UTC boundaries", async (label, start_at, end_at) => {
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockClear();

    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(screen.getByRole("button", { name: label }));

    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith({
      timezone: "Asia/Shanghai",
      page: 1,
      page_size: 25,
      start_at: expect.stringMatching(`^${start_at.slice(0, 19)}`),
      end_at: expect.stringMatching(`^${end_at.slice(0, 19)}`),
    }));
  });

  it.each([
    ["近 7 天", "2026-03-05T17:00:00.000Z"],
    ["近 30 天", "2026-02-10T17:00:00.000Z"],
  ])("subtracts local calendar days for %s across New York DST", async (label, start_at) => {
    process.env.TZ = "America/New_York";
    vi.setSystemTime(new Date("2026-03-12T16:00:00.000Z"));
    vi.mocked(Intl.DateTimeFormat.prototype.resolvedOptions).mockReturnValue({
      timeZone: "America/New_York",
    } as Intl.ResolvedDateTimeFormatOptions);
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockClear();

    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(screen.getByRole("button", { name: label }));

    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith({
      timezone: "America/New_York",
      page: 1,
      page_size: 25,
      start_at: expect.stringMatching(`^${start_at.slice(0, 19)}`),
      end_at: expect.stringMatching("^2026-03-12T16:00:00"),
    }));
  });

  it("uses local calendar midnights and an exclusive next-day custom end", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockClear();

    await user.click(screen.getByRole("button", { name: "自定义" }));
    await user.type(screen.getByLabelText("开始日期"), "2026-07-01");
    await user.type(screen.getByLabelText("结束日期"), "2026-07-02");
    await user.click(screen.getByRole("button", { name: "应用" }));

    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith({
      timezone: "Asia/Shanghai",
      page: 1,
      page_size: 25,
      start_at: "2026-06-30T16:00:00.000Z",
      end_at: "2026-07-02T16:00:00.000Z",
    }));
  });

  it("searches and paginates session details without replacing aggregate totals", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockClear();

    await user.type(screen.getByRole("searchbox", { name: "搜索会话" }), "会话 A");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith(expect.objectContaining({ query: "会话 A", page: 1 })));
    expect(screen.getByText("1,000")).toBeInTheDocument();

    getLLMUsage.mockClear();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith(expect.objectContaining({ query: "会话 A", page: 2 })));
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("keeps the applied range selected when a replacement range fails", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockRejectedValueOnce(new Error("range failed"));

    await user.click(screen.getByRole("button", { name: "近 7 天" }));

    expect(await screen.findByText("range failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "全部历史" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "近 7 天" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("restores applied custom dates when replacement dates fail", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    await user.click(screen.getByRole("button", { name: "自定义" }));
    const startDate = screen.getByLabelText("开始日期");
    const endDate = screen.getByLabelText("结束日期");
    await user.type(startDate, "2026-07-01");
    await user.type(endDate, "2026-07-02");
    await user.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "自定义" })).toHaveAttribute("aria-pressed", "true"));

    getLLMUsage.mockRejectedValueOnce(new Error("custom failed"));
    await user.clear(startDate);
    await user.type(startDate, "2026-07-03");
    await user.clear(endDate);
    await user.type(endDate, "2026-07-04");
    await user.click(screen.getByRole("button", { name: "应用" }));

    expect(await screen.findByText("custom failed")).toBeInTheDocument();
    expect(startDate).toHaveValue("2026-07-01");
    expect(endDate).toHaveValue("2026-07-02");
    expect(screen.getByRole("button", { name: "自定义" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("restores the applied search text when a replacement search fails", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockRejectedValueOnce(new Error("search failed"));
    const search = screen.getByRole("searchbox", { name: "搜索会话" });

    await user.type(search, "会话 A");
    await user.keyboard("{Enter}");

    expect(await screen.findByText("search failed")).toBeInTheDocument();
    expect(search).toHaveValue("");
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("custom-failure-plus-search-edit rolls back only the failed custom draft", async () => {
    let rejectCustom!: (reason?: unknown) => void;
    const pendingCustom = new Promise<typeof usageResponse>((_resolve, reject) => { rejectCustom = reject; });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");

    await user.click(screen.getByRole("button", { name: "自定义" }));
    const startDate = screen.getByLabelText("开始日期");
    const endDate = screen.getByLabelText("结束日期");
    await user.type(startDate, "2026-07-01");
    await user.type(endDate, "2026-07-02");
    await user.click(screen.getByRole("button", { name: "应用" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "自定义" })).toHaveAttribute("aria-pressed", "true"));

    getLLMUsage.mockImplementationOnce(() => pendingCustom);
    await user.clear(startDate);
    await user.type(startDate, "2026-07-03");
    await user.clear(endDate);
    await user.type(endDate, "2026-07-04");
    await user.click(screen.getByRole("button", { name: "应用" }));
    await user.type(screen.getByRole("searchbox", { name: "搜索会话" }), "保留搜索草稿");
    await act(async () => rejectCustom(new Error("custom failed")));

    expect(await screen.findByText("custom failed")).toBeInTheDocument();
    expect(startDate).toHaveValue("2026-07-01");
    expect(endDate).toHaveValue("2026-07-02");
    expect(screen.getByRole("searchbox", { name: "搜索会话" })).toHaveValue("保留搜索草稿");
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("search-failure-plus-custom-edit rolls back only the failed search draft", async () => {
    let rejectSearch!: (reason?: unknown) => void;
    const pendingSearch = new Promise<typeof usageResponse>((_resolve, reject) => { rejectSearch = reject; });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");

    getLLMUsage.mockImplementationOnce(() => pendingSearch);
    const search = screen.getByRole("searchbox", { name: "搜索会话" });
    await user.type(search, "失败搜索");
    await user.keyboard("{Enter}");
    await user.click(screen.getByRole("button", { name: "自定义" }));
    await user.type(screen.getByLabelText("开始日期"), "2026-07-03");
    await user.type(screen.getByLabelText("结束日期"), "2026-07-04");
    await act(async () => rejectSearch(new Error("search failed")));

    expect(await screen.findByText("search failed")).toBeInTheDocument();
    expect(search).toHaveValue("");
    expect(screen.getByLabelText("开始日期")).toHaveValue("2026-07-03");
    expect(screen.getByLabelText("结束日期")).toHaveValue("2026-07-04");
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("keeps pagination on the snapshot page when a replacement page fails", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockRejectedValueOnce(new Error("page failed"));

    await user.click(screen.getByRole("button", { name: "下一页" }));

    expect(await screen.findByText("page failed")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("keeps the snapshot visible during refresh and records successful refresh time", async () => {
    let resolveRefresh!: (value: typeof usageResponse) => void;
    const pending = new Promise<typeof usageResponse>((resolve) => { resolveRefresh = resolve; });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockImplementationOnce(() => pending);

    await user.click(screen.getByRole("button", { name: "刷新" }));
    expect(screen.getByText("1,000")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新中" })).toBeDisabled();

    await act(async () => resolveRefresh({ ...usageResponse, totals: { ...usageResponse.totals, total_tokens: 1200 } }));
    expect(await screen.findByText("1,200")).toBeInTheDocument();
    expect(screen.getByText(/更新于/)).toBeInTheDocument();
  });

  it("ignores an older filter response that resolves after the latest request", async () => {
    let resolveOlder!: (value: typeof usageResponse) => void;
    let resolveLatest!: (value: typeof usageResponse) => void;
    const older = new Promise<typeof usageResponse>((resolve) => { resolveOlder = resolve; });
    const latest = new Promise<typeof usageResponse>((resolve) => { resolveLatest = resolve; });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockImplementationOnce(() => older).mockImplementationOnce(() => latest);

    await user.click(screen.getByRole("button", { name: "近 7 天" }));
    await user.click(screen.getByRole("button", { name: "近 30 天" }));
    await act(async () => resolveLatest({ ...usageResponse, totals: { ...usageResponse.totals, total_tokens: 3000 } }));
    expect(await screen.findByText("3,000")).toBeInTheDocument();

    await act(async () => resolveOlder({ ...usageResponse, totals: { ...usageResponse.totals, total_tokens: 7000 } }));
    expect(screen.getByText("3,000")).toBeInTheDocument();
    expect(screen.queryByText("7,000")).not.toBeInTheDocument();
  });

  it("keeps a custom editor opened after an earlier range request succeeds", async () => {
    let resolveRange!: (value: typeof usageResponse) => void;
    const pendingRange = new Promise<typeof usageResponse>((resolve) => { resolveRange = resolve; });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockImplementationOnce(() => pendingRange);

    await user.click(screen.getByRole("button", { name: "近 7 天" }));
    await waitFor(() => expect(getLLMUsage).toHaveBeenLastCalledWith(expect.objectContaining({
      start_at: expect.stringMatching("^2026-07-16T04:00:00"),
    })));
    await user.click(screen.getByRole("button", { name: "自定义" }));
    expect(screen.getByLabelText("开始日期")).toBeInTheDocument();

    await act(async () => resolveRange(structuredClone(usageResponse)));
    expect(screen.getByRole("button", { name: "近 7 天" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("开始日期")).toBeInTheDocument();
  });

  it("merges a pending search into the latest range request and ignores the older response", async () => {
    let resolveSearch!: (value: typeof usageResponse) => void;
    const pendingSearch = new Promise<typeof usageResponse>((resolve) => { resolveSearch = resolve; });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockImplementationOnce(() => pendingSearch).mockResolvedValueOnce({
      ...structuredClone(usageResponse),
      totals: { ...usageResponse.totals, total_tokens: 7000 },
    });

    await user.type(screen.getByRole("searchbox", { name: "搜索会话" }), "会话 A");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(getLLMUsage).toHaveBeenLastCalledWith(expect.objectContaining({ query: "会话 A" })));
    await user.click(screen.getByRole("button", { name: "近 7 天" }));

    await waitFor(() => expect(getLLMUsage).toHaveBeenLastCalledWith(expect.objectContaining({
      query: "会话 A",
      page: 1,
      start_at: expect.stringMatching("^2026-07-16T04:00:00"),
    })));
    expect(await screen.findByText("7,000")).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "搜索会话" })).toHaveValue("会话 A");
    expect(screen.getByRole("button", { name: "近 7 天" })).toHaveAttribute("aria-pressed", "true");

    await act(async () => resolveSearch({
      ...structuredClone(usageResponse),
      totals: { ...usageResponse.totals, total_tokens: 3000 },
    }));
    expect(screen.getByText("7,000")).toBeInTheDocument();
    expect(screen.queryByText("3,000")).not.toBeInTheDocument();
  });

  it("retries an initial failure and preserves an existing snapshot after refresh failure", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    getLLMUsage.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(structuredClone(usageResponse));
    renderUsage();

    expect(await screen.findByText("offline")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("1,000")).toBeInTheDocument();

    getLLMUsage.mockRejectedValueOnce(new Error("refresh failed"));
    await user.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByText("refresh failed")).toBeInTheDocument();
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("shows empty and partial-data states without calling missing cache values zero", async () => {
    getLLMUsage.mockResolvedValueOnce({
      ...structuredClone(usageResponse),
      totals: {
        ...usageResponse.totals,
        runs: 0,
        missing_usage_runs: 2,
        invalid_usage_runs: 1,
        cache_read_tokens: undefined,
        cache_write_tokens: undefined,
        cache_read_reported_runs: 0,
        cache_write_reported_runs: 0,
      },
      sessions: { ...usageResponse.sessions, items: [], total_items: 0, total_pages: 0 },
    });
    renderUsage();

    expect(await screen.findByText(/无供应商报告的用量/)).toBeInTheDocument();
    expect(screen.getByText(/部分运行数据不可用/)).toBeInTheDocument();
    expect(screen.getByText(/缺少用量数据的运行: 2/)).toBeInTheDocument();
    expect(screen.getByText(/用量数据无效的运行: 1/)).toBeInTheDocument();
    expect(screen.getAllByText("未提供").length).toBeGreaterThanOrEqual(2);
  });

  it("renders accessible charts with API-provided trend and sorted breakdown data and disposes them", async () => {
    const { unmount } = renderUsage();
    await screen.findByText("1,000");

    expect(screen.getByRole("img", { name: "Token 趋势" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "供应商和模型分布" })).toBeInTheDocument();
    expect(setOption).toHaveBeenCalledWith(expect.objectContaining({
      xAxis: expect.objectContaining({ data: ["2026-07-22", "2026-07-23"] }),
      series: [expect.objectContaining({ data: [400, 600] })],
    }));
    expect(setOption).toHaveBeenCalledWith(expect.objectContaining({
      yAxis: expect.objectContaining({ data: ["openai / gpt-5", "anthropic / claude"] }),
      series: [expect.objectContaining({
        data: [
          { value: 600, itemStyle: { color: "#f59e0b" } },
          { value: 400, itemStyle: { color: "#8b5cf6" } },
        ],
      })],
    }));

    unmount();
    expect(dispose).toHaveBeenCalledTimes(2);
  });

  it("rebuilds and cleans up both charts when the root dark class changes", async () => {
    const disconnect = vi.spyOn(ResizeObserver.prototype, "disconnect");
    renderUsage();
    await screen.findByText("1,000");
    expect(init).toHaveBeenCalledTimes(2);

    await act(async () => {
      document.documentElement.classList.add("dark");
      await Promise.resolve();
    });

    await waitFor(() => expect(init).toHaveBeenCalledTimes(4));
    expect(dispose).toHaveBeenCalledTimes(2);
    expect(disconnect).toHaveBeenCalledTimes(2);
  });

  it("renders sessions in vertically safe rows on narrow screens", async () => {
    const { container } = renderUsage();
    await screen.findByText("1,000");

    const sessionList = screen.getByRole("region", { name: "会话明细" });
    expect(within(sessionList).getByRole("button", { name: /会话 A/i })).toHaveClass("flex-col", "sm:flex-row");
    expect(container.querySelector(".overflow-x-auto")).not.toBeInTheDocument();
  });

  it("formats usage numbers and dates with the active i18n locale", async () => {
    await i18n.changeLanguage("ar");
    renderUsage();

    expect(await screen.findByText(new Intl.NumberFormat("ar").format(1000))).toBeInTheDocument();
    expect(screen.getByText(new Date("2026-07-23T03:00:00Z").toLocaleString("ar"))).toBeInTheDocument();
  });

  it("mirrors pagination chevrons in RTL locales", async () => {
    await i18n.changeLanguage("ar");
    renderUsage();
    await screen.findByText(new Intl.NumberFormat("ar").format(1000));

    const previous = screen.getByRole("button", { name: i18n.t("usageCenter.previousPage") });
    const next = screen.getByRole("button", { name: i18n.t("usageCenter.nextPage") });
    expect(previous.querySelector("svg")).toHaveClass("rtl:flip-x");
    expect(next.querySelector("svg")).toHaveClass("rtl:flip-x");
  });
});
