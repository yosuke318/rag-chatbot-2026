"""テスト共通の準備。

app.llm は import 時点で anthropic / voyageai のクライアントを生成する
（キーが無いと構築時に落ちる実装）。ここで先にダミーキーを入れておくことで、
DB や外部APIを一切呼ばない純ロジックのテストでも app パッケージを import できる。
クライアントを構築するだけでは通信は発生しないため、実キーは不要。
"""
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-for-tests")
os.environ.setdefault("VOYAGE_API_KEY", "dummy-key-for-tests")
