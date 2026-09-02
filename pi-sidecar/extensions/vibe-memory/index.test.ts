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

  // ---- 生产注入路径（真实事件形状）----
  // 真实 SDK：{ type:"before_provider_request", payload:<请求体> }，handler 返回整体替换请求（runner: if(l!==void 0) s=l）。
  // openai 请求体 = { model, messages }；anthropic = { model, system, messages }。
  function setupTurn(block: string) {
    diagnostics.length = 0;
    (globalThis as Record<string, unknown>).__VIBE_HOST__ = makeHost({ memory_context: { block } });
    return (async () => {
      const mod = await import("./index");
      const pi = makeFakePi();
      mod.default(pi as never);
      await pi.handlers["input"]({ text: "q" }, {});
      await pi.handlers["before_agent_start"]({}, {});
      return pi.handlers["before_provider_request"];
    })();
  }

  test("production: openai dialect appends memory to system message (messages[0])", async () => {
    const fire = await setupTurn("## memory\n- aaple rules");
    const req = { model: "gpt-x", messages: [{ role: "system", content: "BASE" }, { role: "user", content: "hi" }] };
    const out = await fire({ type: "before_provider_request", payload: req }, {});
    expect(out).toBeDefined();
    const messages = (out as { messages: Array<{ role: string; content: string }> }).messages;
    expect(messages[0].role).toBe("system");
    expect(messages[0].content).toBe("BASE\n\n## memory\n- aaple rules");
    expect(messages[1].content).toBe("hi"); // 用户消息未被改动
    // 不改动调用方数组（非变异契约）
    expect(req.messages[0].content).toBe("BASE");
  });

  test("production: openai dialect unshifts system message when none present", async () => {
    const fire = await setupTurn("M");
    const out = await fire(
      { type: "before_provider_request", payload: { model: "gpt-x", messages: [{ role: "user", content: "hi" }] } },
      {},
    );
    const messages = (out as { messages: Array<{ role: string; content: string }> }).messages;
    expect(messages[0]).toEqual({ role: "system", content: "M" });
    expect(messages[1].content).toBe("hi");
  });

  test("production: anthropic dialect sets the top-level system field", async () => {
    const fire = await setupTurn("## mem\n- rules");
    const req = { model: "claude-x", system: "BASE", messages: [{ role: "user", content: "hi" }] };
    const out = await fire({ type: "before_provider_request", payload: req }, {});
    expect(out).toBeDefined();
    const o = out as { system: string; messages: Array<{ role: string }> };
    expect(o.system).toBe("BASE\n\n## mem\n- rules");
    expect(o.messages).toEqual([{ role: "user", content: "hi" }]); // 会话消息不被注入
  });

  test("production: only the first provider request of a turn is injected", async () => {
    const fire = await setupTurn("MEM");
    const make = () => ({ type: "before_provider_request", payload: { model: "gpt-x", messages: [{ role: "user", content: "hi" }] } });
    const first = await fire(make(), {});
    const second = await fire(make(), {});
    const firstMsg = (first as { messages: Array<{ role: string; content: string }> }).messages;
    expect(firstMsg[0].role).toBe("system");
    expect(firstMsg[0].content).toBe("MEM");
    expect(second).toBeUndefined(); // 同轮第二请求原样返回（runner: void 0 → 不替换）
  });

  test("production: empty memory block is a no-op (request unchanged)", async () => {
    const fire = await setupTurn("");
    const req = { model: "gpt-x", messages: [{ role: "system", content: "BASE" }] };
    const out = await fire({ type: "before_provider_request", payload: req }, {});
    expect(out).toBeUndefined();
  });
});
