/** sidecar→Python 双向 RPC helper：id 关联 + 超时。main.ts 负责把响应帧喂给 resolve。 */
export class HostRpc {
  private seq = 0;
  private pending = new Map<string, { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> }>();

  constructor(
    private readonly send: (raw: string) => void,
    private readonly encode: (frame: unknown) => string,
  ) {}

  call(op: string, params: Record<string, unknown>, timeoutMs = 60_000): Promise<unknown> {
    const id = `s-${++this.seq}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(Object.assign(new Error(`host call ${op} timed out`), { code: "timeout" }));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.send(this.encode({ v: 1, id, op, params }));
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e as Error);
      }
    });
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
