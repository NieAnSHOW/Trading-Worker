import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "@/i18n";
import type { NewsPageState } from "@/hooks/useNews";
import type {
  NewsArticle,
  NewsPublicError,
  NewsRefreshStatus,
  NewsSnapshot,
  NewsTrack,
  NewsTrackId,
} from "@/lib/api";

const mocks = vi.hoisted(() => ({
  refreshNews: vi.fn().mockResolvedValue(undefined),
  startNewsRefresh: vi.fn(),
  state: {} as NewsPageState,
}));

vi.mock("@/lib/api", () => ({
  api: { startNewsRefresh: mocks.startNewsRefresh },
}));

vi.mock("@/hooks/useNews", async () => {
  const React = await import("react");
  return {
    useNews: () => {
      const [selectedTrackId, selectTrack] = React.useState<NewsTrackId | null>(
        mocks.state.selectedTrackId,
      );
      return {
        ...mocks.state,
        selectedTrackId,
        selectTrack,
        refreshNews: mocks.refreshNews,
      };
    },
  };
});

import { News } from "../News";

const TRACK_IDS: NewsTrackId[] = [
  "ai",
  "semi",
  "robot",
  "auto",
  "energy",
  "bio",
  "space",
  "security",
  "tech",
  "consumer",
  "macro",
  "science",
];

const publicError: NewsPublicError = {
  code: "upstream_failed",
  message: "news refresh failed",
};

const idleRefresh: NewsRefreshStatus = {
  state: "idle",
  task_id: null,
  started_at: null,
  completed_at: null,
  processed_endpoints: 0,
  successful_endpoints: 0,
  failed_endpoints: 0,
  processed_tracks: 0,
  total_endpoints: 106,
  total_tracks: 12,
  error: null,
};

function article(overrides: Partial<NewsArticle> = {}): NewsArticle {
  return {
    id: "article-1",
    track_id: "ai",
    title: "Original AI headline",
    title_zh: "人工智能新闻",
    summary: "这是一条用于投资研究的摘要。",
    source: {
      id: "source-1",
      name: "Example Wire",
      url: "https://example.com/feed",
    },
    published_at: "2026-07-20T08:00:00Z",
    url: "https://example.com/article",
    ...overrides,
  };
}

function track(
  trackId: NewsTrackId,
  overrides: Partial<NewsTrack> = {},
): NewsTrack {
  return {
    track_id: trackId,
    state: "fresh",
    generated_at: "2026-07-20T09:00:00Z",
    stale: false,
    partial: false,
    items: [
      article({
        id: `${trackId}-article`,
        track_id: trackId,
        title_zh: trackId === "semi" ? "芯片新闻" : "人工智能新闻",
      }),
    ],
    ai: {
      available: true,
      generated_at: "2026-07-20T09:01:00Z",
      highlights: ["要点一", "要点二", "要点三"],
      error: null,
    },
    source_stats: {
      endpoint_success_count: 2,
      endpoint_failure_count: 0,
      assignment_success_count: 2,
      assignment_failure_count: 0,
    },
    ...overrides,
  };
}

function snapshot(overrides: Partial<NewsSnapshot> = {}): NewsSnapshot {
  return {
    schema_version: 1,
    generated_at: "2026-07-20T09:00:00Z",
    upstream_commit: "fixture-commit",
    source_stats: {
      endpoint_success_count: 24,
      endpoint_failure_count: 0,
      assignment_success_count: 24,
      assignment_failure_count: 0,
    },
    errors: [],
    tracks: TRACK_IDS.map((trackId) => track(trackId)),
    ...overrides,
  };
}

function pageState(overrides: Partial<NewsPageState> = {}): NewsPageState {
  return {
    snapshot: snapshot(),
    available: true,
    stale: false,
    snapshotError: null,
    refreshStatus: idleRefresh,
    selectedTrackId: "ai",
    isLoading: false,
    isRefreshing: false,
    error: null,
    ...overrides,
  };
}

