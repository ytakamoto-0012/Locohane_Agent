# .locohane/skills/

ビルトインの `skills/` を汚さずに置ける、ユーザー独自スキル用のディレクトリ。
`skills/` とマージ走査され、同名スキルが両方に存在する場合はこちら側が優先される
（`src/skills.py` の `scan_skills()` 参照）。

ディレクトリ構成・`SKILL.md` の書き方は `skills/SKILLS_README.md` と同じ。
