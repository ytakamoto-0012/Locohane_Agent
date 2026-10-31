"""スキル研究室（管理ツールの「スキル研究室」タブ）。

研究テーマ（開発中のスキル・サブエージェントと、それらにまたがるケースのまとまり）を、
対象インスタンス（組織別・役割別の Locohane 本体）ごとに管理する。

- themes.py: テーマの置き場（対象インスタンスの ${common_data_dir}/skill_lab/themes/）・
  ファイル操作・状態の管理
- sources.py: 取り込める利用者のドラフト・正式スキル・正式エージェントの一覧
- evaluation.py: テーマの評価（evals/run_all.py に全資産を重ね、研究室の LLM 接続先で実行）
- llm.py / fixer.py / judge.py / drafting.py: 研究室の LLM による修正・判定・下書き
- worker.py: スキル調整ワーカー（kind=skill_lab）として起動されるワーカー
- promotion.py: 昇格（対象インスタンス専用の instance_locohane_dir へ配置）・差し戻し・アーカイブ
"""