describe("News workspace", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("zh-CN");
    mocks.state = pageState();
    mocks.refreshNews.mockClear();
    mocks.startNewsRefresh.mockClear();
  });

  it("renders all 12 desktop tracks and a mobile track selector", () => {
    render(<News />);

    expect(screen.getAllByRole("tab")).toHaveLength(12);
    const selector = screen.getByRole("combobox", { name: "选择资讯赛道" });
    expect(within(selector).getAllByRole("option")).toHaveLength(12);
  });

  it("exposes a roving tab set linked to the selected track panel", async () => {
    const user = userEvent.setup();
    render(<News />);

    const aiTab = screen.getByRole("tab", { name: "人工智能" });
    const semiTab = screen.getByRole("tab", { name: "半导体" });
    expect(aiTab).toHaveAttribute("tabindex", "0");
    expect(semiTab).toHaveAttribute("tabindex", "-1");
    expect(aiTab).toHaveAttribute("aria-controls", "news-panel-ai");
    for (const tab of screen.getAllByRole("tab")) {
      const panelId = tab.getAttribute("aria-controls");
      expect(panelId).toBeTruthy();
      expect(document.getElementById(panelId!)).toHaveAttribute(
        "aria-labelledby",
        tab.id,
      );
    }
    expect(screen.getByRole("tabpanel")).toHaveAttribute(
      "aria-labelledby",
      "news-tab-ai",
    );

    aiTab.focus();
    await user.keyboard("{ArrowRight}");

    expect(semiTab).toHaveFocus();
    expect(semiTab).toHaveAttribute("tabindex", "0");
    expect(aiTab).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tabpanel")).toHaveAttribute(
      "aria-labelledby",
      "news-tab-semi",
    );
    expect(screen.getByRole("heading", { name: "芯片新闻" })).toBeInTheDocument();
  });

  it("supports Home and End keyboard navigation across desktop tracks", async () => {
    const user = userEvent.setup();
    render(<News />);

    const firstTab = screen.getByRole("tab", { name: "人工智能" });
    const lastTab = screen.getByRole("tab", { name: "科学" });
    firstTab.focus();

    await user.keyboard("{ArrowLeft}");
    expect(lastTab).toHaveFocus();
    expect(lastTab).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowRight}");
    expect(firstTab).toHaveFocus();
    expect(firstTab).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{End}");
    expect(lastTab).toHaveFocus();
    expect(lastTab).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{Home}");
    expect(firstTab).toHaveFocus();
    expect(firstTab).toHaveAttribute("aria-selected", "true");
  });

  it("switches tracks without changing refresh scope", async () => {
    const user = userEvent.setup();
    render(<News />);

    await user.click(screen.getByRole("tab", { name: "半导体" }));

    expect(screen.getByRole("heading", { name: "芯片新闻" })).toBeInTheDocument();
    expect(mocks.refreshNews).not.toHaveBeenCalled();
    expect(mocks.startNewsRefresh).not.toHaveBeenCalled();
  });

  it("switches tracks through the mobile selector", async () => {
    const user = userEvent.setup();
    render(<News />);

    await user.selectOptions(
      screen.getByRole("combobox", { name: "选择资讯赛道" }),
      "semi",
    );

    expect(screen.getByRole("heading", { name: "芯片新闻" })).toBeInTheDocument();
  });

  it("uses the Chinese title first and falls back for missing fields", () => {
    const current = track("ai", {
      items: [
        article({ title: "English fallback", title_zh: "中文标题" }),
        article({
          id: "missing-fields",
          title: "Fallback headline",
          title_zh: null,
          summary: null,
          published_at: null,
        }),
      ],
    });
    mocks.state = pageState({
      snapshot: snapshot({
        tracks: TRACK_IDS.map((id) => (id === "ai" ? current : track(id))),
      }),
    });

    render(<News />);

    expect(screen.getByRole("heading", { name: "中文标题" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Fallback headline" })).toBeInTheDocument();
    expect(screen.queryByText("English fallback")).not.toBeInTheDocument();
    expect(screen.getByTestId("missing-fields")).toHaveTextContent("暂无摘要");
    expect(screen.getByTestId("missing-fields")).toHaveTextContent("时间未知");
  });

  it("renders between three and five AI highlights", () => {
    const highlighted = track("ai", {
      ai: {
        available: true,
        generated_at: "2026-07-20T09:01:00Z",
        highlights: ["要点一", "要点二", "要点三", "要点四", "要点五"],
        error: null,
      },
    });
    mocks.state = pageState({
      snapshot: snapshot({
        tracks: TRACK_IDS.map((id) => (id === "ai" ? highlighted : track(id))),
      }),
    });

    render(<News />);

    expect(within(screen.getByRole("list", { name: "AI 要点" })).getAllByRole("listitem")).toHaveLength(5);
  });

  it.each([
    {
      label: "fresh",
      state: pageState(),
      expected: "最新",
    },
    {
      label: "stale",
      state: pageState({ stale: true }),
      expected: "数据可能已过期",
    },
    {
      label: "unavailable",
      state: pageState({
        snapshot: snapshot({
          tracks: TRACK_IDS.map((id) =>
            id === "ai"
              ? track(id, {
                  state: "unavailable",
                  generated_at: null,
                  items: [],
                  ai: { available: false, generated_at: null, highlights: [], error: publicError },
                })
              : track(id),
          ),
        }),
      }),
      expected: "资讯暂不可用",
    },
    {
      label: "partial",
      state: pageState({
        snapshot: snapshot({
          tracks: TRACK_IDS.map((id) =>
            id === "ai" ? track(id, { partial: true }) : track(id),
          ),
        }),
      }),
      expected: "部分来源暂不可用",
    },
  ])("shows the $label data state", ({ state, expected }) => {
    mocks.state = state;
    render(<News />);
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it("shows when AI highlights are unavailable", () => {
    mocks.state = pageState({
      snapshot: snapshot({
        tracks: TRACK_IDS.map((id) =>
          id === "ai"
            ? track(id, {
                ai: { available: false, generated_at: null, highlights: [], error: publicError },
              })
            : track(id),
        ),
      }),
    });

    render(<News />);
    expect(screen.getByText("AI 要点暂不可用")).toBeInTheDocument();
  });

  it("shows loading, empty snapshot, refresh progress, and overall failure", () => {
    const { rerender } = render(<News />);

    mocks.state = pageState({ snapshot: null, available: false, selectedTrackId: null, isLoading: true });
    rerender(<News />);
    expect(screen.getByText("正在加载资讯…")).toBeInTheDocument();

    mocks.state = pageState({ snapshot: null, available: false, selectedTrackId: null });
    rerender(<News />);
    expect(screen.getByText("暂无资讯快照")).toBeInTheDocument();

    mocks.state = pageState({
      isRefreshing: true,
      refreshStatus: {
        ...idleRefresh,
        state: "fetching",
        processed_endpoints: 26,
        total_endpoints: 106,
        processed_tracks: 3,
      },
    });
    rerender(<News />);
    expect(screen.getByText("正在刷新：26 / 106 个来源，3 / 12 个赛道")).toBeInTheDocument();

    mocks.state = pageState({ error: new Error("backend offline") });
    rerender(<News />);
    expect(screen.getByText("加载资讯时出现问题")).toBeInTheDocument();
    expect(screen.getByText("backend offline")).toBeInTheDocument();
  });

  it("refreshes all news from an accessible icon button", async () => {
    const user = userEvent.setup();
    render(<News />);

    const refresh = screen.getByRole("button", { name: "刷新全部资讯" });
    expect(refresh).toHaveAttribute("title", "刷新全部资讯");
    await user.click(refresh);

    expect(mocks.refreshNews).toHaveBeenCalledOnce();
  });

  it("renders only valid http article URLs as external links", () => {
    const current = track("ai", {
      items: [
        article({ id: "safe", title_zh: "安全链接", url: "https://example.com/story" }),
        article({ id: "unsafe", title_zh: "危险链接", url: "javascript:alert(1)" }),
        article({ id: "malformed", title_zh: "损坏链接", url: "not a url" }),
      ],
    });
    mocks.state = pageState({
      snapshot: snapshot({
        tracks: TRACK_IDS.map((id) => (id === "ai" ? current : track(id))),
      }),
    });

    render(<News />);

    const safe = within(screen.getByTestId("safe")).getByRole("link", { name: "查看原文" });
    expect(safe).toHaveAttribute("href", "https://example.com/story");
    expect(safe).toHaveAttribute("target", "_blank");
    expect(safe).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(screen.getByTestId("unsafe")).queryByRole("link")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("malformed")).queryByRole("link")).not.toBeInTheDocument();
  });

  it("localizes the complete workspace and formats dates with the resolved language", async () => {
    const dateSpy = vi
      .spyOn(Date.prototype, "toLocaleString")
      .mockImplementation(function formatDate(locales) {
        return `formatted:${String(locales)}`;
      });
    await i18n.changeLanguage("en");
    const current = track("ai", {
      items: [
        article({
          id: "missing-fields",
          title_zh: null,
          summary: null,
          published_at: null,
        }),
        article({
          id: "dated-article",
          title_zh: null,
          published_at: "2026-07-20T08:00:00Z",
        }),
      ],
    });
    mocks.state = pageState({
      isRefreshing: true,
      refreshStatus: {
        ...idleRefresh,
        state: "fetching",
        processed_endpoints: 26,
        processed_tracks: 3,
      },
      snapshot: snapshot({
        tracks: TRACK_IDS.map((id) => (id === "ai" ? current : track(id))),
      }),
    });

    const { rerender } = render(<News />);

    expect(screen.getByRole("heading", { name: "Investment News" })).toBeInTheDocument();
    expect(screen.getByRole("tablist", { name: "News tracks" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Select news track" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh all news" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Refreshing: 26 / 106 sources, 3 / 12 tracks",
    );
    expect(screen.getByTestId("missing-fields")).toHaveTextContent("No summary available");
    expect(screen.getByTestId("missing-fields")).toHaveTextContent("Time unavailable");
    expect(screen.getAllByRole("link", { name: "View original" })).toHaveLength(2);
    expect(screen.getByRole("region", { name: "News articles" })).toBeInTheDocument();
    expect(screen.getAllByText("formatted:en")).toHaveLength(2);
    expect(dateSpy).toHaveBeenCalledTimes(2);
    expect(dateSpy).toHaveBeenNthCalledWith(1, "en");
    expect(dateSpy).toHaveBeenNthCalledWith(2, "en");

    mocks.state = pageState({
      snapshot: null,
      available: false,
      selectedTrackId: null,
      isLoading: true,
    });
    rerender(<News />);
    expect(screen.getByText("Loading news…")).toBeInTheDocument();

    mocks.state = pageState({
      snapshot: null,
      available: false,
      selectedTrackId: null,
    });
    rerender(<News />);
    expect(screen.getByText("No news snapshot is available")).toBeInTheDocument();
  });
});
