import { describe, expect, test } from "bun:test";
import { createCommandHandler, type SessionDriver, type SessionLike } from "./commands";

function makeFakeSession(over: Partial<SessionLike> = {}): SessionLike {
  return {
    sessionId: "a1b2c3d4e5f6",
    sessionFile: "/tmp/s.jsonl",
    isBusy: () => false,
    subscribe: () => () => {},
    prompt: async () => {},
    steer: () => {},
    followUp: () => {},
    abort: () => {},
    setModel: async () => {},
    setThinkingLevel: async () => {},
    compact: async () => {},
    navigateTree: async () => "leaf123",
    getMessages: () => ({ messages: [{ entry_id: "e1", role: "user", content: "hi", timestamp: "t" }], nextCursor: null }),
    exportTo: async () => {},
    importMessages: async () => 2,
    ...over,
  };
}

function makeDriver(over: Partial<SessionDriver> = {}): SessionDriver {
  const sessions = new Map<string, SessionLike>();
  return {
    open: async (id) => {
      const s = sessions.get(id);
      if (!s) throw Object.assign(new Error("not_found"), { code: "not_found" });
      return s;
    },
    create: async (id) => {
      const s = makeFakeSession({ sessionId: id });
      sessions.set(id, s);
      return s;
    },
    get: (id) => sessions.get(id),
    setToolManifest: async (tools) => (tools as unknown[]).length,
    cancelToolCall: () => true,
    ...over,
  };
}

function harness(driver = makeDriver()) {
  const out: string[] = [];
  const handler = createCommandHandler({
    driver,
    sink: (raw) => out.push(raw),
    hostCall: async () => ({}),
  });
  // send 直接透传字符串（原始行），对象才 JSON.stringify——否则 "{nope" 会被序列化成合法 JSON 字符串
  return { driver, out, send: (obj: unknown) => handler(typeof obj === "string" ? obj : JSON.stringify(obj)) };
}

const req = (id: string, op: string, params?: Record<string, unknown>) => ({ v: 1, id, op, params });

