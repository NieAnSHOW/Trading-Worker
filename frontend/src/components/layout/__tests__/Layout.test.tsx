import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import i18n from "@/i18n";
import { Layout } from "../Layout";

vi.mock("@/lib/api", () => ({
  api: {
    listSessions: vi.fn().mockResolvedValue([]),
    deleteSession: vi.fn().mockResolvedValue(undefined),
    renameSession: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn().mockResolvedValue(undefined),
}));

function renderLayout() {
  i18n.changeLanguage("zh-CN");
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<div>Dashboard route content</div>} />
          <Route path="/agent" element={<div>Agent route content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Layout sidebar", () => {
  it("renders the sidebar with nav links and route outlet", () => {
    renderLayout();

    expect(screen.getByRole("link", { name: /首页/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /指数/i })).toHaveAttribute("href", "/indices");
    expect(screen.getByText("Dashboard route content")).toBeInTheDocument();
  });

  it("places Market Pulse directly after Dashboard and before Indices", () => {
    renderLayout();

    const dashboard = screen.getByRole("link", { name: /市场看板/i });
    const marketPulse = screen.getByRole("link", { name: /市场异动/i });
    const indices = screen.getByRole("link", { name: /指数/i });

    expect(marketPulse).toHaveAttribute("href", "/market-pulse");
    expect(
      Boolean(
        dashboard.compareDocumentPosition(marketPulse) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true);
    expect(
      Boolean(
        marketPulse.compareDocumentPosition(indices) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ),
    ).toBe(true);
  });

  it("keeps the main area constrained", () => {
    const { container } = renderLayout();

    expect(container.querySelector('[data-testid="web-ui-main"]')).toHaveClass("min-h-0", "min-w-0", "overflow-hidden");
    expect(container.querySelector('[data-testid="web-ui-outlet"]')).toHaveClass("min-h-0", "min-w-0", "overflow-auto");
  });

  it("renders external shortcut buttons when the group is expanded", async () => {
    const user = userEvent.setup();
    renderLayout();

    await user.click(screen.getByRole("button", { name: /快捷入口/i }));

    expect(screen.getByRole("button", { name: /同花顺/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /腾讯财经/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /东方财富/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /新浪财经/i })).toBeInTheDocument();
  });

  it("opens external shortcut URL through the desktop shell", async () => {
    const user = userEvent.setup();
    renderLayout();

    await user.click(screen.getByRole("button", { name: /快捷入口/i }));
    await user.click(screen.getByRole("button", { name: /同花顺/i }));

    const { invoke } = await import("@tauri-apps/api/core");
    expect(invoke).toHaveBeenCalledWith("open_external_url", {
      url: "https://www.10jqka.com.cn/",
    });
  });
});
