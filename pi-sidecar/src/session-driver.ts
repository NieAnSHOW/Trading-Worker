import { createAgentSession, SessionManager, Settings } from "@oh-my-pi/pi-coding-agent";
import { homedir } from "node:os";
import { join } from "node:path";
import type { SessionDriver, SessionLike, MsgOut } from "./commands";
import { SessionIndex } from "./session-index";
import { resolveDefaultModel } from "./model-wiring";

export const PI_AGENT_DIR = join(homedir(), ".vibe-trading", "pi", "agent");
export const PI_SESSIONS_DIR = join(homedir(), ".vibe-trading", "pi", "sessions");

/**
 * 工具桥的构造面（Task 7 `ToolBridge` 的占位契约）。Task 7 未落地时本文件不 import
 * `./tool-bridge`（避免编译期依赖缺失）；真实 ToolBridge 结构上满足此接口。
 */
interface ToolBridgeLike {
  customTools(): unknown[];
  setManifest(tools: unknown[]): Promise<number>;
  cancel(callId: string): boolean;
}

interface PiEventLike {
  type: string;
  [k: string]: unknown;
}

/** AgentSession 的松散结构视图：字段名以 pinned SDK d.ts 为准，可选调用防 SDK 漂移。 */
interface PiAgentSession {
  isStreaming?: () => boolean;
  subscribe?: (fn: (e: PiEventLike) => void) => () => void;
  prompt?: (text: string, opts?: Record<string, unknown>) => Promise<unknown>;
  steer?: (text: string) => unknown;
  followUp?: (text: string) => unknown;
  abort?: () => unknown;
  setModel?: (m: unknown) => Promise<unknown>;
  setThinkingLevel?: (l: unknown) => void;
  compact?: (instructions?: string) => Promise<unknown>;
  navigateTree?: (entryId: string) => Promise<unknown>;
  model?: { provider?: string; id?: string };
  thinkingLevel?: unknown;
  modelRegistry?: { find?: (provider: string, modelId: string) => unknown };
  sessionManager?: {
    getEntries?: () => Array<Record<string, unknown>>;
    appendMessage?: (msg: unknown) => string;
    ensureOnDisk?: () => Promise<void> | void;
  };
}

export class PiSessionHandle implements SessionLike {
  constructor(
    readonly sessionId: string,
    readonly sessionFile: string,
    private readonly session: unknown,
    private readonly index: SessionIndex,
  ) {}

  private get s(): PiAgentSession {
    return this.session as PiAgentSession;
  }

  isBusy(): boolean {
    return this.s.isStreaming?.() ?? false;
  }

  currentModel(): string | null {
    const m = this.s.model;
    return m?.provider && m?.id ? `${m.provider}/${m.id}` : null;
  }

  currentThinkingLevel(): string | null {
    const l = this.s.thinkingLevel;
    return typeof l === "string" && l ? l : null;
  }

  subscribe(fn: (e: { type: string; payload: Record<string, unknown> }) => void): () => void {
    const sub = this.s.subscribe;
    if (!sub) return () => {};
    return sub.call(this.s, (e: PiEventLike) => {
      const { type, ...payload } = e;
      fn({ type, payload: payload as Record<string, unknown> });
    });
  }

  async prompt(text: string): Promise<void> {
    await this.s.prompt?.(text, { streamingBehavior: "steer" });
  }

  steer(text: string): void {
    void this.s.steer?.(text);
  }

  followUp(text: string): void {
    void this.s.followUp?.(text);
  }

  abort(): void {
    void this.s.abort?.();
  }

  async setModel(model: string): Promise<void> {
    const slash = model.indexOf("/");
    const provider = slash > 0 ? model.slice(0, slash) : "";
    const id = slash > 0 ? model.slice(slash + 1) : model;
    const resolved = this.s.modelRegistry?.find?.(provider, id);
    if (!resolved) throw new Error(`model not found: ${model}`);
    await this.s.setModel?.(resolved);
  }

  async setThinkingLevel(level: string): Promise<void> {
    this.s.setThinkingLevel?.(level);
  }

  async compact(instructions?: string): Promise<void> {
    await this.s.compact?.(instructions);
  }

  async navigateTree(entryId: string): Promise<string> {
    const r = (await this.s.navigateTree?.(entryId)) as
      | { cancelled?: boolean; aborted?: boolean }
      | undefined;
    // 钉版 SDK（agent-session.d.ts:954-1006）返回 {cancelled, aborted?, …}，绝不返回字符串。
    // cancelled/aborted = 导航被取消/中止，必须上抛而不是把请求的 entryId 当成功回显。
    if (r && (r.cancelled === true || r.aborted === true)) {
      throw Object.assign(
        new Error(`navigateTree: navigation to ${entryId} was cancelled/aborted`),
        { code: "navigation_failed" },
      );
    }
    return entryId;
  }

