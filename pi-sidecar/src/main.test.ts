import { describe, expect, test } from "bun:test";
import { createMain, type MainDeps } from "./main";
import { decodeFrame } from "./protocol";
import type { SessionDriver, SessionLike } from "./commands";

function makeFakeSession(): SessionLike {
  return {
    sessionId: "a1b2c3d4e5f6",
    sessionFile: "/tmp/fake.jsonl",
    isBusy: () => false,
    subscribe: () => () => {},
    prompt: async () => {},
    steer: () => {},
    followUp: () => {},
    abort: () => {},
    setModel: async () => {},
    setThinkingLevel: async () => {},
    compact: async () => {},
    navigateTree: async () => "leaf1",
    getMessages: () => ({ messages: [], nextCursor: null }),
    exportTo: async () => {},
    importMessages: async () => 0,
  };
}

function makeFakeDriver(): SessionDriver {
  const s = makeFakeSession();
  return {
    open: async () => s,
    create: async () => s,
    get: () => s,
    setToolManifest: async (tools) => (tools as unknown[]).length,
    cancelToolCall: () => true,
  };
}

function harness() {
  const written: string[] = [];
  const deps: Partial<MainDeps> = {
    driver: makeFakeDriver(),
    hostCall: async (_op, params) => ({ content: '{"ok":true}', isError: false, outcome: "ok", echoed: params }),
    writeOut: (raw: string) => written.push(raw),
    extensions: [],
  };
  const main = createMain(deps);
  return { main, written };
}

describe("main entrypoint", () => {
  test("emits ready frame first", () => {
    const { written } = harness();
    const ready = decodeFrame(written[0]);
    expect(ready).toMatchObject({ event: "ready", data: { protocol: 1 } });
    expect((ready as { data: { sdk: string } }).data.sdk).toBe("18.0.11");
  });

  test("ping roundtrip over in-memory duplex", async () => {
    const { main, written } = harness();
    await main.handleLine('{"v":1,"id":"r-1","op":"ping"}\n');
    expect(decodeFrame(written[1])).toMatchObject({ id: "r-1", ok: true, result: { pong: true, protocol: 1 } });
  });

  test("__VIBE_HOST__ bridge set before extensions and routes hostCall", async () => {
    const { main, written } = harness();
    expect((globalThis as Record<string, unknown>).__VIBE_HOST__).toBeDefined();
    const host = (globalThis as Record<string, unknown>).__VIBE_HOST__ as { call: (op: string, p?: Record<string, unknown>) => Promise<unknown> };
    const r = (await host.call("tool_invoke", { call_id: "t-1" })) as Record<string, unknown>;
    expect(r["echoed"]).toMatchObject({ call_id: "t-1" }); // hostCall 经 HostRpc 关联（测试注入直连）
    main.close();
  });

  test("inbound response frames resolve pending hostCall (duplex dispatch)", async () => {
    // HostRpc 响应派发：注入一个手工 HostRpc 响应帧，验证 handleLine 把它喂给挂起的 call
    const { main } = harness();
    const pending = (globalThis as Record<string, unknown>).__VIBE_HOST__ as { call: (op: string) => Promise<unknown> };
    const p = pending.call("memory_context", { query: "q" });
    await main.handleLine('{"v":1,"id":"s-1","ok":true,"result":{"block":"B"}}\n');
    expect(await p).toEqual({ block: "B" });
    main.close();
  });
});
