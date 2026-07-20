/**
 * READMEに載せるスクリーンショットを撮る。
 *
 * 使い方（backend/frontend が起動している状態で）:
 *   cd frontend && npm run screenshot
 *
 * ブラウザは同梱せず、インストール済みの Chrome を使う（puppeteer-core）。
 * 画面を変えたら撮り直せるよう、手作業ではなくスクリプトにしてある。
 */
import { mkdir } from "node:fs/promises";
import puppeteer from "puppeteer-core";

const CHROME =
  process.env.CHROME_PATH ||
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const BASE = process.env.BASE_URL || "http://localhost:3000";
const OUT = new URL("../../docs/images/", import.meta.url).pathname;

// 検索の内訳が見やすい、複数トピックにまたがる質問
const QUESTION =
  "リモートワークで休暇ってどんな扱いなの？休暇の間は経費の扱いどうなる？";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

await mkdir(OUT, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: "new",
  args: ["--no-sandbox", "--force-device-scale-factor=2"], // 2x で文字を鮮明に
});

const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 900, deviceScaleFactor: 2 });
await page.goto(BASE, { waitUntil: "networkidle0" });
await sleep(800); // /retrievers の取得とチェックボックス描画を待つ

// --- 1枚目: 全体像（登録・検索・質問の3パネル）---
await page.screenshot({ path: `${OUT}overview.png`, fullPage: true });
console.log("saved: docs/images/overview.png");

// --- 2枚目: 検索の内訳（3手法すべてを有効にして検索）---
const boxes = await page.$$(".retriever-option input[type=checkbox]");
for (const b of boxes) {
  if (!(await b.evaluate((el) => el.checked))) await b.click();
}
await page.type('input[placeholder^="検索したい質問"]', QUESTION);
await page.evaluate(() =>
  [...document.querySelectorAll("button")]
    .find((b) => b.textContent.trim() === "検索")
    .click(),
);
await page.waitForSelector("tbody tr", { timeout: 30000 });
await sleep(600);

// 検索パネルだけを切り出す
const panel = await page.$$(".panel");
await panel[1].screenshot({ path: `${OUT}search-breakdown.png` });
console.log("saved: docs/images/search-breakdown.png");

await browser.close();
