import { renderHook, act } from "@testing-library/react";
import { useSSE } from "../useSSE";

const { withAuthTicketMock } = vi.hoisted(() => ({ withAuthTicketMock: vi.fn() }));

vi.mock("@/lib/apiAuth", () => ({ withAuthTicket: withAuthTicketMock }));

// ── Mock EventSource ──────────────────────────────────────

type ESHandler = (e: MessageEvent) => void;

class MockEventSource {
  url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, ESHandler[]>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: ESHandler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type)!.push(handler);
  }

  /** Test helper: simulate an event from the server */
  emit(type: string, data: unknown, lastEventId?: string) {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    Object.defineProperty(event, "lastEventId", { value: lastEventId || "" });
    const handlers = this.listeners.get(type) || [];
    handlers.forEach((h) => h(event));
  }

  close() {
    this.listeners.clear();
  }

  // ── Static helpers ──
  static instances: MockEventSource[] = [];

  static reset() {
    this.instances = [];
  }

  static get latest(): MockEventSource {
    return this.instances[this.instances.length - 1];
  }
}

beforeEach(() => {
  MockEventSource.reset();
  vi.stubGlobal("EventSource", MockEventSource);
  vi.useFakeTimers();
  withAuthTicketMock.mockReset();
  withAuthTicketMock.mockImplementation(async (url: string) => url);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function flushTicketRequest() {
  await act(async () => {
    await Promise.resolve();
  });
}

// ── Tests ─────────────────────────────────────────────────

describe("useSSE — connect/disconnect", () => {
  it("creates an EventSource on connect", async () => {
    const { result } = renderHook(() => useSSE());
    act(() => result.current.connect("http://test/events", {}));
    await flushTicketRequest();
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.latest.url).toBe("http://test/events");
  });

  it("mints a fresh ticket for each reconnect", async () => {
    withAuthTicketMock
      .mockResolvedValueOnce("http://test/events?ticket=first")
      .mockResolvedValueOnce("http://test/events?ticket=second");
    const { result } = renderHook(() =>
      useSSE({ initialRetryMs: 100, maxRetryMs: 100, backoffFactor: 2 }),
    );

    act(() => result.current.connect("http://test/events", {}));
    await flushTicketRequest();
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.latest.url).toBe("http://test/events?ticket=first");

    act(() => MockEventSource.latest.onerror?.());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(MockEventSource.instances).toHaveLength(2);
    expect(MockEventSource.latest.url).toBe("http://test/events?ticket=second");
    expect(withAuthTicketMock).toHaveBeenCalledTimes(2);
  });

  it("retries with a fresh ticket after ticket minting fails", async () => {
    withAuthTicketMock
      .mockRejectedValueOnce(new Error("ticket denied"))
      .mockResolvedValueOnce("http://test/events?ticket=fresh");
    const { result } = renderHook(() =>
      useSSE({ initialRetryMs: 100, maxRetryMs: 100, backoffFactor: 2 }),
    );

    act(() => result.current.connect("http://test/events", {}));
    await flushTicketRequest();
    expect(MockEventSource.instances).toHaveLength(0);
    expect(result.current.getStatus()).toBe("reconnecting");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(MockEventSource.latest.url).toBe("http://test/events?ticket=fresh");
  });

  it("closes previous EventSource on reconnect", async () => {
    const { result } = renderHook(() => useSSE());
    const closeSpy = vi.fn();

    act(() => result.current.connect("http://test/events", {}));
    await flushTicketRequest();
    MockEventSource.latest.close = closeSpy;

    act(() => result.current.connect("http://test/events2", {}));
    expect(closeSpy).toHaveBeenCalled();
  });

  it("does not attach a ticket resolved after disconnect", async () => {
    let resolveTicket!: (url: string) => void;
    withAuthTicketMock.mockReturnValueOnce(new Promise<string>((resolve) => {
      resolveTicket = resolve;
    }));
    const { result } = renderHook(() => useSSE());

    act(() => result.current.connect("http://test/events", {}));
    act(() => result.current.disconnect());
    await act(async () => {
      resolveTicket("http://test/events?ticket=late");
      await Promise.resolve();
    });

    expect(MockEventSource.instances).toHaveLength(0);
  });

  it("sets status to connected on open", async () => {
    const statuses: string[] = [];
    const { result } = renderHook(() => useSSE());

    act(() => {
      result.current.onStatusChange((s) => statuses.push(s));
      result.current.connect("http://test/events", {});
    });
    await flushTicketRequest();

    act(() => MockEventSource.latest.onopen?.());
    expect(statuses).toContain("connected");
    expect(result.current.getStatus()).toBe("connected");
  });

  it("disconnect closes source and sets disconnected status", () => {
    const statuses: string[] = [];
    const { result } = renderHook(() => useSSE());

    act(() => {
      result.current.onStatusChange((s) => statuses.push(s));
      result.current.connect("http://test/events", {});
    });

    act(() => result.current.disconnect());
    expect(statuses).toContain("disconnected");
    expect(result.current.getStatus()).toBe("disconnected");
  });
});

