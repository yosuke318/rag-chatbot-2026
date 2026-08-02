/** テーマ（ライト / ダーク）の保存と適用。
 *
 * ユーザーが選ぶのは auto / light / dark の3つだが、★DOMに書き込むのは
 * 解決後の light / dark だけ★。こうすると CSS 側は :root と
 * :root[data-theme="dark"] の2ブロックで済み、prefers-color-scheme の
 * 分岐をCSSに二重に持たなくてよい。auto のときのOS設定への追従は
 * ThemeToggle が matchMedia を購読して行う。
 */

/** ユーザーの選択。auto は「OSの設定に合わせる」。 */
export type ThemeChoice = "auto" | "light" | "dark";

/** 実際に <html data-theme> へ書き込む値。 */
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "rag-inspector:theme";

/** 保存された選択を読む。未設定・壊れた値はすべて auto に倒す。 */
export function readThemeChoice(): ThemeChoice {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === "light" || saved === "dark" || saved === "auto") return saved;
  } catch {
    // localStorage が使えない環境（プライベートモード等）では既定に倒す
  }
  return "auto";
}

export function prefersDark(): boolean {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    // matchMedia 自体が使えない環境。ここまで来たら判断材料がないのでライト扱い
    return false;
  }
}

export function resolveTheme(choice: ThemeChoice): ResolvedTheme {
  if (choice === "auto") return prefersDark() ? "dark" : "light";
  return choice;
}

/** 選択を <html> に反映する。保存はしない（呼び出し側の責務）。 */
export function applyTheme(choice: ThemeChoice): ResolvedTheme {
  const resolved = resolveTheme(choice);
  document.documentElement.dataset.theme = resolved;
  return resolved;
}

/** <head> で同期実行して、最初の描画の前に <html data-theme> を立てるスクリプト。
 *
 * これがないと、React がマウントして localStorage を読むまでのあいだ
 * ライトテーマで描画され、ダーク選択時に一瞬白く光る。上の関数群と処理が
 * 重複するが、バンドルの読み込みを待たずに走らせる必要があるため、
 * インラインの素のJSとして別に持つ。
 *
 * ★try は保存値の読み取りと OS 設定の参照で分ける★
 *   ひとまとめにして catch で light に倒すと、localStorage が使えない環境
 *   （プライベートモード等）で OS がダークのときにライトで描画され、マウント後に
 *   暗転する——防ぎたかった flicker がそのまま再発する。保存値が読めなくても
 *   OS 設定は読めるので、そこまでは活かす。
 */
export const THEME_INIT_SCRIPT = `(function(){
var c=null;
try{c=localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});}catch(e){}
if(c!=="light"&&c!=="dark"){
try{c=window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";}catch(e){c="light";}
}
document.documentElement.setAttribute("data-theme",c);
})();`;
