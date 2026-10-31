# office_shared（意図的にSKILL.md無し）が起動のたびWARNINGとして記録され続ける

- **区分**: 問題点 → 修正済み
- **検知日時**: 2026-08-22 20:35:42
- **対象ログファイル**: data/logs/app_20260822_203542.log（同種の警告は
  app_20260822_195744.log 等、監視を始めた全ログファイルの起動直後に
  毎回出現）

## 経緯

`src/skills.py` の `_scan_one()`（125-134行目）は `skills/` 配下の各
サブディレクトリを走査し、`SKILL.md` が無ければ無条件で
`logger.warning("SKILL.md が無いためスキップ: %s", entry.name)` を出す。

`skills/office_shared/`（`docx_common.py`/`excel_common.py`/
`pptx_common.py`/`office_theme.py` を置く共用コードディレクトリ）は
`skills/OFFICE_SKILLS_README.md`（103-104行目）に明記されている通り
**意図的に** `SKILL.md` を持たない設計であり、「設定ミス」ではない。
にもかかわらず、アプリ起動・スキル再走査のたびに必ずこのWARNINGが
記録され続けていた（今回監視した2つの起動ログいずれでも複数回出現）。

## ログ引用

```
2026-08-22 20:35:42,013 WARNING src.skills: SKILL.md が無いためスキップ: office_shared
2026-08-22 20:35:43,711 WARNING src.skills: SKILL.md が無いためスキップ: office_shared
2026-08-22 20:35:51,076 WARNING src.skills: SKILL.md が無いためスキップ: office_shared
```

## 推定原因（特定済み）

`_scan_one()` が「SKILL.md 不在＝異常（設定ミス・壊れたスキート）」として
一律WARNING扱いしており、「意図的にSKILL.mdを持たない共用ディレクトリ」を
区別する仕組みが無かった。`office_shared`は`skills/OFFICE_SKILLS_README.md`で
正式に定義された恒久的な設計（B2方式）であり、今後もこのWARNINGは毎回
（アプリ起動のたび、Chainlitのセッション再走査のたびなど）出続ける。

このスキル自身（monitor-app-log）の運用上も実害があった。WARNINGとして
永続的にログへ出続けるため、5分おきの自動監視が毎回これを拾ってしまい
（実際には過去に別issueとして起票されたことは無いが、本来のバグ検知を
埋もれさせるノイズとして機能し続けていた）。

## 対応（修正済み）

`src/skills.py` に既知の非スキル・共用ディレクトリ名の許可リスト
`_KNOWN_NON_SKILL_DIRS`（現状 `{"office_shared"}`、
`skills/OFFICE_SKILLS_README.md`の記述を典拠として参照）を追加し、
このリストに含まれるディレクトリはWARNINGではなくDEBUGでログするよう
`_scan_one()` を修正。リストに無い未知のディレクトリは従来通りWARNING
のまま（設定ミスの早期検知を維持）。

`tests/test_skills_scan.py` を新規作成し、
- `office_shared`相当のディレクトリでSKILL.md不在時にWARNINGが出ない
  （DEBUGで出る）こと
- 未知のディレクトリ名では従来通りWARNINGが出ること

の2点を回帰テストとして追加。`pytest tests/` 356件全通過を確認
（1件のUNCパステストの不安定挙動は本修正と無関係の既存の環境依存フレーク
であることを、修正前後どちらの状態でも再現・非再現することを確認して
切り分け済み）。

## ユーザー回答

ここにはユーザーの回答が記述される
