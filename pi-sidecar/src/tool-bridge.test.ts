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
});
