/** ToolBridge 等调用方注入的 host 传输函数签名。signal 可选：中止时传输层应立即 reject（code:"aborted"）。 */
export type HostCall = (
  op: string,
  params: Record<string, unknown>,
  timeoutMs?: number,
  signal?: AbortSignal,
) => Promise<unknown>;

/** sidecar→Python 双向 RPC helper：id 关联 + 超时。main.ts 负责把响应帧喂给 resolve。 */
export class HostRpc {
  private seq = 0;
  private pending = new Map<string, { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> }>();

  constructor(
    private readonly send: (raw: string) => void,
    private readonly encode: (frame: unknown) => string,
  ) {}

  call(op: string, params: Record<string, unknown>, timeoutMs = 60_000, signal?: AbortSignal): Promise<unknown> {
    const id = `s-${++this.seq}`;
    const { promise, resolve, reject } = Promise.withResolvers<unknown>();
    let timer: ReturnType<typeof setTimeout>;
    // 外部信号中止（ToolBridge 兜底计时器 / cancel / SDK 中止）：立即以 code:"aborted" reject，
    // 并清掉自己的超时计时器，避免挂死或双重结算。
    const onAbort = () => {
      clearTimeout(timer);
      this.pending.delete(id);
      reject(Object.assign(new Error(`host call ${op} aborted`), { code: "aborted" }));
    };
    timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      this.pending.delete(id);
      reject(Object.assign(new Error(`host call ${op} timed out`), { code: "timeout" }));
    }, timeoutMs);
    if (signal) {
      if (signal.aborted) {
        onAbort();
        return promise;
      }
      signal.addEventListener("abort", onAbort, { once: true });
    }
    this.pending.set(id, { resolve, reject, timer });
    try {
      this.send(this.encode({ v: 1, id, op, params }));
    } catch (e) {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      this.pending.delete(id);
      reject(e as Error);
    }
    return promise;
  }

  /** main.ts 读循环：把 id 相同的响应帧派发给等待者。 */
  onFrame(frame: { id?: string; ok?: boolean; result?: unknown; error?: { code: string; message: string } }): boolean {
    const id = frame.id;
    if (!id || !("ok" in frame)) return false;
    const p = this.pending.get(id);
    if (!p) return false;
    this.pending.delete(id);
    clearTimeout(p.timer);
    if (frame.ok) p.resolve(frame.result);
    else p.reject(Object.assign(new Error(frame.error?.message ?? "host error"), { code: frame.error?.code ?? "internal" }));
    return true;
  }

  rejectAll(err: Error): void {
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(err);
    }
    this.pending.clear();
  }
}
