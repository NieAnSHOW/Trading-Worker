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
