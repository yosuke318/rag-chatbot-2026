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

// ★等倍で撮る（2x にしない）★
// 2x は文字が鮮明になる代わりに画素数が4倍になり、PNGが1枚500KBを超えて
// リポジトリに入れられなくなった。READMEでは横900px程度に縮んで表示され、
// 原寸で開いても等倍なら文字は読める。容量を取るほどの差にならない。
const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: "new",
  args: ["--no-sandbox", "--force-device-scale-factor=1"],
});

const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 900, deviceScaleFactor: 1 });
await page.goto(BASE, { waitUntil: "networkidle0" });
await sleep(800); // /retrievers の取得とチェックボックス描画を待つ

// --- 1枚目: 全体像（開いた直後の画面 = ① 文書を登録）---
// サイドバーで機能を切り替えるようになってからは、1枚に全機能は写らない。
// 開いた直後に何が見えるか（左に4つの機能・右に①の3つの章）を写す。
await page.screenshot({ path: `${OUT}overview.png`, fullPage: true });
console.log("saved: docs/images/overview.png");

// --- 2枚目: 検索の内訳（テキストの3手法を有効にして検索）---
// ★この1枚だけ画面を広く取る★ 手法を3つ入れると「順位 + スコア」の列が
// 3組並ぶので、1280 幅では右端の「内容」が切れて、どのチャンクの話なのかが
// 読めなくなる。
await page.setViewport({ width: 1700, height: 900, deviceScaleFactor: 1 });

// ②のタブを選ばないと検索の画面は描画されない（選択中のタブしか描かない）。
await page.evaluate(() => {
  const tab = [...document.querySelectorAll(".sidebar-tab")].find((b) =>
    b.textContent.includes("検索の内訳"),
  );
  tab.click();
});
await page.waitForSelector(".retriever-option input[type=checkbox]");
await sleep(400);

// 画像ベクトル検索は外す。図表を入れていないDBだと列が「―」だけになり、
// 各手法が順位をどう付けたかを見せる、というこの図の趣旨から外れるため。
const boxes = await page.$$(".retriever-option");
for (const opt of boxes) {
  const isImage = await opt.evaluate((el) => el.textContent.includes("画像"));
  const box = await opt.$('input[type="checkbox"]');
  const checked = await box.evaluate((el) => el.checked);
  if (checked !== !isImage) await box.click();
}

await page.type('input[placeholder^="検索したい質問"]', QUESTION);
await page.evaluate(() =>
  [...document.querySelectorAll("button")]
    .find((b) => b.textContent.trim() === "検索")
    .click(),
);
await page.waitForSelector("tbody tr", { timeout: 30000 });
await sleep(600);

// ★撮る前にポインタとフォーカスを画面から外す★
// 説明（.tip）は hover でも focus-within でも開くので、操作した場所に
// ポインタが残っていると、後から描かれた表の上に説明が被った状態で写る。
await page.mouse.move(0, 0);
await page.evaluate(() => document.activeElement?.blur());
await sleep(300);

// 結果の表を含むパネルを切り出す。★何番目か（panel[1]）では指さない★
// タブごとにパネルの数が変わるので、表を持っているパネルを探して撮る。
const panel = await page.evaluateHandle(() =>
  [...document.querySelectorAll(".panel")].find((p) => p.querySelector("tbody tr")),
);
await panel.asElement().screenshot({ path: `${OUT}search-breakdown.png` });
console.log("saved: docs/images/search-breakdown.png");

await browser.close();
