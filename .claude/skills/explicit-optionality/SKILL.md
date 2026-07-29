---
name: explicit-optionality
description: >-
  Use whenever adding or changing a field in backend/app/schemas.py (Pydantic
  models), a column in backend/app/db.py (table DDL), or a TypeScript field
  shape in the frontend — anywhere a field's required/optional-ness is being
  decided. Makes sure a field is never simultaneously "required" and
  "nullable" (must be present, yet allowed to hold no value). Trigger on
  "必須", "required", "nullable", "Optional", "NOT NULL" showing up in a diff,
  or when a new Field()/table column/request-response shape is being written.
---

# 必須なのにnullable、を作らない

フィールドの状態は次の2つのどちらかであるべきで、両方の性質を同時に持たせない。

- **必須・non-nullable**: 呼び出し側は必ずキーを渡し、値は必ず意味のある実値。
- **任意**: 呼び出し側はキーを省略できる。省略時のデフォルト（多くは`None`）が
  明示されている。

避けるべきなのは「**必須なのにnullable**」＝ キー自体は必ず渡さないといけないが、
値としては`None`も許される、という中途半端な状態。これは「本当は任意にしたい
だけなのに書き忘れた」か「nullに意味を持たせたいなら、それは実質デフォルト値
なので任意にすべき」のどちらかで、ほぼ確実にバグかAPI設計の不備。

## このリポジトリでの具体的な地雷: Pydantic v2

`backend/requirements.txt` は pydantic v2 系（fastapiの依存で2.13系が入る）。
**v1と違い、v2では `Optional[X]` と書いても暗黙に `= None` にはならない。**
`default` を明示しない限りそのフィールドは「必須」になる（値としてはNone可、
だがキー自体は必ず要る）。実際に確認済み:

```python
class M(BaseModel):
    rank: Optional[int] = Field(description="x")

M()  # → ValidationError: rank Field required [type=missing]
```

`schemas.py` には既にこのパターンが複数残っている（`Contribution.rank` /
`metric_value` / `rrf_term`、`EvalResult.rank`、`EvalReport.retrievers` /
`rerank` など）。今のところ全部レスポンス型で、サーバー側が常に全フィールドを
明示的に組み立てているので実害は出ていないが、リクエスト型で同じ書き方をすると
即バリデーションエラーになる。**新しいフィールドを足すときにこのパターンを
コピーしない。**

### 書き方のルール

- 本当に必須（常に値がある）→ `Optional`/`| None` を付けない。
  例: `source: str = Field(description="...")`
- 本当に任意（省略可・未指定の意味がある）→ `Optional[X] = Field(default=None,
  ...)` と `default` を必ず明示する。
  例: `project: Optional[str] = Field(default=None, description="プロジェクト（任意）")`
- 「nullには"設定の既定値を使う"のような積極的な意味がある」場合
  （例: `EvalReport.retrievers` の「null=設定の既定」）も、結局は
  **省略可能にすべき**なので `default=None` を付ける。null自体に意味を持たせる
  ことと、フィールドを必須にすることは別の話。

### レビュー時のセルフチェック

`Optional[` または `| None` を書いたら、同じ行 or 近くに `default=` か
`= None` があるか必ず確認する。無ければバグ。

```bash
# 見落としチェック（Field(...)にdefaultが無いOptionalを探す）
grep -n "Optional\[" backend/app/schemas.py
```
（`default=`が無い行がヒットしたら、意図的か確認して直す）

## DBカラム（backend/app/db.py）

`NOT NULL` = 必須、無指定 = nullable（任意）というのは素直に対応が付くので、
Pydantic ほどの落とし穴は無い。ただし既存のコードは「nullableにする理由」を
必ずコメントで書く文化がある（例: `chunks.embedding` は「複数モデル併存・遅延
埋め込みの自由度を残すため」、`documents.project`/`topic` は「NULL=共通文書」）。
これを踏襲する:

- 新しいカラムを追加するときは、必須なら `NOT NULL`（可能なら `DEFAULT` も
  検討）、任意ならnullableのままにして **NULLが何を意味するかを1行コメントで
  書く**。理由なきnullable列も、理由なきNOT NULL強制も避ける。
- アプリ側が挿入時に必ず値を持てないカラムをNOT NULLにしない（既存の
  `chunks.document_id` のように「NULLだと孤児化して検索から黙って外れる」
  というような、nullableだと**実害がある**理由がある場合はNOT NULL）。

## フロントエンド（TypeScript）

`frontend/app/api-types.ts` は `/openapi.json` から `openapi-typescript` で
自動生成される（`npm run gen:types`）。つまり型の出どころは常に
`schemas.py` 側。フロントで直接 `field?: T | null` のような手書き型を増やす
のではなく、**大元のPydanticモデル側で必須/任意を正しく表現し、型生成をやり直す**
のが直すべき場所。手書きの型（`frontend/app/page.tsx` 内のローカル型など）を
足す場合も同じ基準（必須なら`?`を付けない、任意なら`?`を付けて呼び出し側の
省略を許す）で揃える。
