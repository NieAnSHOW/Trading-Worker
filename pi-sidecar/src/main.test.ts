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

  test("ready is emitted only after extension-load resolves (finding 6 gate)", async () => {
    // 生产 ready-gating 回归守卫：gateReady 下 ready 帧不得先于 vibe-providers/vibe-memory
    // 装载完成发出（否则首个 new_session 撞 "no model"，靠重试自愈）。注入 deferred loader，
    // 断言装载 resolve 前零输出、resolve 后首帧为 ready——无真实计时器，纯微任务拍平驱动。
    const written: string[] = [];
    const gate = Promise.withResolvers<void>();
    const main = createMain({
      driver: makeFakeDriver(),
      extensions: [],
      hostCall: async () => ({}),
      writeOut: (raw: string) => written.push(raw),
      gateReady: true,
      loadExtensions: async () => gate.promise,
    });
    // 装载未完成：ready 被 gate 住，尚无任何输出
    expect(written.length).toBe(0);
    gate.resolve();
    await gate.promise;
    // 拍平 microtask 队列直至 init 续体（emitReady）执行
    for (let i = 0; i < 16 && written.length === 0; i++) await Promise.resolve();
    expect(decodeFrame(written[0])).toMatchObject({ event: "ready", data: { protocol: 1 } });
    main.close();
  });
});
