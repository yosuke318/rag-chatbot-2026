"""取り込み: テキスト → チャンク分割 → 文脈付与 → 埋め込み → pgvector へ保存。

分割は app.chunking（見出し・条文の構造で切る）、
文脈付与は app.llm.generate_chunk_contexts（contextual retrieval）に任せ、
ここは「その2つを繋いでDBに入れる」役に徹する。
再取り込みは content_hash で差分検知し、内容が変わっていなければ
埋め込み・文脈生成のAPI呼び出しごと省く（content_hash 関数を参照）。

文書内の画像はテキストとは別の流れで扱う: 抽出→S3保存→画像チャンクとして
登録（store_images）。埋め込みAPIを呼ばないので、本文が変わっていない
再取り込みでも実行する（この機能より前に登録した文書を入れ直せばよい、という
移行経路を作るため）。
"""
from __future__ import annotations

import hashlib
import logging
import os

from app import parsers, scopes, storage
from app.chunking import Chunk, split_chunks
from app.config import (
    CHUNK_MAX_CHARS,
    CHUNK_MIN_CHARS,
    CHUNK_OVERLAP,
    CHUNKING_VERSION,
    EMBED_MODEL,
    EXTRACT_IMAGES,
    IMAGE_INDEX_METHOD,
    USE_CONTEXTUAL_CHUNKING,
)
from app.db import get_conn
from app.keywords import noun_text
from app.llm import (
    embed_images,
    embed_texts,
    generate_chunk_contexts,
    generate_image_captions,
)

logger = logging.getLogger(__name__)


class UnsupportedFileType(Exception):
    """まだテキスト抽出に対応していない拡張子。呼び出し側は415で返す。"""

    def __init__(self, ext: str):
        self.ext = ext
        super().__init__(ext)


# テキスト系はここでデコード、バイナリ文書(PDF/XLSX/PPTX)は parsers 側で抽出。
# 拡張子なしは「プレーンテキスト」とみなす。
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ""}


def _decode_text(data: bytes) -> str:
    """テキストファイルのバイト列を文字列にする。

    UTF-8 を第一に、日本語のレガシーファイル向けに cp932 を代替として試す。
    """
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("テキストとして読み取れませんでした（文字コード不明）")


