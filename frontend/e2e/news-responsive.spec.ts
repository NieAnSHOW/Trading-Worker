import { expect, test, type Locator, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const evidenceRoot = path.resolve(
  process.cwd(),
  "../docs/superpowers/reviews",
);
const assetsRoot = path.join(evidenceRoot, "assets");
mkdirSync(assetsRoot, { recursive: true });

const trackIds = [
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
] as const;

const sourceStats = {
  endpoint_success_count: 2,
  endpoint_failure_count: 0,
  assignment_success_count: 2,
  assignment_failure_count: 0,
};

const refreshStatus = {
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

const snapshotResponse = {
  available: true,
  stale: false,
  snapshot: {
    schema_version: 1,
    generated_at: "2026-07-20T09:00:00Z",
    upstream_commit: "playwright-fixture",
    source_stats: {
      endpoint_success_count: 24,
      endpoint_failure_count: 0,
      assignment_success_count: 24,
      assignment_failure_count: 0,
    },
    errors: [],
    tracks: trackIds.map((trackId, index) => ({
      track_id: trackId,
      state: "fresh",
      generated_at: "2026-07-20T09:00:00Z",
      stale: false,
      partial: false,
      items: [
        {
          id: `${trackId}-article`,
          track_id: trackId,
          title: `Original investment headline ${index + 1}`,
          title_zh:
            trackId === "ai"
              ? "人工智能基础设施投资进入效率验证阶段"
              : `赛道资讯 ${index + 1}`,
          summary:
            "资本开支、供应链交付与盈利兑现成为本轮研究的共同观察指标。",
          source: {
            id: "fixture-wire",
            name: "Fixture Wire",
            url: "https://example.com/feed",
          },
          published_at: "2026-07-20T08:30:00Z",
          url: `https://example.com/articles/${trackId}`,
        },
      ],
      ai: {
        available: true,
        generated_at: "2026-07-20T09:01:00Z",
        highlights: [
          "算力投资正从规模扩张转向利用率与单位经济性验证。",
          "供应链交付周期缩短，但关键器件仍需跟踪库存变化。",
          "短期催化来自业绩兑现，中期风险集中在资本回报率。",
        ],
        error: null,
      },
      source_stats: sourceStats,
    })),
  },
  refresh: refreshStatus,
  error: null,
};

type ViewportEvidence = {
  name: "mobile" | "desktop";
  width: number;
  height: number;
  screenshot: string;
};

const completedEvidence: ViewportEvidence[] = [];

function boxesOverlap(
  first: NonNullable<Awaited<ReturnType<Locator["boundingBox"]>>>,
  second: NonNullable<Awaited<ReturnType<Locator["boundingBox"]>>>,
): boolean {
  return !(
    first.x + first.width <= second.x
    || second.x + second.width <= first.x
    || first.y + first.height <= second.y
    || second.y + second.height <= first.y
  );
}

async function expectPairwiseSeparation(locators: Locator[]) {
  const boxes = await Promise.all(locators.map((locator) => locator.boundingBox()));
  boxes.forEach((box) => expect(box).not.toBeNull());

  for (let firstIndex = 0; firstIndex < boxes.length; firstIndex += 1) {
    for (let secondIndex = firstIndex + 1; secondIndex < boxes.length; secondIndex += 1) {
      expect(
        boxesOverlap(boxes[firstIndex]!, boxes[secondIndex]!),
        `elements ${firstIndex} and ${secondIndex} overlap`,
      ).toBe(false);
    }
  }
}

async function interceptNewsApi(page: Page) {
  await page.route("**/news-api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/news-api/snapshot") {
      await route.fulfill({ status: 200, json: snapshotResponse });
      return;
    }
    if (pathname === "/news-api/refresh/status") {
      await route.fulfill({ status: 200, json: refreshStatus });
      return;
    }
    if (pathname === "/news-api/refresh") {
      await route.fulfill({
        status: 202,
        json: {
          task_id: "00000000-0000-4000-8000-000000000001",
          reused: false,
          status: {
            ...refreshStatus,
            state: "fetching",
            task_id: "00000000-0000-4000-8000-000000000001",
            started_at: "2026-07-20T09:02:00Z",
          },
        },
      });
      return;
    }
    await route.abort("blockedbyclient");
  });

  await page.route("**/sessions**", async (route) => {
    await route.fulfill({ status: 200, json: [] });
  });
}

const viewports: ViewportEvidence[] = [
  {
    name: "mobile",
    width: 390,
    height: 844,
    screenshot: "investment-news-mobile.png",
  },
  {
    name: "desktop",
    width: 1440,
    height: 900,
    screenshot: "investment-news-desktop.png",
  },
];

for (const viewport of viewports) {
  test(`${viewport.name} news workspace stays within the viewport`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.addInitScript(() => {
      window.localStorage.setItem("i18nextLng", "zh-CN");
      window.localStorage.setItem("qa-sidebar", "collapsed");
    });
    await interceptNewsApi(page);

    await page.goto("/news");
    await expect(page.getByRole("heading", { name: "投资资讯" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "人工智能基础设施投资进入效率验证阶段" })).toBeVisible();
    await expect(page.getByRole("list", { name: "AI 要点" }).getByRole("listitem")).toHaveCount(3);

    const trackControl = viewport.name === "mobile"
      ? page.getByRole("combobox", { name: "选择资讯赛道" })
      : page.getByRole("tablist", { name: "资讯赛道" });
    const hiddenTrackControl = viewport.name === "mobile"
      ? page.getByRole("tablist", { name: "资讯赛道" })
      : page.getByRole("combobox", { name: "选择资讯赛道" });

    await expect(trackControl).toBeVisible();
    await expect(hiddenTrackControl).toBeHidden();
    await expect(page.getByRole("button", { name: "刷新全部资讯" })).toBeVisible();
    await expect(page.getByTestId("ai-article")).toBeVisible();

    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(hasHorizontalOverflow).toBe(false);

    await expectPairwiseSeparation([
      trackControl,
      page.getByRole("button", { name: "刷新全部资讯" }),
      page.getByRole("list", { name: "AI 要点" }),
      page.getByTestId("ai-article"),
    ]);

    await page.screenshot({
      path: path.join(assetsRoot, viewport.screenshot),
      fullPage: true,
    });
    completedEvidence.push(viewport);
  });
}

test.afterAll(() => {
  const rows = completedEvidence.map(
    (item) =>
      `| ${item.name} | ${item.width} x ${item.height} | PASS | assets/${item.screenshot} |`,
  );
  writeFileSync(
    path.join(evidenceRoot, "2026-07-20-investment-news-hub-viewport-evidence.md"),
    [
      "# Investment News Hub Viewport Evidence",
      "",
      "Generated by the intercepted Chromium Playwright scenario. No live backend, feed, or LLM was contacted.",
      "",
      "| Viewport | Size | Result | Screenshot |",
      "| --- | --- | --- | --- |",
      ...rows,
      "",
      "Checks: document width stayed within the viewport; the responsive track control, refresh button, AI highlights, and first article had non-overlapping bounding boxes.",
      "",
    ].join("\n"),
    "utf8",
  );
});
