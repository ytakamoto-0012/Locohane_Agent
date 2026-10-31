"""設定ダッシュボード（管理ツール）。

Locohane本体（app.py、config.ini）とは別プロセス・別ポートで動く独立した
FastAPIアプリ。ブラウザから config.ini の値をキー単位で閲覧・変更したり、
本体（複数インスタンス）の起動・停止・再起動を行える。

config.ini 自体は既定値として扱い書き換えない。変更値は
instances/<name>/config_overrides.json に記録し、src.config.load_config() が
起動時にそれを読んで既定値を上書きする（優先度: 環境変数 >
config_overrides.json > config.ini。詳細は README_DETAIL.md「設定ダッシュボード」参照）。
"""
