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
});
