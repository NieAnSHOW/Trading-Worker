import { encodeFrame, type EventFrame, type Frame } from "./protocol";

export interface MsgOut {
  entry_id: string;
  role: string;
  content: string;
  timestamp: string;
}

export interface SessionLike {
  sessionId: string;
  sessionFile: string;
  isBusy(): boolean;
  subscribe(fn: (e: { type: string; payload: Record<string, unknown> }) => void): () => void;
  prompt(text: string): Promise<void>;
  steer(text: string): void;
  followUp(text: string): void;
  abort(): void;
  setModel(model: string): Promise<void>;
  setThinkingLevel(level: string): Promise<void>;
  compact(instructions?: string): Promise<void>;
  navigateTree(entryId: string): Promise<string>;
  getMessages(beforeCursor: string | null, limit: number): { messages: MsgOut[]; nextCursor: string | null };
  exportTo(outPath: string): Promise<void>;
  importMessages(msgs: { role: string; content: string; timestamp?: string }[]): Promise<number>;
}

export interface SessionDriver {
  open(vibeId: string): Promise<SessionLike>;
  create(vibeId: string, model?: string, thinkingLevel?: string): Promise<SessionLike>;
  get(vibeId: string): SessionLike | undefined;
  setToolManifest(tools: unknown[]): Promise<number>;
  cancelToolCall(callId: string): boolean;
}

export type FrameSink = (raw: string) => void;

const ok = (id: string, result: unknown) => encodeFrame({ v: 1, id, ok: true, result });
const err = (id: string, code: string, message: string) => encodeFrame({ v: 1, id, ok: false, error: { code, message } });
const event = (frame: Omit<EventFrame, "v">) => encodeFrame({ v: 1, ...frame });

function requireParams(params: Record<string, unknown> | undefined, keys: string[]): Record<string, unknown> {
  const p = params ?? {};
  for (const k of keys) {
    if (!(k in p)) throw Object.assign(new Error(`missing param: ${k}`), { code: "bad_request" });
  }
  return p;
}

function needSession(deps: { driver: SessionDriver }, sessionId: unknown): SessionLike {
  if (typeof sessionId !== "string") throw Object.assign(new Error("missing session_id"), { code: "bad_request" });
  const s = deps.driver.get(sessionId);
  if (!s) throw Object.assign(new Error(`no open session ${sessionId}`), { code: "no_session" });
  return s;
}

function errCode(e: unknown): string {
  const c = (e as { code?: unknown })?.code;
  return typeof c === "string" ? c : "internal";
}

