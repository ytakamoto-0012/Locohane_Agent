---
name: apply-memory-to-skills
description: Locohane の指定インスタンスの永続メモリー（../Locohane_Agent-Settings/data/<インスタンス名>/memory/ の共通分と、ユーザー別 users/<ユーザー>/ のうち共有（visibility: shared）のもの）を読み、そのインスタンスで使用可能なスキルのうち関係するスキルの改善・効率化につながる内容があれば、そのスキルの SKILL.md に反映し、関係する evals/cases のケースで tune-prompt の評価ループを回す。「メモリーをスキルに反映して」「メモリーからSKILL.mdを改善して」「/apply-memory-to-skills」等で使う。ケースが無い場合の新規作成は create-eval-case、メモリー同士の重複統合は consolidate-memory の担当。
---

# apply-memory-to-skills: 永続メモリーのSKILL.mdへの反映とtune-prompt

Locohane のエージェントは、作業中に得た教訓（「excel-read は `--columns` で列を絞ると
速い」等）を `<memory_dir>/{user,feedback,project,reference}/*.md` に永続メモリーとして
残す（`src/memory.py`）。メモリーは次回以降そのインスタンスでしか参照されず、
`search_memory` で引けなければ活かされない。本スキルは、スキルの使い方に関する
汎用的な教訓をメモリーから SKILL.md 本体へ移し、全インスタンスで最初から効くようにする。
反映後は関係するevalケースで `tune-prompt` を回し、劣化していないことを確かめる。

## 状態ファイル

`.claude/state/apply-memory-to-skills/state.json`（プロジェクトルート基準）:

```json
{"test1": {"last_checked": "2026-10-03T09:00:00"}}
```

- キーはインスタンス名。`last_checked` は前回このスキルでメモリーを確認し終えた時刻
  （ISO形式、ローカル時刻）。
- ファイルが無い/壊れている/該当インスタンスのキーが無い場合は、全メモリーを対象にする。

## 手順0: インスタンスを選ぶ（毎回必須）

**他の作業より先に行う。** 依頼文でインスタンスが指定されていれば、それを使う。
指定が無ければ、`<instances_dir>`（`config.ini` の `[admin] instances_dir`。既定 `../Locohane_Agent-Settings/instances`。実際の場所はプロジェクトルートで `"<PYTHON_DIR>\Scripts\python.exe" -c "from src.config import resolve_instances_root; print(resolve_instances_root())"` で取得する） 直下のディレクトリ名（`admin_changes.log` 等のファイルは
除く。`<instances_dir>` が無ければ `default` のみ）を選択肢（`multiSelect: false`）にして
`AskUserQuestion` で1つ選ばせる。以降 `<instance>` はこの名前を指す。

## 手順1: memory_dirと使用可能スキルを取得する

次のコマンドで、インスタンス適用後の実効 `memory_dir` と使用可能スキルの一覧を取得する
（インスタンス別 `.env`・`config_overrides.json`・`${instance}` の解決を
`apply_instance()`/`load_config()` に任せる。パスを手で組み立てない）。`python_env.bat` の `PYTHON_DIR` の
Python（`<PYTHON_DIR>\Scripts\python.exe`）を使い、プロジェクトルートで実行する:

```
"<PYTHON_DIR>\Scripts\python.exe" -c "from evals.instance import apply_instance; apply_instance('<instance>'); from src.config import load_config; from src.skills import scan_skills; c = load_config(); print('memory_dir:', c.memory_dir); [print(s.name, s.skill_md_path) for s in scan_skills([c.skills_dir, *c.locohane_skills_dirs])]"
```

- 出力されたスキル（名前と SKILL.md のパス）が「そのインスタンスで使用可能なスキル」。
  ここに無いスキルは対象外（メモリーに出てきても編集しない）。
- `memory_dir` が存在しない、またはメモリーが0件なら、その旨を報告して終了する。

## 手順2: メモリーを読む

1. 共通の `memory_dir/{user,feedback,project,reference}/*.md` と、ユーザー別の
   `memory_dir/users/<ユーザー>/{user,feedback,project,reference}/*.md` のうち frontmatter が
   `visibility: shared` のものを `Glob`/`Read` で読む（`MEMORY.md` 索引は読まなくてよい）。
   `visibility: private`（または `visibility` 無し）のユーザーのメモリーは**読まない・反映しない**
   （本人以外に見せない約束のメモリーで、`skills/` は全インスタンス共有かつ公開リポジトリのため。
   報告にも件数以外は書かない）。
2. `state.json` の `last_checked` があれば、frontmatter の `updated`（無ければ
   `created`）がそれより新しいメモリーだけを候補にする。

## 手順3: スキルと関係づけ、反映するか判断する

候補メモリーごとに本文を実際に読み、次を**全て**満たすものだけ反映対象にする。
迷うものは反映しない（見送り理由を報告に書く）。

1. **関係するスキルが特定できる**: 手順1の一覧のスキル名、そのスキルの
   `scripts/` 配下のファイル名（例: `read_excel.py` → `excel-read`）、
   SKILL.md に書かれたオプション名等で、どのスキルの話か1つ（または少数）に絞れる。
   スキルと無関係な内容（ユーザーの業務情報・個人的な好み・ファイル置き場所等）は対象外。
