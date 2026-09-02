import { describe, expect, test } from "bun:test";

type Handler = (event: unknown, ctx: unknown) => Promise<unknown>;
interface FakePi {
  handlers: Record<string, Handler>;
  tools: Array<{ name: string; execute: (id: string, params: Record<string, unknown>) => Promise<{ content: Array<{ type: string; text: string }> }> }>;
  on(event: string, handler: Handler): void;
  registerTool(t: { name: string; execute: (id: string, params: Record<string, unknown>) => Promise<{ content: Array<{ type: string; text: string }> }> }): void;
}

function makeFakePi(): FakePi {
  const pi: FakePi = { handlers: {}, tools: [], on(e, h) { pi.handlers[e] = h; }, registerTool(t) { pi.tools.push(t); } };
  return pi;
}

function makeHost(responses: Record<string, unknown>, fail = false) {
  const calls: Array<{ op: string; params?: Record<string, unknown> }> = [];
  return {
    calls,
    call: async (op: string, params?: Record<string, unknown>) => {
      calls.push({ op, params });
      if (fail) throw new Error("memory down");
      return responses[op];
    },
    emitDiagnostic: (kind: string, msg: string) => diagnostics.push([kind, msg]),
  };
}
const diagnostics: Array<[string, string]> = [];

describe("vibe-memory extension", () => {
  test("registers three compat tools", async () => {
    (globalThis as Record<string, unknown>).__VIBE_HOST__ = makeHost({});
    const mod = await import("./index");
    const pi = makeFakePi();
    mod.default(pi as never);
    const names = pi.tools.map((t) => t.name).sort();
    expect(names).toEqual(["memory_remove", "memory_search", "remember"]);
  });

  test("remember tool calls host and returns text", async () => {
    (globalThis as Record<string, unknown>).__VIBE_HOST__ = makeHost({ remember: { ok: true } });
    const mod = await import("./index");
    const pi = makeFakePi();
    mod.default(pi as never);
    const remember = pi.tools.find((t) => t.name === "remember")!;
    const r = await remember.execute("id1", { name: "n", content: "c" });
    expect(r.content[0].text).toContain("ok");
  });

  test("input event captured and passed as memory_context query", async () => {
    diagnostics.length = 0;
    const h = makeHost({ memory_context: { block: "B" } });
    (globalThis as Record<string, unknown>).__VIBE_HOST__ = h;
    const mod = await import("./index");
    const pi = makeFakePi();
    mod.default(pi as never);
    await pi.handlers["input"]({ text: "research AAPL position limits" }, {});
    await pi.handlers["before_agent_start"]({}, {});
    const mc = h.calls.find((c) => c.op === "memory_context");
    expect(mc).toBeDefined();
    expect(mc!.params).toEqual({ query: "research AAPL position limits" });
  });

  test("before_agent_start failure degrades with diagnostic, turn continues", async () => {
    diagnostics.length = 0;
    (globalThis as Record<string, unknown>).__VIBE_HOST__ = makeHost({}, true);
    const mod = await import("./index");
    const pi = makeFakePi();
    mod.default(pi as never);
    await pi.handlers["input"]({ text: "q" }, {});
    await pi.handlers["before_agent_start"]({}, {});
    expect(diagnostics.some(([k]) => k === "memory_degraded")).toBe(true);
    // 后续 provider request 不注入任何 block
    let seen = "";
    await pi.handlers["before_provider_request"](
      { systemPrompt: "BASE", revise: (s: unknown) => { seen = JSON.stringify(s); return {}; } },
      {},
    );
    expect(seen).not.toContain("MEMORY");
  });

  test("memory block injected into first provider request of a turn only", async () => {
    diagnostics.length = 0;
    (globalThis as Record<string, unknown>).__VIBE_HOST__ = makeHost({ memory_context: { block: "## relevant\n- risk-rules" } });
    const mod = await import("./index");
    const pi = makeFakePi();
    mod.default(pi as never);
    let first = "";
    let second = "";
    const revise = (tag: string) => (req: { systemPrompt: string }) => {
      if (tag === "1") first = req.systemPrompt;
      else second = req.systemPrompt;
      return {};
    };
    await pi.handlers["input"]({ text: "position limits" }, {});
    await pi.handlers["before_agent_start"]({}, {});
    await pi.handlers["before_provider_request"]({ systemPrompt: "BASE", revise: revise("1") }, {});
    await pi.handlers["before_provider_request"]({ systemPrompt: "BASE", revise: revise("2") }, {});
    expect(first).toContain("risk-rules");   // 第一请求注入
    expect(second).not.toContain("risk-rules"); // 同轮后续请求不重复注入
  });
});