  getMessages(beforeCursor: string | null, limit: number): { messages: MsgOut[]; nextCursor: string | null } {
    const entries = (this.s.sessionManager?.getEntries?.() ?? []) as Array<Record<string, unknown>>;
    const msgs = entries.filter((e) => e.type === "message");
    let end = msgs.length;
    if (beforeCursor) {
      const i = msgs.findIndex((e) => e.id === beforeCursor);
      if (i < 0) throw Object.assign(new Error("stale cursor"), { code: "stale_cursor" });
      end = i;
    }
    const start = Math.max(0, end - limit);
    const slice = msgs.slice(start, end);
    return {
      messages: slice.map((e) => this.toMsgOut(e)),
      nextCursor: start > 0 ? String(slice[0]?.id ?? "") || null : null,
    };
  }

  private toMsgOut(e: Record<string, unknown>): MsgOut {
    const m = (e.message ?? {}) as { role?: string; content?: unknown };
    let content = "";
    if (typeof m.content === "string") content = m.content;
    else if (Array.isArray(m.content)) {
      content = m.content
        .filter((b) => (b as { type?: string }).type === "text")
        .map((b) => (b as { text?: string }).text ?? "")
        .join("");
    }
    return {
      entry_id: String(e.id ?? ""),
      role: String(m.role ?? "user"),
      content,
      timestamp: String(e.timestamp ?? ""),
    };
  }

  /** header 必须落盘后 index.register 才能读到 headerId（B2）。 */
  private async ensureOnDisk(): Promise<void> {
    const sm = this.s.sessionManager;
    if (sm?.ensureOnDisk) await sm.ensureOnDisk();
  }

  async exportTo(outPath: string): Promise<void> {
    await Bun.write(outPath, await Bun.file(this.sessionFile).text());
  }

  async importMessages(msgs: { role: string; content: string; timestamp?: string }[]): Promise<number> {
    const sm = this.s.sessionManager;
    if (!sm?.appendMessage) throw new Error("SessionManager appendMessage unavailable in pinned SDK");
    let n = 0;
    for (const m of msgs) {
      if (m.role !== "user" && m.role !== "assistant") continue;
      sm.appendMessage({ role: m.role, content: m.content, timestamp: Date.now() });
      n += 1;
    }
    // 触发落盘 + 索引登记（design §Lazy migration: 同 ID Pi 会话；create() 已登记，此处幂等）
    await this.ensureOnDisk();
    this.index.register(this.sessionId, this.sessionFile);
    return n;
  }
}

export class PiSessionDriver implements SessionDriver {
  readonly index: SessionIndex;

  constructor(
    private readonly opts: {
      index: SessionIndex;
      toolBridge: ToolBridgeLike;
      env?: NodeJS.ProcessEnv;
      extensions?: unknown[]; // B1：vibe-providers/vibe-memory factories（main.ts 生产注入）
    },
  ) {
    this.index = opts.index;
  }

  async create(vibeId: string, model?: string, _thinkingLevel?: string): Promise<SessionLike> {
    const sessionsDir = this.index.sessionsDir;
    const sm = SessionManager.create(sessionsDir, sessionsDir);
    const m = model ?? resolveDefaultModel(this.opts.env);
    const s = await this.buildSession(vibeId, sm, m);
    this.handles.set(vibeId, s);
    // B2/B2b 落盘序：SessionManager.create 新会话默认 memory-only（omp://session.md），
    // ensureOnDisk 后 index.register 才能读到 headerId（Task 3 B2 语义）。
    await s.ensureOnDisk();
    this.index.register(vibeId, s.sessionFile);
    return s;
  }

  private async buildSession(vibeId: string, sessionManager: SessionManager, model: string | null): Promise<PiSessionHandle> {
    const sessionsDir = this.index.sessionsDir;
    const result = await createAgentSession({
      cwd: sessionsDir,
      agentDir: PI_AGENT_DIR,
      settings: Settings.isolated(),
      sessionManager,
      extensions: this.opts.extensions as never[],
      customTools: this.opts.toolBridge.customTools() as never[],
      toolNames: [], // Global Constraint: 内建 read/write/edit/bash 全禁用
      restrictToolNames: true,
      allowRestrictedCustomTools: true,
      enableMCP: false,
      enableLsp: false,
      disableExtensionDiscovery: true,
      ...(model ? { modelPattern: model } : {}),
    });
    const session = result.session;
    const file = session.sessionFile ?? "";
    return new PiSessionHandle(vibeId, file, session, this.index);
  }

  async open(vibeId: string): Promise<SessionLike> {
    const p = this.index.resolve(vibeId);
    if (!p) throw Object.assign(new Error(`session ${vibeId} not found`), { code: "not_found" });
    const sm = await SessionManager.open(p);
    const s = await this.buildSession(vibeId, sm, null);
    this.handles.set(vibeId, s);
    return s;
  }

  get(vibeId: string): SessionLike | undefined {
    return this.handles.get(vibeId);
  }

  setToolManifest(tools: unknown[]): Promise<number> {
    return this.opts.toolBridge.setManifest(tools);
  }

  cancelToolCall(callId: string): boolean {
    return this.opts.toolBridge.cancel(callId);
  }

  private handles = new Map<string, SessionLike>();
}