def extract_text(filename: str, data: bytes) -> str:
    """アップロードされたファイルのバイト列から本文テキストを取り出す。

    - テキスト系(.txt/.md/.csv 等): デコードする
    - PDF/XLSX/PPTX: parsers のパーサで抽出する
    - それ以外: UnsupportedFileType（呼び出し側で415）

    解析はできたが本文が空（例: スキャンPDF）の場合は空文字を返し、
    「本文が取り出せなかった」判断は呼び出し側に委ねる。
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext in TEXT_EXTENSIONS:
        return _decode_text(data)
    parser = parsers.PARSERS.get(ext)
    if parser is None:
        raise UnsupportedFileType(ext)
    return parser(data)


def extract_images(filename: str, data: bytes) -> list[parsers.ExtractedImage]:
    """アップロードされたファイルから画像を取り出す（PDF/XLSX/PPTX）。

    extract_text と違い★例外を投げない★。画像が無い形式（.txt 等）も、解析に
    失敗した場合も空リストを返す。図が取れなくても文書としては成立するので、
    ここで取り込み全体を止めない、という判断（詳細は app.parsers の方針）。
    EXTRACT_IMAGES=false なら常に空（画像機能を丸ごと止めるスイッチ）。
    """
    if not EXTRACT_IMAGES:
        return []
    ext = os.path.splitext(filename)[1].lower()
    extractor = parsers.IMAGE_EXTRACTORS.get(ext)
    if extractor is None:
        return []
    return extractor(data)


# 画像の検索対象化の方式（config.IMAGE_INDEX_METHOD が取りうる値）。
# API境界での検証にも使う（/admin/reindex-images?method=...）。
IMAGE_INDEX_METHODS = ("caption", "multimodal", "none")


def _image_placeholder(label: str) -> str:
    """索引を作らない（作れなかった）画像チャンクの content。

    content は NOT NULL なので、本文の代わりに由来のわかるラベルを置く。
    名詞も埋め込みも付かないため、この状態の画像は検索に出ない。
    """
    return f"[画像] {label}"


def build_image_index(
    source: str,
    images: list[parsers.ExtractedImage],
    method: str | None = None,
    retry_waits: list[int] | None = None,
) -> list[dict]:
    """画像を「テキストの質問で引ける」形にする。images と同じ長さを返す。

    各要素は chunks に入れる索引用の値:
      {"content", "context", "content_nouns", "embedding", "image_embedding"}

    方式は IMAGE_INDEX_METHOD（引数 method で上書き可・eval の比較評価用）:
      caption    … 案A: Claudeに説明文を書かせ、既存のテキスト経路で埋め込む。
                   説明文は content に入る＝ベクトル・字面・BM25の3手法すべてに乗る。
      multimodal … 案B: 画像を直接ベクトル化して image_embedding に入れる。
                   content は説明文を持たないので、当たるのは image 検索だけ。
      none       … 索引を作らない（保管のみ）。

    ★失敗しても取り込みを止めない★。APIキー未設定・レート制限・APIエラーは
    すべて「索引なし（＝検索に出ない画像）」に落として警告ログにとどめる。
    図が引けないのは困るが、そのために文書登録ごと失敗させる方がもっと困る。

    retry_waits: 埋め込みAPIが429を返したときに待つ秒数の並び（None=待たない）。
      ★評価では必ず渡すこと★。ここが429で失敗すると索引なしの画像が並び、
      「その方式では図が引けなかった」という実測値と区別が付かない数字が出る
      （実際に踏んだ。compare_image_index_methods が indexed 件数を検査するのはこのため）。
    """
    resolved = (method or IMAGE_INDEX_METHOD).lower()
    # context には★常にラベルを入れる★（説明文が付いたかどうかに関わらず）。
    # 「文書内での位置づけ」というテキストチャンクと同じ意味づけであり、かつ
    # 索引を作り直す(reindex_images)ときに「何ページ目の図か」を復元する唯一の
    # 手がかりになる（content は説明文で上書きされてラベルが消えるため）。
    blank = [
        {
            "content": _image_placeholder(img.label),
            "context": img.label,
            "content_nouns": None,
            "embedding": None,
            "image_embedding": None,
        }
        for img in images
    ]
    if resolved not in IMAGE_INDEX_METHODS:
        # 設定のtypoで黙って案Aに落ちると、案Bを測っているつもりの評価が
        # 案Aの数字を返す。索引は作らず、はっきり警告に出す。
        logger.warning(
            "未知の画像索引方式です（画像は保管のみになります）: %s / 利用可能: %s",
            resolved,
            ", ".join(IMAGE_INDEX_METHODS),
        )
        return blank
    if not images or resolved == "none":
        return blank

    try:
        if resolved == "multimodal":
            vectors = embed_images(
                [img.data for img in images], retry_waits=retry_waits
            )
            for row, vec in zip(blank, vectors):
                row["image_embedding"] = vec
            return blank

        # 案A（既定）: 説明文 → 既存のテキスト経路
        captions = generate_image_captions(
            [(img.data, img.content_type, img.label) for img in images], source
        )
        # 説明文が書けた画像だけを埋め込む。失敗分(空文字)を混ぜると
        # 「ラベルだけのベクトル」ができ、無関係な質問に当たりはじめる。
        indexable = [i for i, c in enumerate(captions) if c.strip()]
        if not indexable:
            return blank
        # 埋め込むのは「ラベル + 説明文」。テキストチャンクが文脈を前置するのと
        # 同じ狙いで、「何ページ目の図か」を検索側から当てられるようにする。
        embeddings = embed_texts(
            [_embed_source(images[i].label, captions[i]) for i in indexable],
            input_type="document",
            retry_waits=retry_waits,
        )
        for i, vec in zip(indexable, embeddings):
            caption = captions[i].strip()
            blank[i]["content"] = caption
            blank[i]["content_nouns"] = noun_text(
                _embed_source(images[i].label, caption)
            )
            blank[i]["embedding"] = vec
        return blank
    except Exception:
        logger.warning(
            "画像の索引作成に失敗しました（方式=%s・画像は保管のみになります）: %s",
            resolved,
            source,
            exc_info=True,
        )
        return blank


def store_images(
    document_id: int,
    source: str,
    images: list[parsers.ExtractedImage],
    index_method: str | None = None,
) -> int:
    """画像の原本を S3 に保存し、chunks に画像チャンクとして登録する。保存件数を返す。

    画像チャンクは image_path（S3キー）を持つ行。検索に当てるための索引
    （説明文＋埋め込み、または画像ベクトル）は build_image_index が決める。

    ★S3に保存できた画像だけをDBに入れる★。行だけ作ると、実体の無いキーを指す
    画像チャンクが残り、回答生成で毎回取得に失敗することになるため。
    索引作成（Claude/Voyage を呼ぶ）はS3保存の後に回す ＝ 保存できなかった画像に
    APIコストを払わない。
    S3未設定なら画像は扱わない（0件）。

    同じ文書の画像チャンクは毎回まるごと入れ替える。再取り込みで同じ絵が
    二重に積み上がるのを防ぐ（テキストチャンク側の「消してから入れ直す」と同じ方針）。
    """
    if not images:
        return 0
    if not storage.is_enabled():
        logger.info("S3が未設定のため文書内画像は保存しません: %s", source)
        return 0

    stored: list[tuple[str, parsers.ExtractedImage]] = []  # (S3キー, 画像)
    for i, image in enumerate(images, start=1):
        key = storage.image_key(source, i, image.ext)
        if storage.save_bytes(key, image.data, image.content_type):
            stored.append((key, image))
    if not stored:
        return 0

    index = build_image_index(source, [img for _, img in stored], index_method)

    with get_conn() as conn:
        with conn.transaction():
            conn.execute(
                "DELETE FROM chunks WHERE document_id = %s AND image_path IS NOT NULL",
                (document_id,),
            )
            # chunk_index はテキストチャンクの続き番号にする。テキストと画像で
            # 番号が衝突すると、chunk_index 順に並べる処理（原本の復元など）で
            # 順序が入れ替わるため。
            next_index = conn.execute(
                "SELECT COALESCE(MAX(chunk_index), -1) + 1 FROM chunks "
                "WHERE document_id = %s",
                (document_id,),
            ).fetchone()[0]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks "
                    "(document_id, chunk_index, content, context, content_nouns, "
                    " embedding, image_embedding, image_path) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            document_id,
                            next_index + n,
                            row["content"],
                            row["context"],
                            row["content_nouns"],
                            row["embedding"],
                            row["image_embedding"],
                            key,
                        )
                        for n, ((key, _img), row) in enumerate(zip(stored, index))
                    ],
                )
    return len(stored)


def _indexed_count(index: list[dict]) -> int:
    """索引が実際に付いた画像の枚数（どちらかのベクトルを持つ行）。

    ★0件は「引けない画像が並んだ」ということ★。方式の実力ではなく
    APIの失敗でもこうなるので、呼び出し側が区別できるよう件数を返す。
    """
    # 両方とも「値があるか」で数える。片方を真偽値で見ると、空ベクトル([])が
    # 返ったときに索引済みなのに0件と数えてしまう（and/or の優先順位も紛らわしい）。
    return sum(
        1
        for row in index
        if row["embedding"] is not None or row["image_embedding"] is not None
    )


def reindex_images(
    method: str | None = None, retry_waits: list[int] | None = None
) -> dict:
    """既存の画像チャンクの索引だけを作り直す。
    {"documents", "images", "indexed"} を返す。

    ★索引方式の比較評価を回すための道具★。索引方式は取り込み時に決まるので、
    素直にやると方式を変えるたびに全ファイルを上げ直すことになる。原本画像は
    S3にあるのだから、そこから読み直して索引だけ差し替えれば足りる。
    説明文を書き直したい（プロンプトを変えた）ときにも使う。

    method を省略すると現在の設定(IMAGE_INDEX_METHOD)。
    S3から取れなかった画像は飛ばす（その行は前の索引のまま残る）。

    indexed は★実際に索引が付いた枚数★。images と食い違っていたら、その分は
    レート制限やAPIエラーで索引を作れていない ＝ 検索に出ない。評価の前に
    ここを見ないと「その方式では引けなかった」という結論を誤って出す。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.id, c.image_path, c.context, d.source "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE c.image_path IS NOT NULL "
            "ORDER BY d.source, c.chunk_index"
        ).fetchall()

    # 文書ごとにまとめる。build_image_index は文書名を説明文の手がかりに使うのと、
    # 1文書ぶんをまとめて埋め込みAPIに渡してリクエスト数を減らすため。
    by_source: dict[str, list[tuple[int, str, str]]] = {}
    for chunk_id, image_path, context, source in rows:
        by_source.setdefault(source, []).append((chunk_id, image_path, context or ""))

    documents = 0
    updated = 0
    indexed = 0
    for source, items in by_source.items():
        fetched: list[tuple[int, parsers.ExtractedImage]] = []
        for chunk_id, image_path, label in items:
            obj = storage.get_object(image_path)
            if obj is None:
                logger.warning(
                    "原本画像を取得できませんでした（この画像は飛ばします）: %s", image_path
                )
                continue
            data, content_type = obj
            fetched.append(
                (
                    chunk_id,
                    parsers.ExtractedImage(
                        data=data,
                        ext=os.path.splitext(image_path)[1],
                        content_type=content_type,
                        label=label or "画像",
                        # 幅・高さは索引作成に使わない（足切りは抽出時に済んでいる）
                        width=0,
                        height=0,
                    ),
                )
            )
        if not fetched:
            continue

        index = build_image_index(
            source, [img for _, img in fetched], method, retry_waits
        )
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE chunks SET content = %s, context = %s, "
                    "content_nouns = %s, embedding = %s, image_embedding = %s "
                    "WHERE id = %s",
                    [
                        (
                            row["content"],
                            row["context"],
                            row["content_nouns"],
                            row["embedding"],
                            row["image_embedding"],
                            chunk_id,
                        )
                        for (chunk_id, _img), row in zip(fetched, index)
                    ],
                )
        documents += 1
        updated += len(fetched)
        indexed += _indexed_count(index)

    return {"documents": documents, "images": updated, "indexed": indexed}


