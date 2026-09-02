import { describe, expect, test } from "bun:test";
import {
  MAX_FRAME_BYTES,
  PROTOCOL_VERSION,
  FrameError,
  LineSplitter,
  decodeFrame,
  encodeFrame,
} from "./protocol";

describe("encodeFrame", () => {
  test("appends LF and keeps JSON round-trippable", () => {
    const req = { v: 1, id: "r-1", op: "ping" } as const;
    const line = encodeFrame(req);
    expect(line.endsWith("\n")).toBe(true);
    expect(decodeFrame(line)).toEqual(req);
  });

  test("rejects frames over 1 MiB", () => {
    const big = { v: 1, id: "r-1", op: "x", params: { blob: "a".repeat(MAX_FRAME_BYTES) } };
    expect(() => encodeFrame(big as never)).toThrow(FrameError);
    try {
      encodeFrame(big as never);
    } catch (e) {
      expect((e as FrameError).code).toBe("frame_too_large");
    }
  });
});

describe("decodeFrame", () => {
  test("bad json -> bad_json", () => {
    expect(() => decodeFrame("{nope")).toThrow(FrameError);
    try {
      decodeFrame("{nope");
    } catch (e) {
      expect((e as FrameError).code).toBe("bad_json");
    }
  });

  test("empty line -> bad_json", () => {
    expect(() => decodeFrame("   ")).toThrow(FrameError);
  });

  test("wrong shape -> bad_frame", () => {
    const bads = [
      '{"v":2,"id":"x","op":"ping"}',
      '{"id":"x","op":"ping"}',
      '{"v":1,"id":1,"op":"ping"}',
      '{"v":1,"event":42}',
      '{"v":1,"id":"x","ok":false,"error":{"code":"x"}}',
    ];
    for (const bad of bads) {
      try {
        decodeFrame(bad);
        expect.unreachable();
      } catch (e) {
        expect((e as FrameError).code).toBe("bad_frame");
      }
    }
  });

  test("accepts all four frame kinds", () => {
    expect(decodeFrame('{"v":1,"id":"r-1","op":"ping","params":{"a":1}}')).toMatchObject({ op: "ping" });
    expect(decodeFrame('{"v":1,"id":"r-1","ok":true,"result":{}}')).toMatchObject({ ok: true });
    expect(decodeFrame('{"v":1,"id":"r-1","ok":false,"error":{"code":"x","message":"y"}}')).toMatchObject({ ok: false });
    expect(decodeFrame('{"v":1,"event":"ready","data":{"protocol":1}}')).toMatchObject({ event: "ready" });
  });
});

describe("LineSplitter", () => {
  test("splits incremental chunks on LF", () => {
    const s = new LineSplitter();
    expect(s.push('{"v":1,"id":"a')).toEqual([]);
    expect(s.push('","op":"ping"}\n{"v":1,"id":"b","op":"p"}\n')).toEqual([
      '{"v":1,"id":"a","op":"ping"}',
      '{"v":1,"id":"b","op":"p"}',
    ]);
  });

  test("oversized line -> frame_too_large", () => {
    const s = new LineSplitter();
    expect(() => s.push("x".repeat(MAX_FRAME_BYTES + 1))).toThrow(FrameError);
    try {
      s.push("x".repeat(MAX_FRAME_BYTES + 1));
    } catch (e) {
      expect((e as FrameError).code).toBe("frame_too_large");
    }
  });

  test("protocol version constant is 1", () => {
    expect(PROTOCOL_VERSION).toBe(1);
  });
});
