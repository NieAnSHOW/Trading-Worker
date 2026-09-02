import { describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SessionIndex } from "./session-index";

describe("PiSessionDriver construction", () => {
  test("creates with isolated dirs and tool bridge (smoke)", async () => {
    const { PiSessionDriver } = await import("./session-driver");
    const dir = mkdtempSync(join(tmpdir(), "vibe-pidrv-"));
    try {
      const bridge = {
        customTools: () => [] as unknown[],
        onSessionEvent: () => {},
      };
      const driver = new PiSessionDriver({
        index: new SessionIndex(join(dir, "idx.json"), dir),
        toolBridge: bridge as never,
        env: {}, // 测试环境不注册任何 provider
      });
      expect(typeof driver.create).toBe("function");
      expect(typeof driver.open).toBe("function");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("navigateTree surfaces cancellation/abort as a thrown error, happy path returns entry id", async () => {
    const { PiSessionHandle } = await import("./session-driver");
    const dir = mkdtempSync(join(tmpdir(), "vibe-pidrv3-"));
    try {
      const idx = new SessionIndex(join(dir, "idx.json"), dir);
      const make = (s: unknown) =>
        new PiSessionHandle("sess1", join(dir, "sess1.json"), s, idx);

      // 取消：{cancelled:true} 必须上抛，不能把请求的 entryId 当成功回显
      await expect(
        make({ navigateTree: async () => ({ cancelled: true }) }).navigateTree("leaf1"),
      ).rejects.toThrow(/cancelled|aborted/);

      // 中止：{aborted:true} 同样上抛
      await expect(
        make({ navigateTree: async () => ({ aborted: true }) }).navigateTree("leaf1"),
      ).rejects.toThrow(/cancelled|aborted/);

      // SDK 拒绝（reject）时错误原样透传
      await expect(
        make({
          navigateTree: async () => {
            throw new Error("sdk boom");
          },
        }).navigateTree("leaf1"),
      ).rejects.toThrow(/sdk boom/);

      // 成功：返回请求的 entryId
      await expect(
        make({ navigateTree: async () => ({ cancelled: false }) }).navigateTree("leaf9"),
      ).resolves.toBe("leaf9");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("importMessages builds structurally valid AssistantMessage for assistant turns", async () => {
    const { PiSessionHandle } = await import("./session-driver");
    const dir = mkdtempSync(join(tmpdir(), "vibe-pidrv4-"));
    try {
      const idx = new SessionIndex(join(dir, "idx.json"), dir);
      const captured: unknown[] = [];
      const sm = {
        appendMessage: (m: unknown) => {
          captured.push(m);
          return "entry1";
        },
        // 真实流程 ensureOnDisk 会落盘 session header（register 据此校验），fake 补齐
        ensureOnDisk: async () => {
          writeFileSync(join(dir, "sess1.json"), JSON.stringify({ type: "session", id: "abcd12345678" }));
        },
      };
      const h = new PiSessionHandle("abcd12345678", join(dir, "sess1.json"), { sessionManager: sm }, idx);
      const n = await h.importMessages([
        { role: "user", content: "hi", timestamp: "2026-09-01T00:00:00Z" },
        { role: "assistant", content: "hello back", timestamp: "2026-09-01T00:00:01Z" },
      ]);
      expect(n).toBe(2);

      // user 路径保持原样（UserMessage.content 允许 string）
      const userMsg = captured[0] as Record<string, unknown>;
      expect(userMsg.role).toBe("user");
      expect(userMsg.content).toBe("hi");

      // assistant 路径：content 必须是内容块数组 + 必填元数据齐全
      const asst = captured[1] as Record<string, unknown>;
      expect(asst.role).toBe("assistant");
      expect(Array.isArray(asst.content)).toBe(true);
      expect(asst.content).toEqual([{ type: "text", text: "hello back" }]);
      expect(asst.api).toBe("anthropic-messages");
      expect(asst.provider).toBe("vibe-import");
      expect(asst.model).toBe("imported");
      // usage 必须带 cost（SDK addUsage 无守卫读 usage.cost.total；Task 17 集成实证）
      expect(asst.usage).toEqual({
        input: 0,
        output: 0,
        cacheRead: 0,
        cacheWrite: 0,
        totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
      });
      expect(asst.stopReason).toBe("stop");
      expect(typeof asst.timestamp).toBe("number");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("create produces session whose sessionFile lives under sessions dir and header id matches", async () => {
    const { PiSessionDriver } = await import("./session-driver");
    const dir = mkdtempSync(join(tmpdir(), "vibe-pidrv2-"));
    try {
      const bridge = { customTools: () => [], onSessionEvent: () => {} };
      const driver = new PiSessionDriver({
        index: new SessionIndex(join(dir, "idx.json"), dir),
        toolBridge: bridge as never,
        env: {},
      });
      const s = await driver.create("a1b2c3d4e5f6");
      expect(s.sessionId).toBe("a1b2c3d4e5f6");
      expect(s.sessionFile.startsWith(dir)).toBe(true);
      // 触发落盘并登记索引
      await s.importMessages([{ role: "user", content: "hello" }]);
      expect(driver.index.resolve("a1b2c3d4e5f6")).toBe(s.sessionFile);
      // open 回读同一文件
      const s2 = await driver.open("a1b2c3d4e5f6");
      expect(s2.sessionFile).toBe(s.sessionFile);
      writeFileSync(join(dir, "marker"), "done");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  // 回归守卫（Fix: builtin tool lockdown restored）：Global Constraint 要求 Pi 内建
  // read/write/edit/bash 不可解析、manifest customTools 正常装载、任何工具执行不得绕过
  // Python gateway。commit e1ca82ba 把 restrictToolNames 翻 false 后 toolNames:[] 并不
  // 禁用内建——实测 write 仍在（plan mode 触发 ensureWriteRegistered 泄漏）。本测试断言
  // 内建 write 缺席 + 注册的 custom tool 在会话工具集内（锁 = toolNames:[] + plan.enabled=false）。
  test("builtin write/edit/bash are absent and manifest custom tool is present in session tools", async () => {
    const { PiSessionDriver } = await import("./session-driver");
    const dir = mkdtempSync(join(tmpdir(), "vibe-pidrv5-"));
    try {
      const fakeTool = {
        name: "fake_manifest_tool",
        description: "probe custom manifest tool",
        parameters: { type: "object", properties: {} },
        execute: async () => ({ content: [{ type: "text", text: "ok" }] }),
      };
      const bridge = {
        customTools: () => [fakeTool],
        onSessionEvent: () => {},
      };
      const driver = new PiSessionDriver({
        index: new SessionIndex(join(dir, "idx.json"), dir),
        toolBridge: bridge as never,
        env: {},
      });
      const s = await driver.create("b2c3d4e5f6a7");
      // PiSessionHandle 把底层 AgentSession 收在 private getter `s`；测试需探工具集，
      // 单点边界断言形状（test-only，非生产路径）。
      type ToolProbe = { getToolByName(name: string): unknown };
      const raw: ToolProbe = (s as unknown as { s: ToolProbe }).s;
      for (const builtin of ["read", "write", "edit", "bash"]) {
        expect(raw.getToolByName(builtin), `builtin ${builtin} must not be resolvable`).toBeUndefined();
      }
      expect(raw.getToolByName("fake_manifest_tool")).toBeDefined();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
