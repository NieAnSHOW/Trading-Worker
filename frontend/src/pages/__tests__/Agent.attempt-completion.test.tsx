import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAgentStore } from "@/stores/agent";

const { apiMock, connectMock, disconnectMock, onStatusChangeMock, handlersRef } = vi.hoisted(() => ({
  apiMock: {
    getSessionMessages: vi.fn(),
    getRun: vi.fn(),
    getGoal: vi.fn(),
    getLiveStatus: vi.fn(),
    getLLMSettings: vi.fn(),
    sseUrl: vi.fn(),
  },
  connectMock: vi.fn(),
  disconnectMock: vi.fn(),
  onStatusChangeMock: vi.fn(),
  handlersRef: { current: {} as Record<string, (data: Record<string, unknown>) => void> },
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: "en" } }),
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), info: vi.fn(), success: vi.fn(), warning: vi.fn() } }));
vi.mock("@/lib/telemetry", () => ({ track: vi.fn() }));
vi.mock("@/lib/api", () => ({
  api: apiMock,
  ApiError: class ApiError extends Error { status = 0; },
  AUTH_REQUIRED_MESSAGE: "auth required",
  isAuthRequiredError: () => false,
}));
vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    connect: connectMock.mockImplementation((_url: string, handlers: Record<string, (data: Record<string, unknown>) => void>) => {
      handlersRef.current = handlers;
    }),
    disconnect: disconnectMock,
    onStatusChange: onStatusChangeMock,
  }),
}));
vi.mock("@/components/chat/AgentAvatar", () => ({ AgentAvatar: () => <div /> }));
vi.mock("@/components/chat/WelcomeScreen", () => ({ WelcomeScreen: () => <div /> }));
vi.mock("@/components/chat/MessageBubble", () => ({ MessageBubble: () => <div /> }));
vi.mock("@/components/chat/ThinkingTimeline", () => ({ ThinkingTimeline: () => <div /> }));
vi.mock("@/components/chat/ConversationTimeline", () => ({ ConversationTimeline: () => <div /> }));
vi.mock("@/components/chat/ToolProgressIndicator", () => ({ ToolProgressIndicator: () => <div /> }));
vi.mock("@/components/chat/MandateProposalCard", () => ({ MandateProposalCard: () => <div /> }));
vi.mock("@/components/chat/RunnerStatus", () => ({ RunnerStatus: () => <div /> }));
vi.mock("@/components/chat/SwarmStatusCard", () => ({ SwarmStatusCard: () => <div /> }));
vi.mock("@/components/chat/LLMUsagePanel", () => ({
  LLMUsagePanel: ({ usage }: { usage: { totals: { input_tokens: number } } }) => (
    <div data-testid="live-usage">{usage.totals.input_tokens}</div>
  ),
}));

import { Agent } from "../Agent";

describe("Agent attempt completion", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAgentStore.getState().reset();
    useAgentStore.getState().setSessionId("session-a");
    apiMock.getSessionMessages.mockResolvedValue([]);
    apiMock.getGoal.mockResolvedValue(null);
    apiMock.getLiveStatus.mockResolvedValue({ global_halted: false, brokers: [] });
    apiMock.getLLMSettings.mockResolvedValue({ sse_timeout_seconds: 90 });
    apiMock.sseUrl.mockReturnValue("/sessions/session-a/events");
  });

  it("keeps the current attempt live usage when an earlier completion fetch resolves", async () => {
    let resolveRun!: (run: Record<string, unknown>) => void;
    apiMock.getRun.mockReturnValue(new Promise((resolve) => { resolveRun = resolve; }));

    render(
      <MemoryRouter initialEntries={["/agent?session=session-a"]}>
        <Routes><Route path="/agent" element={<Agent />} /></Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(connectMock).toHaveBeenCalled());

    await act(async () => {
      handlersRef.current["attempt.created"]({ attempt_id: "attempt-a" });
      handlersRef.current.llm_usage({
        iter: 1, provider: "provider", model: null, metering_eligible: false,
        input_tokens: 1, output_tokens: 0, total_tokens: 1,
      });
    });
    expect(screen.getByTestId("live-usage")).toHaveTextContent("1");

    act(() => {
      void handlersRef.current["attempt.completed"]({
        attempt_id: "attempt-a",
        run_dir: "/runs/run-a",
        summary: "A complete",
      });
    });
    await waitFor(() => expect(apiMock.getRun).toHaveBeenCalledWith("run-a"));

    await act(async () => {
      handlersRef.current["attempt.created"]({ attempt_id: "attempt-b" });
      handlersRef.current.llm_usage({
        iter: 2, provider: "provider", model: null, metering_eligible: false,
        input_tokens: 2, output_tokens: 0, total_tokens: 2,
      });
    });
    expect(screen.getByTestId("live-usage")).toHaveTextContent("2");

    await act(async () => {
      resolveRun({ metrics: {}, equity_curve: [], artifacts: [], llm_usage: null });
      await Promise.resolve();
    });

    expect(screen.getByTestId("live-usage")).toHaveTextContent("2");
  });

  it("keeps the current attempt live usage when an earlier completion fetch fails", async () => {
    let rejectRun!: (error: Error) => void;
    apiMock.getRun.mockReturnValue(new Promise((_resolve, reject) => { rejectRun = reject; }));

    render(
      <MemoryRouter initialEntries={["/agent?session=session-a"]}>
        <Routes><Route path="/agent" element={<Agent />} /></Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(connectMock).toHaveBeenCalled());

    await act(async () => {
      handlersRef.current["attempt.created"]({ attempt_id: "attempt-a" });
      handlersRef.current.llm_usage({
        iter: 1, provider: "provider", model: null, metering_eligible: false,
        input_tokens: 1, output_tokens: 0, total_tokens: 1,
      });
    });

    act(() => {
      void handlersRef.current["attempt.completed"]({
        attempt_id: "attempt-a",
        run_dir: "/runs/run-a",
        summary: "A complete",
      });
    });
    await waitFor(() => expect(apiMock.getRun).toHaveBeenCalledWith("run-a"));

    await act(async () => {
      handlersRef.current["attempt.created"]({ attempt_id: "attempt-b" });
      handlersRef.current.llm_usage({
        iter: 2, provider: "provider", model: null, metering_eligible: false,
        input_tokens: 2, output_tokens: 0, total_tokens: 2,
      });
    });

    await act(async () => {
      rejectRun(new Error("run unavailable"));
      await Promise.resolve();
    });

    expect(screen.getByTestId("live-usage")).toHaveTextContent("2");
  });
});