export function createCommandHandler(deps: {
  driver: SessionDriver;
  sink: FrameSink;
  hostCall: (op: string, params: Record<string, unknown>, timeoutMs?: number) => Promise<unknown>;
}): (line: string) => Promise<void> {
  const { driver, sink, hostCall } = deps;

  // 每会话事件订阅：driver open/create 后由 handler 首次接线
  const wired = new Set<string>();
  function wireEvents(s: SessionLike): void {
    if (wired.has(s.sessionId)) return;
    wired.add(s.sessionId);
    s.subscribe((e) => {
      try {
        sink(event({ event: "session_event", session_id: s.sessionId, data: { type: e.type, payload: e.payload } }));
      } catch {
        // sink 写失败（管道关闭）由 main 层处理；此处不 throw
      }
    });
  }

  async function handleRequest(id: string, op: string, params: Record<string, unknown> | undefined): Promise<string> {
    switch (op) {
      case "ping":
        return ok(id, { pong: true, protocol: 1 });
      case "new_session": {
        const p = requireParams(params, ["session_id"]);
        const s = await driver.create(String(p.session_id), p.model as string | undefined, p.thinking_level as string | undefined);
        wireEvents(s);
        return ok(id, { session_file: s.sessionFile });
      }
      case "open_session": {
        const p = requireParams(params, ["session_id"]);
        const s = await driver.open(String(p.session_id));
        wireEvents(s);
        return ok(id, { session_file: s.sessionFile });
      }
      case "prompt": {
        const p = requireParams(params, ["session_id", "text"]);
        const s = needSession(deps, p.session_id);
        if (s.isBusy()) return err(id, "session_busy", "a turn is already active");
        wireEvents(s);
        void s.prompt(String(p.text)).catch(() => {
          // prompt 失败不 ack 失败（已 ack accepted）；错误经 session_event 流出
        });
        return ok(id, { accepted: true });
      }
      case "steer": {
        const p = requireParams(params, ["session_id", "text"]);
        needSession(deps, p.session_id).steer(String(p.text));
        return ok(id, { queued: true });
      }
      case "follow_up": {
        const p = requireParams(params, ["session_id", "text"]);
        needSession(deps, p.session_id).followUp(String(p.text));
        return ok(id, { queued: true });
      }
      case "abort": {
        const p = requireParams(params, ["session_id"]);
        needSession(deps, p.session_id).abort();
        return ok(id, { aborted: true });
      }
      case "get_state": {
        const p = requireParams(params, ["session_id"]);
        const s = needSession(deps, p.session_id);
        return ok(id, {
          busy: s.isBusy(),
          model: (s as unknown as { currentModel?: () => string | null }).currentModel?.() ?? null,
          thinking_level: (s as unknown as { currentThinkingLevel?: () => string | null }).currentThinkingLevel?.() ?? null,
          session_file: s.sessionFile,
          session_id: s.sessionId,
        });
      }
      case "get_messages": {
        const p = requireParams(params, ["session_id"]); // M1：limit 可选，默认 100
        const s = needSession(deps, p.session_id);
        if (s.isBusy()) return err(id, "session_busy", "cannot page messages mid-turn");
        const before = (p.before_cursor as string | undefined) ?? null;
        const limit = p.limit === undefined ? 100 : Number(p.limit);
        if (!Number.isInteger(limit) || limit < 1) return err(id, "bad_request", "limit must be positive integer");
        const r = s.getMessages(before, limit);
        return ok(id, { messages: r.messages, next_cursor: r.nextCursor });
      }
      case "set_model": {
        const p = requireParams(params, ["session_id", "model"]);
        await needSession(deps, p.session_id).setModel(String(p.model));
        return ok(id, { model: String(p.model) });
      }
      case "set_thinking_level": {
        const p = requireParams(params, ["session_id", "level"]);
        await needSession(deps, p.session_id).setThinkingLevel(String(p.level));
        return ok(id, { thinking_level: String(p.level) });
      }
      case "compact": {
        const p = requireParams(params, ["session_id"]);
        const s = needSession(deps, p.session_id);
        void s.compact(p.instructions as string | undefined).catch(() => {});
        return ok(id, { started: true });
      }
      case "navigate_tree": {
        const p = requireParams(params, ["session_id", "entry_id"]);
        const leaf = await needSession(deps, p.session_id).navigateTree(String(p.entry_id));
        return ok(id, { leaf_id: leaf });
      }
      case "export_session": {
        const p = requireParams(params, ["session_id", "out_path"]);
        await needSession(deps, p.session_id).exportTo(String(p.out_path));
        return ok(id, { path: String(p.out_path) });
      }
      case "set_tool_manifest": {
        const p = requireParams(params, ["tools"]);
        const count = await driver.setToolManifest(p.tools as unknown[]);
        return ok(id, { count });
      }
      case "import_messages": {
        const p = requireParams(params, ["session_id", "messages"]);
        const msgs = p.messages as { role: string; content: string; timestamp?: string }[];
        const n = await needSession(deps, p.session_id).importMessages(msgs);
        return ok(id, { imported: n });
      }
      case "tool_cancel": {
        const p = requireParams(params, ["call_id"]);
        return ok(id, { cancelled: driver.cancelToolCall(String(p.call_id)) });
      }
      default:
        return err(id, "unknown_op", `unknown op: ${op}`);
    }
  }

  return async (line: string): Promise<void> => {
    let id = "unknown";
    try {
      const trimmed = line.trim();
      if (!trimmed) return;
      let obj: Record<string, unknown>;
      try {
        obj = JSON.parse(trimmed) as Record<string, unknown>;
      } catch (e) {
        sink(err("unknown", "bad_json", `invalid JSON: ${(e as Error).message}`));
        return;
      }
      if (obj.v !== 1 || typeof obj.id !== "string" || typeof obj.op !== "string") {
        sink(err("unknown", "bad_frame", "request must be {v:1,id,op}"));
        return;
      }
      id = obj.id;
      sink(await handleRequest(id, obj.op, obj.params as Record<string, unknown> | undefined));
    } catch (e) {
      sink(err(id, errCode(e), (e as Error).message));
    }
  };
}
