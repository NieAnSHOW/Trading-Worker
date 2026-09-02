#!/usr/bin/env bun
import { createCommandHandler, type SessionDriver, type SessionLike } from "./commands";
import { HostRpc } from "./host-rpc";
import { ToolBridge } from "./tool-bridge";
import { SessionIndex } from "./session-index";
import { PiSessionDriver } from "./session-driver";
import { encodeFrame, LineSplitter, decodeFrame, FrameError } from "./protocol";

export interface MainDeps {
  driver: SessionDriver;
  hostCall: (op: string, params: Record<string, unknown>, timeoutMs?: number) => Promise<unknown>;
  writeOut: (raw: string) => void;
  extensions: unknown[]; // ExtensionFactory[]；测试注入 []，生产注入 vibe-memory + vibe-providers
}

export function createMain(deps?: Partial<MainDeps>) {
  // 1) ready 帧（协议输出唯一出口 writeOut；测试可注入捕获器）
  const writeOut = deps?.writeOut ?? ((raw: string) => process.stdout.write(raw));
  writeOut(encodeFrame({ v: 1, event: "ready", data: { protocol: 1, pid: process.pid, sdk: "18.0.11" } }));

  // 2) HostRpc：sidecar→Python 双向 RPC（响应帧经 handleLine 回流派发）。
  //    测试注入 hostCall 时把它当作传输层：出站请求帧解码后直连注入函数，结果以 ok 帧
  //    回灌 hostRpc（pending 的 id 关联结算；手工帧先到则先结算，天然幂等）。
  const injectedHostCall = deps?.hostCall;
  let hostRpc: HostRpc;
  const send: (raw: string) => void = injectedHostCall
    ? (raw) => {
        let req: { id?: unknown; op?: unknown; params?: unknown };
        try {
          req = JSON.parse(raw);
        } catch {
          writeOut(raw);
          return;
        }
        if (typeof req.id !== "string" || typeof req.op !== "string") {
          writeOut(raw);
          return;
        }
        void injectedHostCall(req.op, (req.params ?? {}) as Record<string, unknown>)
          .then((result) => hostRpc.onFrame({ id: req.id, ok: true, result }))
          .catch((e: unknown) =>
            hostRpc.onFrame({
              id: req.id,
              ok: false,
              error: { code: (e as { code?: string }).code ?? "internal", message: (e as Error).message ?? String(e) },
            }),
          );
      }
    : (raw) => writeOut(raw);
  hostRpc = new HostRpc(send, (frame) => encodeFrame(frame as never));

  // 3) host bridge 必须先于 extension import 设置（vibe-providers/vibe-memory 的 factory
  //    在会话创建时才调用 bridge，但全局引用必须在 import 时已可见）
  (globalThis as Record<string, unknown>).__VIBE_HOST__ = {
    call: (op: string, params: Record<string, unknown> = {}) => hostRpc.call(op, params, 60_000),
    emitDiagnostic: (kind: string, message: string) => {
      process.stderr.write(`[vibe:${kind}] ${message}\n`); // stderr 永不混入协议流
      writeOut(encodeFrame({ v: 1, event: "session_event", session_id: "", data: { type: "pi.notice", payload: { kind, message } } }));
    },
  };

  // 4) 工具桥 + 会话索引 + 驱动（生产模式动态 import 打包后的 extension 产物）。
  //    extensions 数组持有在 main.ts：PiSessionDriver 构造 opts 读同一引用，异步装载
  //    的 push 对会话创建时的 createAgentSession({ extensions }) 可见。
  const toolBridge = new ToolBridge((op, params, timeoutMs) => hostRpc.call(op, params, timeoutMs));
  const index = new SessionIndex();
  const extensions: unknown[] = [];
  const baseDriver: SessionDriver =
    deps?.driver ?? new PiSessionDriver({ index, toolBridge, env: process.env, extensions });

  // B1 桥接：会话事件喂 ToolBridge（幂等键上下文）+ setSessionId 落在真实 vibe session id
  // （commands.ts 的 wireEvents 负责 Python 侧 session_event 帧；本处负责 bridge 侧，
  //   ToolBridgeLike 接缝刻意不含 onSessionEvent，由装配层直接从 handle 接线）
  function wireSession(s: SessionLike, vibeId: string): SessionLike {
    toolBridge.setSessionId(vibeId);
    s.subscribe((e) => toolBridge.onSessionEvent(e));
    return s;
  }
  const driver: SessionDriver = {
    open: async (vibeId) => wireSession(await baseDriver.open(vibeId), vibeId),
    create: async (vibeId, model, thinkingLevel) =>
      wireSession(await baseDriver.create(vibeId, model, thinkingLevel), vibeId),
    get: (vibeId) => baseDriver.get(vibeId),
    setToolManifest: (tools) => baseDriver.setToolManifest(tools),
    cancelToolCall: (callId) => baseDriver.cancelToolCall(callId),
  };

  if (!deps?.driver && !deps?.extensions) {
    // 生产模式：从暂存资源动态加载 extensions（打包产物 extensions/*.js，Task 15）；
    // 开发态回退相对源路径的 index.ts（brief 实现者注）
    void (async () => {
      for (const [prod, dev] of [
        ["extensions/vibe-providers.js", "../extensions/vibe-providers/index.ts"],
        ["extensions/vibe-memory.js", "../extensions/vibe-memory/index.ts"],
      ] as const) {
        let mod: { default?: unknown } | undefined;
        for (const p of [`${import.meta.dir}/${prod}`, dev]) {
          try {
            mod = (await import(p)) as { default?: unknown };
            break;
          } catch {
            // 候选路径不存在/装载失败 → 尝试下一个（vibe-memory 属 Task 14，开发态预期缺）
          }
        }
        if (mod && mod.default !== undefined) extensions.push(mod.default);
        else process.stderr.write(`[vibe:extension-load] ${prod}: no loadable module\n`);
      }
    })();
  }

  // 5) 命令处理 + stdin 行循环
  const handler = createCommandHandler({
    driver,
    sink: writeOut,
    hostCall: (op, params, timeoutMs) => hostRpc.call(op, params, timeoutMs),
  });

  async function handleLine(line: string): Promise<void> {
    // 入站帧可能是：Python 请求（op）→ handler；或 HostRpc 响应（ok）→ 派发；其余忽略
    const trimmed = line.trim();
    if (!trimmed) return;
    try {
      const probe = decodeFrame(trimmed);
      if ("ok" in probe) {
        hostRpc.onFrame(probe as never); // HostRpc 响应（含 Python 对 host 请求的答复）
        return;
      }
    } catch {
      // 非 JSON/坏帧：交给 handler 走统一错误帧路径
    }
    await handler(line);
  }

  if (!deps?.writeOut) {
    // 真实 stdin 循环（测试注入 writeOut 时不启动）
    const splitter = new LineSplitter();
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk: string) => {
      let lines: string[] = [];
      try {
        lines = splitter.push(chunk);
      } catch (e) {
        if (e instanceof FrameError) writeOut(encodeFrame({ v: 1, id: "unknown", ok: false, error: { code: e.code, message: e.message } }));
        return;
      }
      for (const ln of lines) void handleLine(ln);
    });
    process.stdin.on("end", () => process.exit(0));
  }

  return {
    handleLine,
    close: () => {
      hostRpc.rejectAll(new Error("main closed"));
    },
  };
}

if (import.meta.main) {
  createMain(); // 生产入口：生产 extensions 经上方动态 import 装配
}
