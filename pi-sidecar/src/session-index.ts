import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export const DEFAULT_SESSIONS_DIR = join(homedir(), ".vibe-trading", "pi", "sessions");
export const DEFAULT_INDEX_PATH = join(homedir(), ".vibe-trading", "pi", "session-index.json");

const VIBE_ID_RE = /^[0-9a-f]{12}$/;

export function isValidVibeSessionId(id: string): boolean {
  return VIBE_ID_RE.test(id);
}

/** 扫描文件前 8KB，找 type==="session" 的条目，返回其 id。 */
export function readSessionHeaderId(jsonPath: string): string | null {
  let text: string;
  try {
    // Bun.file(...).text() 是异步 Promise，不符合本同步 API；按 brief 注改用 readFileSync（8KB 前缀扫描语义相同）
    text = readFileSync(jsonPath, "utf8").slice(0, 8192);
  } catch {
    return null;
  }
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    try {
      const obj = JSON.parse(t) as Record<string, unknown>;
      if (obj.type === "session" && typeof obj.id === "string") return obj.id;
    } catch {
      // 固定宽度 title slot 可能不是合法整行 JSON 的一部分；跳过坏行
    }
  }
  return null;
}

export interface IndexedSession {
  path: string;
  headerId: string; // 文件自身的 Pi session header id（B2：不强制等于 vibeId）
}

export class SessionIndex {
  private map: Record<string, IndexedSession>;

  constructor(
    private readonly indexPath = DEFAULT_INDEX_PATH,
    private readonly sessionsDir = DEFAULT_SESSIONS_DIR,
  ) {
    try {
      const raw = JSON.parse(readFileSync(this.indexPath, "utf8")) as Record<string, IndexedSession | string>;
      // 兼容 v0 纯路径格式（升级容忍）
      this.map = {};
      for (const [k, v] of Object.entries(raw)) {
        this.map[k] = typeof v === "string" ? { path: v, headerId: k } : v;
      }
    } catch {
      this.map = {};
    }
  }

  resolve(vibeId: string): string | null {
    if (!isValidVibeSessionId(vibeId)) return null;
    const entry = this.map[vibeId];
    if (!entry) return null;
    // B2 替换检测：文件必须仍有 session header，且 id === 登记时记录的 headerId
    if (readSessionHeaderId(entry.path) !== entry.headerId) return null;
    return entry.path;
  }

  register(vibeId: string, jsonlPath: string): void {
    if (!isValidVibeSessionId(vibeId)) throw new Error(`invalid vibe session id: ${vibeId}`);
    const headerId = readSessionHeaderId(jsonlPath);
    if (headerId === null) {
      throw new Error(`session file has no valid session header: ${jsonlPath}`);
    }
    this.map[vibeId] = { path: jsonlPath, headerId };
    mkdirSync(dirname(this.indexPath), { recursive: true });
    mkdirSync(this.sessionsDir, { recursive: true });
    const tmp = `${this.indexPath}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.map, null, 2));
    renameSync(tmp, this.indexPath); // 原子写（design §Lazy migration 标记语义）
  }

  all(): Record<string, IndexedSession> {
    return { ...this.map };
  }
}
