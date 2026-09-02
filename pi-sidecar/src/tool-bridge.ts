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
  /** cancel() 显式取消标记：区分「用户中止」与「兜底超时/传输失败」（M8 语义）。 */
  cancelled?: boolean;
}

/** 每会话幂等键上下文（final-review finding 3）：两个并发 attempt 的交错事件各自
 * 更新自己的 scope，不再污染共享单值 → 幂等键前缀不再跨会话误标。 */
interface SessionScope {
  sessionId: string;
  assistantEntryId: string;
  turnIndex: number;
}

function newSessionScope(sessionId: string): SessionScope {
  return { sessionId, assistantEntryId: "unknown", turnIndex: 0 };
}

export class ToolBridge {
  private manifest: ToolManifestEntry[] = [];
  private inflight = new Map<string, Inflight>();
  private readonly scopes = new Map<string, SessionScope>();
  private readonly defaultScope: SessionScope = newSessionScope("unknown");

  constructor(private readonly hostCall: HostCall) {}

  /** 返回（并按需创建）sessionId 对应的会话上下文；undefined/空 → 默认 "unknown" scope。 */
  private scope(sessionId?: string): SessionScope {
    const id = sessionId && sessionId.length > 0 ? sessionId : "unknown";
    if (id === "unknown") return this.defaultScope;
    let sc = this.scopes.get(id);
    if (!sc) {
      sc = newSessionScope(id);
      this.scopes.set(id, sc);
    }
    return sc;
  }

  onSessionEvent(e: { type: string; payload: Record<string, unknown> }, sessionId?: string): void {
    // final-review finding 3：显式 vibeId（main 装配层按会话订阅传入）优先；缺省回落
    // 事件自带的 payload.sessionId；再无 → 默认 scope（tool-bridge 单测无会话上下文形态）。
    const target = sessionId
      ? this.scope(sessionId)
      : typeof e.payload.sessionId === "string"
        ? this.scope(e.payload.sessionId)
        : this.defaultScope;
    // 会话上下文标记：幂等键的 assistantEntryId 段（Global Constraint）
    if (e.type === "message_start" && typeof e.payload.entryId === "string") {
      target.assistantEntryId = e.payload.entryId;
    }
    // M7a 回退：SDK turn 事件携带 turnIndex，直接复用，无需自维护计数器
    if ((e.type === "turn_start" || e.type === "turn_end") && typeof e.payload.turnIndex === "number") {
      target.turnIndex = e.payload.turnIndex;
    }
  }

  setSessionId(sessionId: string): void {
    // 兼容保留：确保该会话 scope 存在（幂等键前缀固定为该会话 id）。
    this.scope(sessionId);
  }

  async setManifest(tools: unknown[]): Promise<number> {
    this.manifest = tools as ToolManifestEntry[];
    return this.manifest.length;
  }

  customTools(sessionId?: string): unknown[] {
    const scope = this.scope(sessionId);
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
          scope.assistantEntryId === "unknown" ? `turn${scope.turnIndex}` : scope.assistantEntryId;
        const idempotencyKey = `${scope.sessionId}:${assistantEntryRef}:${toolCallId}`;
        const controller = new AbortController();
        if (signal) signal.addEventListener("abort", () => controller.abort(), { once: true });
        // M8：兜底预算 = 2×timeout_seconds（下限 1h）。既作 sidecar 兜底计时器，也作为传给传输层的
        // timeoutMs（≥ 兜底预算）——sidecar 自己的计时器先触发并 abort，由它决定 outcome 分类，
        // 而不是让 HostRpc 默认 60s 超时抢先（否则写类传输失败会被误判为 error）。
        const fallbackMs = Math.max(t.timeout_seconds * 2_000, FALLBACK_TIMEOUT_MS);
        const entry: Inflight = {
          controller,
          timer: setTimeout(() => controller.abort(), fallbackMs),
        };
        this.inflight.set(toolCallId, entry);
        try {
          const r = (await this.hostCall(
            "tool_invoke",
            {
              call_id: toolCallId,
              toolCallId,
              toolName: t.name,
              arguments: args,
              idempotencyKey,
              is_readonly: t.is_readonly,
              side_effecting: t.side_effecting,
              repeatable: t.repeatable,
              timeout_seconds: t.timeout_seconds,
            },
            fallbackMs,
            controller.signal,
          )) as { content?: string; isError?: boolean; outcome?: string };
          const text = r?.content ?? JSON.stringify({ status: "error", error: "empty tool result" });
          return { content: [{ type: "text", text }], details: { outcome: r?.outcome ?? "ok", idempotencyKey } };
        } catch (e) {
          const err = e as Error & { code?: string };
          // cancel() 显式取消 → tool_cancelled 语义（commands.ts 的 tool_cancel 响应）。
          if (entry.cancelled) {
            return {
              content: [
                {
                  type: "text",
                  text: JSON.stringify({
                    status: "error",
                    error: "tool_cancelled",
                    outcome: !t.is_readonly ? "outcome_unknown" : "error",
                  }),
                },
              ],
            };
          }
          // M8：传输层失败（超时/中止/连接错误）下，写类结果未知 → outcome_unknown（绝不伪造结果）；
          // 只读 → error。side-effecting 永不自动重试（不重发即满足）。
          const text = JSON.stringify({
            status: "error",
            error: err.message,
            outcome: !t.is_readonly ? "outcome_unknown" : "error",
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
    e.cancelled = true;
    e.controller.abort();
    return true;
  }
}