describe("useSSE — event handling", () => {
  it("dispatches typed events to handlers", async () => {
    const textDeltas: unknown[] = [];
    const { result } = renderHook(() => useSSE());

    act(() =>
      result.current.connect("http://test/events", {
        text_delta: (data) => textDeltas.push(data),
      }),
    );
    await flushTicketRequest();

    act(() => MockEventSource.latest.emit("text_delta", { content: "hello" }));
    expect(textDeltas).toHaveLength(1);
    expect(textDeltas[0]).toEqual({ content: "hello" });
  });

  it("dispatches llm usage events to the dedicated handler", async () => {
    const events: unknown[] = [];
    const { result } = renderHook(() => useSSE());

    act(() =>
      result.current.connect("http://test/events", {
        llm_usage: (data) => events.push(data),
      }),
    );
    await flushTicketRequest();

    act(() => MockEventSource.latest.emit("llm_usage", { iter: 1, input_tokens: 1 }, "usage-1"));
    expect(events).toEqual([{ iter: 1, input_tokens: 1 }]);
  });

  it("dispatches reasoning progress events", async () => {
    const reasoningEvents: unknown[] = [];
    const { result } = renderHook(() => useSSE());

    act(() =>
      result.current.connect("http://test/events", {
        reasoning_delta: (data) => reasoningEvents.push(data),
      }),
    );
    await flushTicketRequest();

    act(() => MockEventSource.latest.emit("reasoning_delta", { chars: 8 }, "evt-reasoning"));
    expect(reasoningEvents).toEqual([{ chars: 8 }]);
  });

  it("dispatches stream reset events", async () => {
    const resetEvents: unknown[] = [];
    const { result } = renderHook(() => useSSE());

    act(() =>
      result.current.connect("http://test/events", {
        stream_reset: (data) => resetEvents.push(data),
      }),
    );
    await flushTicketRequest();

    act(() =>
      MockEventSource.latest.emit(
        "stream_reset",
        { reason: "provider_stream_retry" },
        "evt-reset",
      ),
    );
    expect(resetEvents).toEqual([{ reason: "provider_stream_retry" }]);
  });

  it("falls back to message handler for known event types without specific handler", async () => {
    const messages: unknown[] = [];
    const { result } = renderHook(() => useSSE());

    act(() =>
      result.current.connect("http://test/events", {
        message: (data) => messages.push(data),
      }),
    );
    await flushTicketRequest();

    // "text_delta" is a known type the hook subscribes to,
    // but no specific handler → falls back to "message"
    act(() => MockEventSource.latest.emit("text_delta", { foo: "bar" }, "evt-1"));
    expect(messages).toHaveLength(1);
  });

  it("handles malformed JSON gracefully", async () => {
    const messages: unknown[] = [];
    const { result } = renderHook(() => useSSE());

    act(() =>
      result.current.connect("http://test/events", {
        text_delta: (data) => messages.push(data),
      }),
    );
    await flushTicketRequest();

    // Emit with raw non-JSON data
    const event = new MessageEvent("text_delta", { data: "not json" });
    Object.defineProperty(event, "lastEventId", { value: "evt-x" });
    const handlers = (MockEventSource.latest as any).listeners.get("text_delta");
    act(() => handlers?.forEach((h: ESHandler) => h(event)));

    expect(messages).toHaveLength(1);
    expect(messages[0]).toEqual({ raw: "not json" });
  });
});

