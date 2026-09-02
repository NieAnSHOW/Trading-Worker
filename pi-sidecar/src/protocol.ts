export const PROTOCOL_VERSION = 1;
export const MAX_FRAME_BYTES = 1_048_576;

export type RequestFrame = { v: 1; id: string; op: string; params?: Record<string, unknown> };
export type OkFrame = { v: 1; id: string; ok: true; result: unknown };
export type ErrFrame = { v: 1; id: string; ok: false; error: { code: string; message: string } };
export type EventFrame = { v: 1; event: string; session_id?: string; data?: unknown };
export type Frame = RequestFrame | OkFrame | ErrFrame | EventFrame;

export class FrameError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "FrameError";
  }
}

function byteLen(s: string): number {
  return Buffer.byteLength(s, "utf8");
}

export function encodeFrame(frame: Frame): string {
  const line = JSON.stringify(frame);
  if (byteLen(line) > MAX_FRAME_BYTES) {
    throw new FrameError("frame_too_large", `frame exceeds ${MAX_FRAME_BYTES} bytes`);
  }
  return line + "\n";
}

function isNonEmptyString(x: unknown): x is string {
  return typeof x === "string" && x.length > 0;
}

export function decodeFrame(line: string): Frame {
  const trimmed = line.trim();
  if (!trimmed) throw new FrameError("bad_json", "empty frame");
  let obj: unknown;
  try {
    obj = JSON.parse(trimmed);
  } catch (e) {
    throw new FrameError("bad_json", `invalid JSON: ${(e as Error).message}`);
  }
  if (typeof obj !== "object" || obj === null) throw new FrameError("bad_frame", "frame is not an object");
  const f = obj as Record<string, unknown>;
  if (f.v !== PROTOCOL_VERSION) throw new FrameError("bad_frame", `unsupported v: ${String(f.v)}`);
  if ("event" in f) {
    if (!isNonEmptyString(f.event)) throw new FrameError("bad_frame", "event must be a non-empty string");
    if (f.session_id !== undefined && !isNonEmptyString(f.session_id)) throw new FrameError("bad_frame", "bad session_id");
    return f as unknown as EventFrame;
  }
  if (!isNonEmptyString(f.id)) throw new FrameError("bad_frame", "id must be a non-empty string");
  if ("op" in f) {
    if (!isNonEmptyString(f.op)) throw new FrameError("bad_frame", "op must be a non-empty string");
    if (f.params !== undefined && (typeof f.params !== "object" || f.params === null || Array.isArray(f.params))) {
      throw new FrameError("bad_frame", "params must be an object");
    }
    return f as unknown as RequestFrame;
  }
  if ("ok" in f) {
    if (f.ok === true) return f as unknown as OkFrame;
    if (f.ok === false) {
      const err = f.error as Record<string, unknown> | undefined;
      if (!err || !isNonEmptyString(err.code) || typeof err.message !== "string") {
        throw new FrameError("bad_frame", "error frame needs {code,message}");
      }
      return f as unknown as ErrFrame;
    }
    throw new FrameError("bad_frame", "ok must be boolean");
  }
  throw new FrameError("bad_frame", "frame is neither request, response nor event");
}

export class LineSplitter {
  private buf = "";

  push(chunk: string): string[] {
    this.buf += chunk;
    if (byteLen(this.buf) > MAX_FRAME_BYTES && !this.buf.includes("\n")) {
      this.buf = "";
      throw new FrameError("frame_too_large", `line exceeds ${MAX_FRAME_BYTES} bytes`);
    }
    const out: string[] = [];
    let idx: number;
    while ((idx = this.buf.indexOf("\n")) >= 0) {
      out.push(this.buf.slice(0, idx));
      this.buf = this.buf.slice(idx + 1);
    }
    return out;
  }
}