2. **改善・効率化につながる**: 呼び出し回数が減る、失敗・再試行が減る、誤用を防ぐ、
   より良い結果が出る、のいずれか。
3. **汎用的である**: `skills/` は全インスタンス共有なので、特定の利用者・特定の
   ファイルだけに効く話ではなく、そのスキルを使う誰にでも当てはまる。
4. **SKILL.md にまだ書かれていない**: 対象 SKILL.md を読み、同じ内容が既にあれば
   反映不要（重複して書かない）。既存記述と矛盾する場合は、メモリーの根拠が
   具体的で新しいときだけ書き換える。
5. **SKILL.md の記述で解決できる**: スクリプト本体の不具合・機能不足が原因なら、
   `scripts/` は直さずユーザーに報告するだけにする。

反映対象が1件も無ければ、見送ったメモリーと理由を報告して終了する
（`state.json` は手順6どおり更新する）。

## 手順4: SKILL.md に反映する

対象スキルごとに:

1. 反映前の SKILL.md の内容を控えておく（手順5で `evals/history/` へ退避する
   ため。`skills/` は git 管理下なので `git diff` でも差分を確認できる）。
2. `Edit` で最小限だけ追記・修正する。書き方は低パラメータモデル向けにする:
   - 一文を短くし、経緯説明・メモリーの引用・長い具体例は書かない。
   - 「ルール＋代替行動」の形にする（例:「列が多い表は `--columns` で必要な列だけ読む」）。
   - 呼び出し例を足すときは、単独オプションの例だけでなく、既存オプションとの
     組み合わせ例も示す。
   - 汎用節より、そのオプション・手順を説明している具体的な節に書く。
3. frontmatter の `description` は、スキルの用途自体が変わる場合を除き変更しない。
4. 1スキルに複数メモリーを反映する場合も、1つの SKILL.md への変更としてまとめてよい
   （tune-prompt の1イテレーション1箇所ルールは、手順5のループ内の修正に適用する）。

## 手順5: 関係するケースで tune-prompt を回す

1. **関係ケースの特定**: `evals/cases/*/`（`config_timeouts` は除く）の yaml を
   `Grep` し、反映したスキル名・そのスクリプトファイル名・反映したオプション名を
   `turns`/`expect`/`judge` に含むケースを関係ケースとする。`tune-prompt` の対応表で
   そのスキルが既定対象になっているケース群（例: `excel-skills` → `excel-read`）の
   ケースも関係ケースに含める。`system_prompt_scale` のケースは重いので、関係が
   あっても `AskUserQuestion` で実行するか確認する。
2. **関係ケースが無い場合**: tune-prompt は回さない。反映内容と「関係ケースが無い」旨を
   報告し、`create-eval-case` でケースを作るかを `AskUserQuestion` で確認する
   （作る場合は create-eval-case を呼ぶ。作成後に本手順5へ戻る）。
3. **tune-prompt の実行**: `.claude/skills/tune-prompt/SKILL.md` を読み、その手順1〜2を
   実行する。手順0（質問）は次の値で決定済みとして省略する:
   - インスタンス: `<instance>`
   - ケース群・ケース: 手順5-1で特定したもの（ケース群が複数なら1つずつ順に）
   - チューニング対象ファイル: 手順4で編集した SKILL.md
   - 評価に入る前に、手順4-1で控えた**反映前**の内容を
     `evals/history/<target>/memapply_<YYYYMMDD>_before_<スキル名>.md`、反映後の内容を
     `memapply_<YYYYMMDD>_after_<スキル名>.md` として退避する（既存の `iter*` ファイルは
     過去のループの記録なので上書きしない）。
   - `evals/tuning_log.md` に「### memapply <YYYYMMDD> (instance: <instance>)」の
     見出しで、反映したメモリーの name と変更箇所を追記してから評価に入る。
4. tune-prompt が llama-server 未起動等で評価できずに終わった場合は、SKILL.md の
   反映は残したまま、その旨を報告する（反映を戻すかはユーザーに任せる）。
5. tune-prompt のループで反映内容が原因の不合格が直らず振動した場合は、反映を
   取り消すべきか `AskUserQuestion` で確認する。

## 手順6: 状態更新と報告

1. `state.json` の `<instance>.last_checked` を、手順2でメモリーを読み始めた時刻で
   更新する（他インスタンスのキーは消さない）。
2. 次を報告する:
   - 反映したメモリー（name）→ 反映先スキル・変更内容の要約
   - 見送ったメモリーと理由（無関係／インスタンス固有／既に記載済み／スクリプト側の問題 等）
   - tune-prompt の結果（ケースごとの合否、イテレーション数）

## 安全策

- 手順1の一覧に無いスキルの SKILL.md、`scripts/` 配下、`system_prompt.md`、
  アプリ本体のコードは編集しない（tune-prompt のループ中も、対象ファイルは
  反映先の SKILL.md だけ）。
- Locohane の永続メモリー（`memory_dir` 配下）は読むだけで、編集・削除しない。
- メモリー本文を SKILL.md にそのまま貼り付けない。個人名・業務データ・ファイルパス等の
  インスタンス固有情報を SKILL.md に持ち込まない。
- git へのコミット・ステージングはしない（ユーザーが差分を確認してから判断する）。