describe("command handler", () => {
  test("ping", async () => {
    const h = harness();
    await h.send(req("r-1", "ping"));
    expect(JSON.parse(h.out[0])).toMatchObject({ id: "r-1", ok: true, result: { pong: true, protocol: 1 } });
  });

  test("unknown op", async () => {
    const h = harness();
    await h.send(req("r-2", "frobnicate"));
    expect(JSON.parse(h.out[0])).toMatchObject({ id: "r-2", ok: false, error: { code: "unknown_op" } });
  });

  test("bad json -> error frame, handler does not throw", async () => {
    const h = harness();
    await h.send("{nope");
    expect(JSON.parse(h.out[0])).toMatchObject({ ok: false, error: { code: "bad_json" } });
  });

  test("new_session then prompt ack, state, abort", async () => {
    const h = harness();
    await h.send(req("r-3", "new_session", { session_id: "a1b2c3d4e5f6" }));
    expect(JSON.parse(h.out[0])).toMatchObject({ id: "r-3", ok: true, result: { session_file: expect.any(String) } });
    await h.send(req("r-4", "prompt", { session_id: "a1b2c3d4e5f6", text: "hi" }));
    expect(JSON.parse(h.out[1])).toMatchObject({ id: "r-4", ok: true, result: { accepted: true } });
    await h.send(req("r-5", "get_state", { session_id: "a1b2c3d4e5f6" }));
    expect(JSON.parse(h.out[2])).toMatchObject({ id: "r-5", ok: true, result: { busy: false } });
    await h.send(req("r-6", "abort", { session_id: "a1b2c3d4e5f6" }));
    expect(JSON.parse(h.out[3])).toMatchObject({ id: "r-6", ok: true, result: { aborted: true } });
  });

  test("open_session missing -> not_found", async () => {
    const h = harness();
    await h.send(req("r-7", "open_session", { session_id: "a1b2c3d4e5f6" }));
    expect(JSON.parse(h.out[0])).toMatchObject({ id: "r-7", ok: false, error: { code: "not_found" } });
  });

  test("prompt without session -> no_session; prompt while busy -> session_busy", async () => {
    const h = harness();
    await h.send(req("r-8", "prompt", { session_id: "000000000000", text: "x" }));
    expect(JSON.parse(h.out[0])).toMatchObject({ ok: false, error: { code: "no_session" } });

    const busy = makeFakeSession({ isBusy: () => true });
    const d = makeDriver({ open: async () => busy, get: () => busy });
    const h2 = harness(d);
    await h2.send(req("r-9", "prompt", { session_id: "a1b2c3d4e5f6", text: "x" }));
    expect(JSON.parse(h2.out[0])).toMatchObject({ ok: false, error: { code: "session_busy" } });
  });

  test("steer/follow_up/compact/set_model/set_thinking_level/navigate/export", async () => {
    const h = harness();
    await h.send(req("r-10", "new_session", { session_id: "a1b2c3d4e5f6" }));
    await h.send(req("r-11", "steer", { session_id: "a1b2c3d4e5f6", text: "s" }));
    expect(JSON.parse(h.out[1])).toMatchObject({ ok: true, result: { queued: true } });
    await h.send(req("r-12", "follow_up", { session_id: "a1b2c3d4e5f6", text: "f" }));
    expect(JSON.parse(h.out[2])).toMatchObject({ ok: true, result: { queued: true } });
    await h.send(req("r-13", "compact", { session_id: "a1b2c3d4e5f6" }));
    expect(JSON.parse(h.out[3])).toMatchObject({ ok: true, result: { started: true } });
    await h.send(req("r-14", "set_model", { session_id: "a1b2c3d4e5f6", model: "openai/gpt-4o" }));
    expect(JSON.parse(h.out[4])).toMatchObject({ ok: true, result: { model: "openai/gpt-4o" } });
    await h.send(req("r-15", "set_thinking_level", { session_id: "a1b2c3d4e5f6", level: "high" }));
    expect(JSON.parse(h.out[5])).toMatchObject({ ok: true, result: { thinking_level: "high" } });
    await h.send(req("r-16", "navigate_tree", { session_id: "a1b2c3d4e5f6", entry_id: "e9" }));
    expect(JSON.parse(h.out[6])).toMatchObject({ ok: true, result: { leaf_id: "leaf123" } });
    await h.send(req("r-17", "export_session", { session_id: "a1b2c3d4e5f6", out_path: "/tmp/x.jsonl" }));
    expect(JSON.parse(h.out[7])).toMatchObject({ ok: true, result: { path: "/tmp/x.jsonl" } });
  });

  test("get_messages + missing params", async () => {
    const h = harness();
    await h.send(req("r-18", "new_session", { session_id: "a1b2c3d4e5f6" }));
    await h.send(req("r-19", "get_messages", { session_id: "a1b2c3d4e5f6", limit: 5 }));
    expect(JSON.parse(h.out[1])).toMatchObject({ ok: true, result: { messages: [{ entry_id: "e1" }], next_cursor: null } });
    await h.send(req("r-20", "get_messages", { session_id: "a1b2c3d4e5f6" })); // M1：limit 缺省 → 默认 100，成功
    expect(JSON.parse(h.out[2])).toMatchObject({ ok: true, result: { messages: [{ entry_id: "e1" }] } });
    await h.send(req("r-20b", "get_messages", { session_id: "a1b2c3d4e5f6", limit: 0 })); // 非法 limit → bad_request
    expect(JSON.parse(h.out[3])).toMatchObject({ ok: false, error: { code: "bad_request" } });
  });

  test("set_tool_manifest / import_messages / tool_cancel", async () => {
    const h = harness();
    await h.send(req("r-21", "set_tool_manifest", { tools: [{ name: "a" }, { name: "b" }] }));
    expect(JSON.parse(h.out[0])).toMatchObject({ ok: true, result: { count: 2 } });
    // import_messages 是会话内操作，需先建会话（计划原测试漏了 new_session 导致 no_session）
    await h.send(req("r-22-prep", "new_session", { session_id: "a1b2c3d4e5f6" }));
    await h.send(req("r-22", "import_messages", { session_id: "a1b2c3d4e5f6", messages: [{ role: "user", content: "x" }] }));
    expect(JSON.parse(h.out[2])).toMatchObject({ ok: true, result: { imported: 2 } });
    await h.send(req("r-23", "tool_cancel", { call_id: "t-1" }));
    expect(JSON.parse(h.out[3])).toMatchObject({ ok: true, result: { cancelled: true } });
  });

  test("session_event forwarding: driver subscribe events reach sink as session_event frames", async () => {
    let listener: ((e: { type: string; payload: Record<string, unknown> }) => void) | null = null;
    const sess = makeFakeSession({
      subscribe: (fn) => {
        listener = fn;
        return () => {};
      },
    });
    const d = makeDriver({ open: async () => sess, get: () => sess });
    const h = harness(d);
    await h.send(req("r-24", "open_session", { session_id: "a1b2c3d4e5f6" }));
    listener!({ type: "agent_end", payload: { isTerminal: true } });
    const last = JSON.parse(h.out[h.out.length - 1]);
    expect(last).toMatchObject({ event: "session_event", session_id: "a1b2c3d4e5f6", data: { type: "agent_end" } });
  });
});
