// テーマ（ライト/ダーク）の解決テスト。
//
// ★狙いは「初回描画のちらつき防止スクリプトが本体と同じ答えを出すこと」★
//   THEME_INIT_SCRIPT は バンドルを待たずに走らせるため、theme.ts の関数とは
//   別に同じ判定を素のJSで持っている。ここがズレると、リロード直後だけ違う
//   テーマで描画されて暗転する——ブラウザで一瞬しか見えず気づきにくいので、
//   両者が一致することをテストで固定する。
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyTheme,
  readThemeChoice,
  resolveTheme,
  THEME_INIT_SCRIPT,
  THEME_STORAGE_KEY,
} from "../app/theme";

/** localStorage の最小スタブ。saved が undefined なら未設定を表す。 */
function stubStorage(saved?: string) {
  const store = new Map<string, string>();
  if (saved !== undefined) store.set(THEME_STORAGE_KEY, saved);
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
  });
  return store;
}

/** プライベートモード等、localStorage に触ると例外が飛ぶ環境。 */
function stubThrowingStorage() {
  vi.stubGlobal("localStorage", {
    getItem: () => {
      throw new Error("SecurityError");
    },
  });
}

function stubOsDark(matches: boolean) {
  vi.stubGlobal("window", {
    matchMedia: (query: string) => ({ matches: query.includes("dark") && matches }),
  });
}

/** <html> の代わり。data-theme の書き込み先だけ持つ。 */
function stubDocument() {
  const el = {
    dataset: {} as Record<string, string>,
    setAttribute(name: string, value: string) {
      // init スクリプトは setAttribute を使うので、dataset と同じ場所に寄せる
      if (name === "data-theme") el.dataset.theme = value;
    },
  };
  vi.stubGlobal("document", { documentElement: el });
  return el;
}

/** THEME_INIT_SCRIPT を実行して、書き込まれた data-theme を返す。 */
function runInitScript(): string | undefined {
  const el = stubDocument();
  new Function(THEME_INIT_SCRIPT)();
  return el.dataset.theme;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("readThemeChoice", () => {
  it("保存された選択をそのまま返す", () => {
    stubStorage("dark");
    expect(readThemeChoice()).toBe("dark");
  });

  it("未設定なら auto", () => {
    stubStorage();
    expect(readThemeChoice()).toBe("auto");
  });

  it("知らない値が入っていても auto に倒す", () => {
    stubStorage("solarized");
    expect(readThemeChoice()).toBe("auto");
  });

  it("localStorage が触れない環境でも例外を投げない", () => {
    stubThrowingStorage();
    expect(readThemeChoice()).toBe("auto");
  });
});

describe("resolveTheme", () => {
  it("auto は OS の設定に従う", () => {
    stubOsDark(true);
    expect(resolveTheme("auto")).toBe("dark");
    stubOsDark(false);
    expect(resolveTheme("auto")).toBe("light");
  });

  it("明示的な選択は OS の設定より優先される", () => {
    stubOsDark(true);
    expect(resolveTheme("light")).toBe("light");
    stubOsDark(false);
    expect(resolveTheme("dark")).toBe("dark");
  });
});

describe("applyTheme", () => {
  it("解決後の light/dark だけを data-theme に書く（auto は書かない）", () => {
    stubOsDark(true);
    const el = stubDocument();
    expect(applyTheme("auto")).toBe("dark");
    expect(el.dataset.theme).toBe("dark");
  });
});

describe("THEME_INIT_SCRIPT", () => {
  it("保存された選択を描画前に反映する", () => {
    stubStorage("dark");
    stubOsDark(false); // OSはライトだが、明示的な dark が勝つ
    expect(runInitScript()).toBe("dark");
  });

  it("未設定なら OS の設定に従う", () => {
    stubStorage();
    stubOsDark(true);
    expect(runInitScript()).toBe("dark");
  });

  it("auto を保存していても OS の設定に従う", () => {
    stubStorage("auto");
    stubOsDark(true);
    expect(runInitScript()).toBe("dark");
  });

  // ★保存値が読めないことと、OS設定が読めないことは別物★
  //   まとめて light に倒すと、プライベートモード＋OSダークで初回だけライトに
  //   なり、マウント後に暗転する。防ぎたかった flicker がそのまま再発する。
  it("localStorage が触れなくても OS の設定には従う", () => {
    stubThrowingStorage();
    stubOsDark(true);
    expect(runInitScript()).toBe("dark");
  });

  it("matchMedia も使えない場合だけライトに倒れる", () => {
    stubThrowingStorage();
    vi.stubGlobal("window", {}); // matchMedia なし
    expect(runInitScript()).toBe("light");
  });

  it.each(["auto", "light", "dark"] as const)(
    "選択が %s のとき resolveTheme と同じ答えを出す",
    (choice) => {
      for (const osDark of [true, false]) {
        stubStorage(choice);
        stubOsDark(osDark);
        expect(runInitScript()).toBe(resolveTheme(choice));
      }
    },
  );

  it("localStorage が触れないときも本体と同じ答えを出す", () => {
    for (const osDark of [true, false]) {
      stubThrowingStorage();
      stubOsDark(osDark);
      expect(runInitScript()).toBe(resolveTheme(readThemeChoice()));
    }
  });
});
