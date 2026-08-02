"use client";

import { createCache, extractStyle, StyleProvider } from "@ant-design/cssinjs";
import { ConfigProvider, theme as antdTheme } from "antd";
import jaJP from "antd/locale/ja_JP";
import { useServerInsertedHTML } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import type { ResolvedTheme } from "./theme";

/** ドロップダウンの候補リストの高さ上限（px）。これを超えるとリスト内でスクロールする。
 *
 * ネイティブの <select> は候補が増えると画面いっぱいに開いてしまい、高さを
 * 指定する手段が無い。antd の Select/AutoComplete は listHeight で上限を
 * 決められるので、区分・文書名のセレクタはこの値で揃える。
 * 8件強で切れる高さ（1件32px）。それ以上はスクロールで送る。
 */
export const LIST_HEIGHT = 264;

/** 現在の解決済みテーマ（light / dark）を返す。
 *
 * ★<html data-theme> を単一の情報源にする★
 *   テーマの選択を持っているのは ThemeToggle（page.tsx）だが、そこから
 *   ConfigProvider まで state を引き回すと、選択の保存・OS設定への追従・
 *   antd への反映の3つが1箇所に集まって絡む。属性を購読する形にすると、
 *   誰が属性を変えても（初回のインラインスクリプト / ThemeToggle /
 *   OS設定の変更）ここは同じように追従できる。
 */
function useResolvedTheme(): ResolvedTheme {
  // サーバー描画では属性が読めないので light で描く。マウント後に実際の値へ
  // 差し替わる。CSS変数側は layout.tsx のインラインスクリプトが描画前に
  // 当てているので、ここがずれている間に色が違うのは antd 製の部品だけ。
  const [resolved, setResolved] = useState<ResolvedTheme>("light");

  useEffect(() => {
    const read = () =>
      document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    setResolved(read());
    // data-theme が誰に書き換えられても拾う（ThemeToggle・OS設定の変更）
    const observer = new MutationObserver(() => setResolved(read()));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);

  return resolved;
}

/** SSRで吐いた antd のスタイルを入れる <style> の id。 */
const SSR_STYLE_ID = "antd-cssinjs";

/** antd を使うための土台。ページ全体をこれで包む。
 *
 * やっていることは2つ。
 *
 * 1. **SSRでスタイルを吐く。** antd v6 は CSS-in-JS なので、何もしないと
 *    スタイルはブラウザで JS が走ってから当たる＝初回描画で素のHTMLが
 *    一瞬見える。useServerInsertedHTML で cache に溜まった分を <style> として
 *    サーバーのHTMLに差し込む。
 * 2. **テーマを渡す。** ダーク時は darkAlgorithm に切り替え、色は globals.css
 *    の CSS変数と揃えた値を token で上書きする（antd の既定色をそのまま使うと、
 *    自前のパネルと antd の部品で青やグレーが微妙に食い違う）。
 */
export function AntdProvider({ children }: { children: React.ReactNode }) {
  const cache = useMemo(() => createCache(), []);
  // useServerInsertedHTML はストリーミング中に複数回呼ばれる。2回目以降で
  // extractStyle すると同じスタイルを重ねて吐くので、1回で打ち切る。
  const inserted = useRef(false);

  useServerInsertedHTML(() => {
    if (inserted.current) return;
    inserted.current = true;
    return (
      <style
        id={SSR_STYLE_ID}
        dangerouslySetInnerHTML={{ __html: extractStyle(cache, true) }}
      />
    );
  });

  const resolved = useResolvedTheme();
  const dark = resolved === "dark";

  return (
    <StyleProvider cache={cache}>
      <ConfigProvider
        locale={jaJP}
        theme={{
          algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
          // ★テーマごとに別のキーを振る★
          //   antd v6 はトークンをCSS変数で配り、変数は .css-var-<key> という
          //   クラスに載る。キーを固定すると、SSRで吐いたライトの変数ブロックと
          //   クライアントが入れるダークの変数ブロックが同じクラス名で重なり、
          //   後ろに置かれた SSR 側が勝って「ダークなのに antd だけ白い」に
          //   なる（サーバーはテーマを知らないので必ずライトで吐く）。
          //   キーを分ければ部品側のクラスがダーク用に切り替わるので、
          //   SSRのライト分はそのまま無効になる。
          cssVar: { key: dark ? "ragi-dark" : "ragi-light" },
          token: {
            // globals.css の :root / :root[data-theme="dark"] と同じ値。
            // 片方だけ直すと色がズレるので、変えるときは両方セットで。
            colorPrimary: dark ? "#6ea8fe" : "#2563eb",
            colorBgContainer: dark ? "#1c1f26" : "#ffffff",
            colorBgElevated: dark ? "#1c1f26" : "#ffffff",
            colorBorder: dark ? "#2e333d" : "#e2e2e6",
            colorText: dark ? "#e6e8eb" : "#1a1a1a",
            colorTextPlaceholder: dark ? "#9aa3af" : "#6b7280",
            colorError: dark ? "#f28b82" : "#b91c1c",
            borderRadius: 8,
            // 自前のCSSは font: inherit で通しているので、antd も同じ字面に揃える
            fontFamily: "inherit",
            // 素の input（globals.css の `font: inherit` ＝ body の16px、上下
            // padding 8px）と同じ行に並ぶので、字の大きさと高さをそちらに合わせる。
            // antd の既定（14px / 32px）のままだと同じ行で背が揃わない。
            fontSize: 16,
            controlHeight: 38,
          },
        }}
      >
        {children}
      </ConfigProvider>
    </StyleProvider>
  );
}
