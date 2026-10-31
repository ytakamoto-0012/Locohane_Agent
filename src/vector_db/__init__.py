"""ベクターDB（社内文書の意味検索）。

文書を取り込んで（ingest.py）チャンクごとの埋め込みベクターを保存し（store.py・
sqlite_vec_store.py・qdrant_store.py）、検索語に近いチャンクを返す（search.py）。
コレクションの定義と取り込み済みファイルの記録はバックエンドによらず catalog.py が
[vector_db].dir/catalog.sqlite に持つ。

LLM 向けのツールは src/tools/vector_db_tools.py、取り込み操作は設定ダッシュボードの
「ナレッジ」タブ（admin/knowledge.py）と CLI（python -m src.vector_db.ingest）から行う。
"""