def build_contexts(
    text: str, chunks: list[Chunk], contextual: bool | None = None
) -> list[str]:
    """各チャンクに前置する文脈を決める。チャンクと同じ長さのリストを返す。

    contextual=True なら Claude に文書全体を読ませて位置づけを書かせ、
    False なら見出しの階層（「第2章 休暇 > 第5条 年次有給休暇」）で代用する。
    Claude 側が失敗した（空が返った）チャンクも見出しにフォールバックするので、
    APIキー未設定やレート制限で取り込み自体が止まることはない。
    """
    use_llm = USE_CONTEXTUAL_CHUNKING if contextual is None else contextual
    generated = (
        generate_chunk_contexts(text, [c.text for c in chunks])
        if use_llm
        else [""] * len(chunks)
    )
    return [g.strip() or c.heading for g, c in zip(generated, chunks)]


def _embed_source(context: str, content: str) -> str:
    """埋め込み・字面検索に渡すテキスト（文脈を本文の前に置いたもの）。"""
    return f"{context}\n\n{content}" if context else content


def content_hash(text: str, contextual: bool) -> str:
    """再取り込みで作り直しが要るかを判定するキー（documents.content_hash）。

    本文だけでなく「埋め込み結果を左右する入力」をすべて混ぜる。本文だけで
    判定すると、設定を変えて取り込み直したのに古い埋め込みが残る:
      - contextual: app.compare は同じ文書を False/True で入れ直して比較するので、
        本文だけのハッシュだと2回目がスキップされ比較が成立しない
      - EMBED_MODEL: モデルを差し替えたらベクトル空間ごと変わる
      - チャンクのサイズ設定と CHUNKING_VERSION: 切り方が変われば中身も変わる

    各要素は「長さ→中身」の順で流し込む。区切り文字で連結すると、本文にその
    区切りが混ざったときに項目の境目が動き、別々の入力が同じ並びに化けうる
    （今の項目は本文以外が固定値なので実際には作れないが、項目を足したときに
    その前提が崩れるのを避ける）。本文を丸ごとコピーせずに済む利点もある。
    """
    digest = hashlib.sha256()
    for part in (
        text,
        str(contextual),
        EMBED_MODEL,
        CHUNKING_VERSION,
        str(CHUNK_MAX_CHARS),
        str(CHUNK_MIN_CHARS),
        str(CHUNK_OVERLAP),
    ):
        encoded = part.encode("utf-8")
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
    return digest.hexdigest()


