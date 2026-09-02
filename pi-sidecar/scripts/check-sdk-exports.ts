const pkg: Record<string, unknown> = await import("@oh-my-pi/pi-coding-agent");
const names = Object.keys(pkg).sort();
console.log(JSON.stringify({ ok: true, version: "18.0.11", exports: names }, null, 2));
