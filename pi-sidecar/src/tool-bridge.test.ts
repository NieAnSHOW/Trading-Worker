import { describe, expect, test } from "bun:test";
import { ToolBridge } from "./tool-bridge";

const TOOL = {
  name: "web_search",
  label: "Web Search",
  description: "Search the web",
  parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
  is_readonly: true,
  side_effecting: false,
  repeatable: true,
  timeout_seconds: 1800,
};

const WRITE_TOOL = {
  name: "edit_file",
  label: "Edit File",
  description: "Write a file",
  parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
  is_readonly: false,
  side_effecting: true,
  repeatable: false,
  timeout_seconds: 1800,
};

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

describe("ToolBridge", () => {
  test("setManifest returns count and customTools carry registered names", async () => {
    const bridge = new ToolBridge(async () => ({}));
    expect(await bridge.setManifest([TOOL])).toBe(1);
    const tools = bridge.customTools() as Array<{ name: string; description: string; parameters: unknown }>;
    expect(tools).toHaveLength(1);
    expect(tools[0].name).toBe("web_search");
    expect(tools[0].description).toBe("Search the web");
    expect(tools[0].parameters).toEqual(TOOL.parameters);
  });

  test("execute routes through tool_invoke with idempotency key and returns content", async () => {
    const calls: Array<{ op: string; params: Record<string, unknown> }> = [];
    const bridge = new ToolBridge(async (op, params) => {
      calls.push({ op, params });
      return { content: '{"status":"ok"}', isError: false, outcome: "ok" };
    });
    await bridge.setManifest([TOOL]);
    bridge.onSessionEvent({ type: "message_start", payload: { entryId: "assist1" } });
    const tools = bridge.customTools() as Array<{
      name: string;
      execute: (id: string, params: Record<string, unknown>, signal: AbortSignal | null, onUpdate: unknown, ctx: unknown) => Promise<{ content: Array<{ type: string; text: string }>; details?: unknown }>;
    }>;
    const r = await tools[0].execute("call-1", { query: "aapl" }, null, null, {});
    expect(r.content).toEqual([{ type: "text", text: '{"status":"ok"}' }]);
    expect(calls[0].op).toBe("tool_invoke");
    const p = calls[0].params;
    expect(p.toolName).toBe("web_search");
    expect(p.toolCallId).toBe("call-1");
    expect(p.arguments).toEqual({ query: "aapl" });
    expect(p.idempotencyKey).toBe("unknown:assist1:call-1"); // 无会话上下文时 sessionId=unknown
    expect(p.is_readonly).toBe(true);
    expect(p.side_effecting).toBe(false);
  });

  test("M7a fallback: idempotency key uses turn counter when SDK events carry no entry id", async () => {
    const calls: Array<{ op: string; params: Record<string, unknown> }> = [];
    const bridge = new ToolBridge(async (op, params) => {
      calls.push({ op, params });
      return { content: "{}", isError: false, outcome: "ok" };
    });
    await bridge.setManifest([TOOL]);
    // pinned 18.0.11 的 message_start 不带 entryId（Task 0 实测）；turn_start/turn_end 携带 SDK turnIndex
    bridge.onSessionEvent({ type: "message_start", payload: {} });
    bridge.onSessionEvent({ type: "turn_start", payload: { turnIndex: 2 } });
    bridge.onSessionEvent({ type: "turn_end", payload: { turnIndex: 2 } });
    const tools = bridge.customTools() as Array<{
      execute: (id: string, params: unknown, signal: unknown, onUpdate: unknown, ctx: unknown) => Promise<unknown>;
    }>;
    await tools[0].execute("call-9", { query: "aapl" }, null, null, {});
    expect(calls[0].params.idempotencyKey).toBe("unknown:turn2:call-9");
  });

  test("execute maps thrown host error to isError content", async () => {
    const bridge = new ToolBridge(async () => {
      throw Object.assign(new Error("boom"), { code: "tool_failed" });
    });
    await bridge.setManifest([TOOL]);
    const tools = bridge.customTools() as Array<{ execute: (id: string) => Promise<{ content: Array<{ type: string; text: string }> }> }>;
    const r = await tools[0].execute("call-2", {}, null, null, {});
    expect(r.content[0].text).toContain("boom");
  });

  test("cancel aborts the in-flight execute signal", async () => {
    const d = deferred<unknown>();
    const bridge = new ToolBridge(() => d.promise);
    await bridge.setManifest([TOOL]);
    const tools = bridge.customTools() as Array<{
      execute: (id: string, params: unknown, signal: AbortSignal | null) => Promise<unknown>;
    }>;
    const p = tools[0].execute("call-3", {}, null);
    expect(bridge.cancel("call-3")).toBe(true);
    d.resolve({ content: "x", isError: true, outcome: "error" });
    await p; // 不挂死
  });

  test("cancel unknown call id returns false", async () => {
    const bridge = new ToolBridge(async () => ({}));
    expect(bridge.cancel("nope")).toBe(false);
  });

  test("M8: write-class transport timeout resolves outcome_unknown", async () => {
    const bridge = new ToolBridge(async () => {
      throw Object.assign(new Error("host call tool_invoke timed out"), { code: "timeout" });
    });
    await bridge.setManifest([WRITE_TOOL]);
    const tools = bridge.customTools() as Array<{
      execute: (id: string, params: Record<string, unknown>, signal: AbortSignal | null, onUpdate: unknown, ctx: unknown) => Promise<{ content: Array<{ type: string; text: string }> }>;
    }>;
    const r = await tools[0].execute("call-w1", { path: "a.txt" }, null, null, {});
    const parsed = JSON.parse(r.content[0].text);
    expect(parsed.outcome).toBe("outcome_unknown"); // 写类传输失败绝不伪造结果
    expect(parsed.status).toBe("error");
  });

  test("M8: read-class transport timeout resolves error", async () => {
    const bridge = new ToolBridge(async () => {
      throw Object.assign(new Error("host call tool_invoke timed out"), { code: "timeout" });
    });
    await bridge.setManifest([TOOL]); // 只读
    const tools = bridge.customTools() as Array<{
      execute: (id: string, params: Record<string, unknown>, signal: AbortSignal | null, onUpdate: unknown, ctx: unknown) => Promise<{ content: Array<{ type: string; text: string }> }>;
    }>;
    const r = await tools[0].execute("call-r1", { query: "aapl" }, null, null, {});
    const parsed = JSON.parse(r.content[0].text);
    expect(parsed.outcome).toBe("error"); // 只读 → isError/error，非 outcome_unknown
    expect(parsed.status).toBe("error");
  });

  test("cancel() interrupts in-flight execute and aborts the injected signal", async () => {
    let seenSignal: AbortSignal | null = null;
    let seenTimeoutMs: number | undefined;
    const bridge = new ToolBridge(async (_op, _params, timeoutMs, signal) => {
      seenTimeoutMs = timeoutMs;
      seenSignal = signal ?? null;
      await new Promise<never>((_resolve, reject) => {
        // 注入的 fake hostCall 遵守 signal：中止立即 reject（code:"aborted"），与 HostRpc 一致
        signal?.addEventListener(
          "abort",
          () => reject(Object.assign(new Error("host call aborted"), { code: "aborted" })),
          { once: true },
        );
      });
    });
    await bridge.setManifest([WRITE_TOOL]);
    const tools = bridge.customTools() as Array<{
      execute: (id: string, params: Record<string, unknown>, signal: AbortSignal | null, onUpdate: unknown, ctx: unknown) => Promise<{ content: Array<{ type: string; text: string }> }>;
    }>;
    const p = tools[0].execute("call-w2", { path: "b.txt" }, null, null, {});
    // 信号 + 显式 timeoutMs(≥ 兜底预算) 已透传给传输层
    expect(seenSignal).toBeInstanceOf(AbortSignal);
    expect(seenTimeoutMs).toBeGreaterThanOrEqual(3_600_000);
    expect(seenSignal!.aborted).toBe(false);
    expect(bridge.cancel("call-w2")).toBe(true);
    // 注入的 signal 确实被 abort —— 传输层据此中断 in-flight execute
    expect(seenSignal!.aborted).toBe(true);
    const r = await p; // 不挂死：cancel 中断后立即返回
    const parsed = JSON.parse(r.content[0].text);
    expect(parsed.error).toBe("tool_cancelled"); // 映射 commands.ts 的 cancelled/tool_cancelled 语义
    expect(parsed.outcome).toBe("outcome_unknown");
  });

  test("finding 3: interleaved sessions' tool invokes carry their own idempotency prefix", async () => {
    // final-review finding 3 回归守卫：两个并发会话（4-thread executor）事件交错到达时，
    // 每会话的幂等键前缀必须固定为各自 vibeId，不得被对方的事件改写（否则 Python 侧
    // _handle_tool_invoke_host 按前缀还原 session_id 会把结果落进错误的会话）。
    const calls: string[] = [];
    const bridge = new ToolBridge(async (_op, params) => {
      calls.push(String((params as Record<string, unknown>).idempotencyKey));
      return { content: "{}", isError: false, outcome: "ok" };
    });
    await bridge.setManifest([TOOL]);
    const toolsA = bridge.customTools("sess-A") as Array<{
      execute: (id: string, params: Record<string, unknown>, signal: AbortSignal | null, onUpdate: unknown, ctx: unknown) => Promise<unknown>;
    }>;
    const toolsB = bridge.customTools("sess-B") as Array<{
      execute: (id: string, params: Record<string, unknown>, signal: AbortSignal | null, onUpdate: unknown, ctx: unknown) => Promise<unknown>;
    }>;
    // 交错喂事件：A 起一轮、B 起一轮、A 又起（若为共享单值，B 的上下文会被 A 覆盖）。
    bridge.onSessionEvent({ type: "message_start", payload: { entryId: "eA1" } }, "sess-A");
    bridge.onSessionEvent({ type: "message_start", payload: { entryId: "eB1" } }, "sess-B");
    bridge.onSessionEvent({ type: "message_start", payload: { entryId: "eA2" } }, "sess-A");
    await toolsA[0].execute("callA", { query: "x" }, null, null, {});
    await toolsB[0].execute("callB", { query: "y" }, null, null, {});
    expect(calls).toEqual(["sess-A:eA2:callA", "sess-B:eB1:callB"]);
  });
});