describe("useSSE — deduplication", () => {
  it("deduplicates events by lastEventId", async () => {
    const messages: unknown[] = [];
    const { result } = renderHook(() => useSSE({ dedupeCapacity: 10 }));

    act(() =>
      result.current.connect("http://test/events", {
        text_delta: (data) => messages.push(data),
      }),
    );
    await flushTicketRequest();

    act(() => MockEventSource.latest.emit("text_delta", { n: 1 }, "dup-1"));
    act(() => MockEventSource.latest.emit("text_delta", { n: 2 }, "dup-1")); // duplicate
    act(() => MockEventSource.latest.emit("text_delta", { n: 3 }, "dup-2"));

    expect(messages).toHaveLength(2);
    expect(messages[0]).toEqual({ n: 1 });
    expect(messages[1]).toEqual({ n: 3 });
  });

  it("evicts oldest events when dedup capacity exceeded", async () => {
    const messages: unknown[] = [];
    const { result } = renderHook(() => useSSE({ dedupeCapacity: 3 }));

    act(() =>
      result.current.connect("http://test/events", {
        text_delta: (data) => messages.push(data),
      }),
    );
    await flushTicketRequest();

    // Fill dedup set to capacity (3)
    act(() => MockEventSource.latest.emit("text_delta", { n: 1 }, "a"));
    act(() => MockEventSource.latest.emit("text_delta", { n: 2 }, "b"));
    act(() => MockEventSource.latest.emit("text_delta", { n: 3 }, "c"));

    // Duplicate: should be suppressed
    act(() => MockEventSource.latest.emit("text_delta", { n: 10 }, "a"));
    expect(messages).toHaveLength(3);

    // Adding 4th event → "a" gets evicted → re-emit "a" should pass through
    act(() => MockEventSource.latest.emit("text_delta", { n: 4 }, "d"));
    expect(messages).toHaveLength(4);
    act(() => MockEventSource.latest.emit("text_delta", { n: 5 }, "a"));
    expect(messages).toHaveLength(5);
  });
});

describe("useSSE — exponential backoff", () => {
  it("schedules reconnect with exponential delay on error", async () => {
    const reconnects: unknown[] = [];
    const { result } = renderHook(() =>
      useSSE({ initialRetryMs: 100, maxRetryMs: 5000, backoffFactor: 2 }),
    );

    act(() =>
      result.current.connect("http://test/events", {
        reconnect: (data) => reconnects.push(data),
      }),
    );
    await flushTicketRequest();

    // Trigger error → should schedule reconnect
    act(() => MockEventSource.latest.onerror?.());

    expect(reconnects).toHaveLength(1);
    expect(reconnects[0]).toEqual({ attempt: 1, delayMs: 100 });
    expect(result.current.getStatus()).toBe("reconnecting");

    // Advance timer past the delay → should create a new EventSource
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(MockEventSource.instances.length).toBeGreaterThan(1);
  });

  it("caps retry delay at maxRetryMs", async () => {
    const reconnects: unknown[] = [];
    const { result } = renderHook(() =>
      useSSE({ initialRetryMs: 100, maxRetryMs: 200, backoffFactor: 10 }),
    );

    act(() =>
      result.current.connect("http://test/events", {
        reconnect: (data) => reconnects.push(data),
      }),
    );
    await flushTicketRequest();

    act(() => MockEventSource.latest.onerror?.());
    expect((reconnects[0] as any).delayMs).toBe(100);

    // Trigger another error on the new source
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    act(() => MockEventSource.latest.onerror?.());
    // backoff: 100 * 10^1 = 1000, but capped at 200
    expect((reconnects[1] as any).delayMs).toBe(200);
  });
});

describe("useSSE — Last-Event-ID resume", () => {
  it("appends Last-Event-ID to reconnect URL", async () => {
    const { result } = renderHook(() => useSSE());

    act(() =>
      result.current.connect("http://test/events", {
        text_delta: () => {},
      }),
    );
    await flushTicketRequest();

    // Emit an event with lastEventId
    act(() => MockEventSource.latest.emit("text_delta", { n: 1 }, "resume-42"));

    // Trigger reconnect
    act(() => MockEventSource.latest.onerror?.());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    // The new EventSource should have Last-Event-ID in the URL
    const newUrl = MockEventSource.latest.url;
    expect(newUrl).toContain("Last-Event-ID=resume-42");
  });
});
