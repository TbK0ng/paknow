// Manual probe: run the viewer's inline script with a stub DOM and
// assert the 读图数据 panel's strip grouping. Run from the repo root:
//   node scripts/smoke_panel.mjs
import { readFileSync } from "node:fs";

const html = readFileSync("apps/viewer/index.html", "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

const node = () => ({
  addEventListener() {}, style: {}, classList: { add() {}, remove() {} },
  files: [], value: "", textContent: "", src: "", innerHTML: "",
  focus() {}, appendChild() {},
});
globalThis.document = { getElementById: () => node(), createElement: () => node() };
globalThis.URL = { createObjectURL: () => "blob:x", revokeObjectURL() {} };
globalThis.fetch = async () => { throw new Error("no network in probe"); };

const { renderCard } = new Function(script + "\nreturn { renderCard };")();

const level = (price, strip, basis) => ({ price, source: "annotation", strip, basis });
const read = (levels) => ({
  chart_read: { market_state: "trading_range", levels, unreadable: [] },
  answer: "正文", intent: "chart_analysis", routes: ["visual_page"], rounds: 1,
});

const two = renderCard("q", read([
  level(7490, 2, "第二条带"), level(7526.75, 1, "第一条带"), level(null, 2, "被裁切"),
]), null, true);
const one = renderCard("q", read([level(7526.75, 1, "唯一一条")]), null, true);

const fail = [];
const check = (ok, what) => { if (!ok) fail.push(what); };

// 2 strips: one header per strip, ascending, all three levels kept
const headers = [...two.matchAll(/<b>条带 (\d+)<\/b>/g)].map((m) => m[1]);
check(headers.join(",") === "1,2", `2-strip headers: ${headers}`);
check(two.indexOf("条带 1</b>") < two.indexOf("第一条带"), "strip 1 group first");
check(two.indexOf("条带 2</b>") < two.indexOf("第二条带"), "strip 2 level after its header");
check((two.match(/class="gitem"/g) || []).length === 5, "2-strip: 3 levels + 2 headers");
check(two.includes("看不清"), "null price stays 看不清");

// 1 strip: no group headers, level still shown
const oneHeaders = [...one.matchAll(/<b>条带 \d+<\/b>/g)];
check(oneHeaders.length === 0, `1-strip headers: ${oneHeaders.length}`);
check((one.match(/class="gitem"/g) || []).length === 1, "1-strip: one level row");
check(one.includes("7526.75"), "1-strip level rendered");

// no chart_read -> no panel at all
const none = renderCard("q", { answer: "文本" }, null, false);
check(!none.includes("读图数据"), "text answer has no panel");

if (fail.length) {
  console.error("FAIL:", fail.join("; "));
  process.exit(1);
}
console.log("panel probe OK: 2-strip grouped 1,2; 1-strip ungrouped; text hidden");