def ingest_text(
    source: str,
    text: str,
    project: str | None = None,
    topic: str | None = None,
    store_original: bool = True,
    contextual: bool | None = None,
    embed_retry_waits: list[int] | None = None,
    images: list[parsers.ExtractedImage] | None = None,
) -> dict:
    """1つの文書を取り込む（upsert）。

    戻り値は {"chunks_created", "replaced", "skipped", "images_stored"}。
    skipped=True なら内容が変わっていないので何も作り直しておらず、
    chunks_created は「今DBにある既存チャンク数」を表す。

    同じ source の文書が既にあれば削除してから入れ直す。
    （紐づく chunks は ON DELETE CASCADE で一緒に消える）
    再取り込みで同名文書が二重に積み上がり、検索結果が重複するのを防ぐ。

    ただし content_hash が既存と一致する場合は入れ直さない。埋め込みAPIも
    Claude（文脈生成）も呼ばずに即戻る（コストとレート制限の節約）。
    区分(project/topic)だけが変わっていた場合は、埋め込みは使い回して
    documents の行だけ更新する。

    store_original: 原本テキストを S3 に保存するか。テキスト貼り付け登録では
      本文＝原本なので True。ファイルアップロード(/ingest-file)では原本は
      元のバイナリ（PDF等）であって抽出テキストではないため False を渡し、
      原本バイナリの保存は呼び出し側(#4 の save_bytes)に任せる。

    contextual: チャンクへの文脈付与に Claude を使うか。None なら設定
      (USE_CONTEXTUAL_CHUNKING)に従う。eval で有無を比較するための引数。

    embed_retry_waits: 埋め込みAPIが 429 を返したときに待つ秒数の並び
      （None = 再試行しない）。文脈生成はこの前に済ませてあるので、
      待って再試行しても Claude を呼び直さない。

    images: 文書から抽出した画像（app.ingest.extract_images の戻り値）。
      原本バイナリを持つ /ingest-file だけが渡す。テキスト貼り付け登録には
      画像が無いので None。★skipped のときも保存する★ ─ 画像の保存には
      埋め込みAPIもClaudeも要らないので省く理由が無く、むしろこの機能より前に
      登録済みの文書を「同じファイルを入れ直すだけ」で画像対応にできる。
    """
    use_contextual = USE_CONTEXTUAL_CHUNKING if contextual is None else contextual
    new_hash = content_hash(text, use_contextual)

    # 区分をマスタへ写して id を得る（app.scopes 参照）。UIのセレクタはマスタを
    # 引くので、ここで登録しないと「文書は入ったのに区分を選べない」状態になる。
    # 差分検知の前に済ませるのは、スキップ経路でも区分の比較・更新に id が要るため。
    project_id, topic_id = scopes.register(project, topic)

    # 差分検知。分割より先に済ませ、変化が無ければ以降の処理ごと省く。
    # 接続はここで一度閉じる（この後の埋め込みAPIは秒単位で待つことがあり、
    # その間コネクションを掴んだままにしない）。
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, content_hash, project_id, topic_id "
            "FROM documents WHERE source = %s",
            (source,),
        ).fetchone()
        # content_hash が NULL の行＝この機能より前に入った文書。作り直して値を入れる。
        unchanged = existing is not None and existing[1] == new_hash
        if unchanged:
            document_id, _, current_project_id, current_topic_id = existing
            # 本文は同じで区分だけ変えた場合（登録し直しでの分類修正）。
            # 埋め込みは使い回せるので documents の行だけ更新する。
            if (current_project_id, current_topic_id) != (project_id, topic_id):
                conn.execute(
                    "UPDATE documents SET project_id = %s, topic_id = %s "
                    "WHERE id = %s",
                    (project_id, topic_id, document_id),
                )
            # チャンク数はスキップ時の戻り値にしか要らないので、ここでだけ数える
            # （作り直す場合は数えても捨てるだけなので、上のSELECTには含めない）。
            # 画像チャンクは数えない。chunks_created は「本文を何チャンクに割ったか」
            # を表す数字なので、新規登録時（len(chunks)）と意味を揃える。
            chunk_count = conn.execute(
                "SELECT count(*) FROM chunks "
                "WHERE document_id = %s AND image_path IS NULL",
                (document_id,),
            ).fetchone()[0]

    if unchanged:
        # 原本の保存だけは続ける。DBに文書があってもS3側だけ欠けている状態
        # （S3障害中に取り込んだ等）を、再取り込みで直せるようにするため。
        if store_original:
            storage.save_text(source, text)
        return {
            "chunks_created": chunk_count,
            "replaced": 0,
            "skipped": True,
            "images_stored": store_images(document_id, source, images or []),
        }

    chunks = split_chunks(text)
    if not chunks:
        return {
            "chunks_created": 0,
            "replaced": 0,
            "skipped": False,
            "images_stored": 0,
        }

    contexts = build_contexts(text, chunks, use_contextual)
    # 埋め込むのは「文脈 + 本文」。本文だけを埋め込むと、断片のままの
    # チャンクが質問のベクトルに当たらない（それが contextual retrieval の狙い）。
    embeddings = embed_texts(
        [_embed_source(ctx, c.text) for ctx, c in zip(contexts, chunks)],
        input_type="document",
        retry_waits=embed_retry_waits,
    )

    with get_conn() as conn:
        # 削除と再登録は一括で（途中で失敗しても文書が消えたままにならない）
        with conn.transaction():
            replaced = conn.execute(
                "DELETE FROM documents WHERE source = %s", (source,)
            ).rowcount

            document_id = conn.execute(
                "INSERT INTO documents (source, project_id, topic_id, content_hash) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (source, project_id, topic_id, new_hash),
            ).fetchone()[0]

            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks "
                    "(document_id, chunk_index, content, context, "
                    " content_nouns, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (
                            document_id,
                            i,
                            # content は原文のまま（回答生成にはこれを渡す）
                            chunk.text,
                            contexts[i],
                            # content_nouns: 字面検索用に名詞だけ抜き出したもの。
                            # 埋め込みと同じ「文脈+本文」から取り、BM25/trgm でも
                            # 文脈語（章名・条名）で当たるようにする
                            noun_text(_embed_source(contexts[i], chunk.text)),
                            embeddings[i],
                        )
                        for i, chunk in enumerate(chunks)
                    ],
                )

    # 原本を S3(MinIO) にも保存し、出典名からダウンロードできるようにする。
    # DBコミットの後に行う（S3が落ちていても取り込み自体は成立させる。best-effort）。
    if store_original:
        storage.save_text(source, text)

    return {
        "chunks_created": len(chunks),
        "replaced": replaced,
        "skipped": False,
        # 画像チャンクは chunks_created に数えない（「本文を何チャンクに割ったか」
        # という数字の意味を保つため）。DELETE→INSERT でテキストを入れ直した
        # 直後なので、この文書の画像チャンクはここで作られる分だけになる。
        "images_stored": store_images(document_id, source, images or []),
    }
