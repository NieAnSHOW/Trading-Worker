import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

interface HostBridge {
  call: (op: string, params?: Record<string, unknown>) => Promise<unknown>;
  emitDiagnostic: (kind: string, message: string) => void;
}

// host bridge 访问器：main.ts 在 createAgentSession 前设置 globalThis.__VIBE_HOST__
function host(): HostBridge {
  return (globalThis as { __VIBE_HOST__?: HostBridge }).__VIBE_HOST__ as HostBridge;
}

/**
 * pinned SDK 事件形状（d.ts governs）：`before_provider_request` = { type, payload: <request> }，
 * 修订机制 = handler 返回值整体替换 request（runner: `if (l !== void 0) s = l`）。
 * openai-completions 请求体 = { model, messages, ... }，system prompt 在 messages 的 role:"system" 条目；
 * anthropic-messages 用顶层 `system` 字段。仅这两种形状可注入；未知形状返回 undefined（请求原样）。
 */
function withSystemPrompt(req: Record<string, unknown>, block: string): Record<string, unknown> | undefined {
  const messages = req.messages;
  if (Array.isArray(messages)) {
    const next = [...messages];
    const sysIdx = next.findIndex((m) => m && typeof m === "object" && m.role === "system");
    if (sysIdx >= 0) {
      const sys = next[sysIdx];
      next[sysIdx] = { ...sys, content: appendBlock(sys.content, block) };
    } else {
      next.unshift({ role: "system", content: block });
    }
    return { ...req, messages: next };
  }
  if ("system" in req) {
    const sys = req.system;
    if (typeof sys === "string") return { ...req, system: `${sys}\n\n${block}` };
    if (Array.isArray(sys)) return { ...req, system: [...sys, { type: "text", text: block }] };
    return { ...req, system: block };
  }
  return undefined;
}

// 追加记忆块到已有 system content；非 string/数组形状时整体替换（该 dialect 不可合并，注入优先）
function appendBlock(content: unknown, block: string): unknown {
  if (typeof content === "string") return `${content}\n\n${block}`;
  if (Array.isArray(content)) return [...content, { type: "text", text: block }];
  return block;
}

// 参数 schema：真实 SDK 的 ExtensionAPI 恒注入 zod（d.ts 契约）；单测 fake pi 无 zod → 退化空 schema（仅校验 name/execute）
function zodObject<T>(pi: ExtensionAPI, build: (zod: ExtensionAPI["zod"]) => T): T {
  const zod = pi.zod;
  return zod ? build(zod) : ({} as T);
}

export default function (pi: ExtensionAPI) {
  let memoryBlock = "";
  let injectedThisTurn = false;
  let lastInput = ""; // B5: 最近用户文本，作为 memory 相关性检索的 query

  pi.on("input", async (event: { text?: string }) => {
    if (typeof event?.text === "string" && event.text.trim()) {
      lastInput = event.text;
    }
  });

  pi.on("before_agent_start", async () => {
    memoryBlock = "";
    injectedThisTurn = false;
    try {
      const r = (await host().call("memory_context", { query: lastInput })) as { block?: string };
      memoryBlock = r?.block ?? "";
    } catch (e) {
      host().emitDiagnostic("memory_degraded", `memory unavailable, continuing without it: ${(e as Error).message}`);
    }
  });

  pi.on("before_provider_request", async (event: unknown) => {
    if (!memoryBlock || injectedThisTurn) return;
    injectedThisTurn = true;
    const ev = event as { payload?: unknown; systemPrompt?: unknown; revise?: (req: unknown) => unknown };
    // pinned SDK：payload = 请求体，返回值 = 修订后的请求（每轮 system prompt 注入；design：永不入用户消息/持久转录）
    if (ev.payload && typeof ev.payload === "object") {
      const payload = ev.payload as Record<string, unknown>; // SDK 契约：payload 即 provider 请求体对象
      return withSystemPrompt(payload, memoryBlock);
    }
    // 预 pinning 假定形状（event.revise({ systemPrompt })）——保留以兼容既有测试契约
    if (typeof ev.revise === "function") {
      ev.revise({ systemPrompt: `${typeof ev.systemPrompt === "string" ? ev.systemPrompt : ""}\n\n${memoryBlock}` });
    }
  });

  pi.registerTool({
    name: "remember",
    label: "Remember",
    description: "Persist a long-term memory entry (immediately durable; current prompt snapshot unchanged)",
    parameters: zodObject(pi, (z) =>
      z.object({
        name: z.string().describe("Memory name"),
        content: z.string().describe("Memory body"),
        memory_type: z.string().optional().describe("user|feedback|project|reference"),
        description: z.string().optional().describe("One-line description"),
      }),
    ),
    async execute(_id, params) {
      const r = (await host().call("remember", params as Record<string, unknown>)) as { ok?: boolean };
      return { content: [{ type: "text", text: JSON.stringify(r ?? { ok: false }) }] };
    },
  });

  pi.registerTool({
    name: "memory_search",
    label: "Memory Search",
    description: "Search long-term memory entries",
    parameters: zodObject(pi, (z) => z.object({ query: z.string() })),
    async execute(_id, params) {
      const r = await host().call("memory_search", params as Record<string, unknown>);
      return { content: [{ type: "text", text: JSON.stringify(r ?? {}) }] };
    },
  });

  pi.registerTool({
    name: "memory_remove",
    label: "Memory Remove",
    description: "Remove a long-term memory entry by name",
    parameters: zodObject(pi, (z) => z.object({ name: z.string() })),
    async execute(_id, params) {
      const r = await host().call("memory_remove", params as Record<string, unknown>);
      return { content: [{ type: "text", text: JSON.stringify(r ?? {}) }] };
    },
  });
}
