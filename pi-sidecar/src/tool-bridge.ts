import type { HostCall } from "./host-rpc";

export interface ToolManifestEntry {
  name: string;
  label?: string;
  description: string;
  parameters: unknown;
  is_readonly: boolean;
  side_effecting: boolean;
  repeatable: boolean;
  timeout_seconds: number;
}

const FALLBACK_TIMEOUT_MS = 3_600_000; // 2 × 1800s 兜底，防 Python 无响应挂死

interface Inflight {
  controller: AbortController;
  timer: ReturnType<typeof setTimeout>;
}

export class ToolBridge {
  private manifest: ToolManifestEntry[] = [];
  private inflight = new Map<string, Inflight>();
  private currentAssistantEntryId = "unknown";
  private currentSessionId = "unknown";
  // M7a：message_start 不带 entryId（pinned 18.0.11 实测），用 SDK 自带的
  // turn_start/turn_end.turnIndex 作为轮次计数（Task 0 bonus finding）。
  private currentTurnIndex = 0;

  constructor(private readonly hostCall: HostCall) {}

  onSessionEvent(e: { type: string; payload: Record<string, unknown> }): void {
    // 会话上下文标记：幂等键的 assistantEntryId 段（Global Constraint）
    if (e.type === "message_start" && typeof e.payload.entryId === "string") {
      this.currentAssistantEntryId = e.payload.entryId;
    }
    // M7a 回退：SDK turn 事件携带 turnIndex，直接复用，无需自维护计数器
    if ((e.type === "turn_start" || e.type === "turn_end") && typeof e.payload.turnIndex === "number") {
      this.currentTurnIndex = e.payload.turnIndex;
    }
    if (typeof e.payload.sessionId === "string") {
      this.currentSessionId = e.payload.sessionId;
    }
  }

  setSessionId(sessionId: string): void {
    this.currentSessionId = sessionId;
  }

  async setManifest(tools: unknown[]): Promise<number> {
    this.manifest = tools as ToolManifestEntry[];
    return this.manifest.length;
  }

  customTools(): unknown[] {
    return this.manifest.map((t) => ({
      name: t.name,
      label: t.label ?? t.name,
      description: t.description,
      parameters: t.parameters,
      execute: async (
        toolCallId: string,
        args: Record<string, unknown>,
        onUpdateOrSignal: unknown,
        _ctx?: unknown,
        maybeSignal?: unknown,
      ): Promise<{ content: Array<{ type: string; text: string }>; details?: unknown }> => {
        // SDK 实际签名 (toolCallId, params, onUpdate, ctx, signal?)：AbortSignal 在第 5 位
        // （customToolToDefinition/wrapper.ts 转发）；brief 测试按 (id, params, signal, onUpdate, ctx)
        // 调用。两种形态下都只认 AbortSignal，避免把 onUpdate 函数误当 signal。
        const signal =
          maybeSignal instanceof AbortSignal
            ? maybeSignal
            : onUpdateOrSignal instanceof AbortSignal
              ? onUpdateOrSignal
              : null;
        // M7a：无 entryId 时用 turn<轮次> 作为 assistantEntryRef 段
        const assistantEntryRef =
          this.currentAssistantEntryId === "unknown" ? `turn${this.currentTurnIndex}` : this.currentAssistantEntryId;
        const idempotencyKey = `${this.currentSessionId}:${assistantEntryRef}:${toolCallId}`;
        const controller = new AbortController();
        if (signal) signal.addEventListener("abort", () => controller.abort(), { once: true });
        const entry: Inflight = {
          controller,
          timer: setTimeout(() => controller.abort(), Math.max(t.timeout_seconds * 2_000, FALLBACK_TIMEOUT_MS)),
        };
        this.inflight.set(toolCallId, entry);
        try {
          const r = (await this.hostCall("tool_invoke", {
            call_id: toolCallId,
            toolCallId,
            toolName: t.name,
            arguments: args,
            idempotencyKey,
            is_readonly: t.is_readonly,
            side_effecting: t.side_effecting,
            repeatable: t.repeatable,
            timeout_seconds: t.timeout_seconds,
          })) as { content?: string; isError?: boolean; outcome?: string };
          const text = r?.content ?? JSON.stringify({ status: "error", error: "empty tool result" });
          return { content: [{ type: "text", text }], details: { outcome: r?.outcome ?? "ok", idempotencyKey } };
        } catch (e) {
          const err = e as Error & { code?: string };
          const cancelled = controller.signal.aborted && err.code === "timeout";
          const text = JSON.stringify({
            status: "error",
            error: err.message,
            outcome: cancelled && !t.is_readonly ? "outcome_unknown" : "error",
          });
          return { content: [{ type: "text", text }] };
        } finally {
          clearTimeout(entry.timer);
          this.inflight.delete(toolCallId);
        }
      },
    }));
  }

  cancel(callId: string): boolean {
    const e = this.inflight.get(callId);
    if (!e) return false;
    e.controller.abort();
    return true;
  }
}
