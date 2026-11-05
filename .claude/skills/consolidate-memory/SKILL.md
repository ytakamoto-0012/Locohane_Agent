---
name: consolidate-memory
description: Locohane の永続メモリー（全インスタンスの ../Locohane_Agent-Settings/data/<インスタンス名>/memory/ の共通分 {user,feedback,project,reference}/*.md とユーザー別 users/<ユーザー>/{type}/*.md）を1日1回棚卸しし、同一テーマ・重複する内容を1本に統合してマージすることで、似たタスクの改善案がバラバラなnameで分散するのを防ぐ。スキル自身がCronCreateで日次・durable:trueの自己スケジュールを登録し、以後はcron発火のたびに自分自身を再度呼び出す。「メモリーを整理して」「重複メモリーを統合して」「/consolidate-memory」等で使う。統合の要否はLLM自身が本文を読んで判断し、機械的な文字列一致だけでは統合しない。
---

# consolidate-memory: Locohane永続メモリーの重複統合

`Locohane` は永続メモリーを YAML frontmatter 付き Markdown として保存する（`src/memory.py`）。
メモリーはユーザー別で、`<memory_dir>/users/<ユーザー>/{user,feedback,project,reference}/*.md` に置かれ、
frontmatter の `visibility`（`private`＝本人だけ／`shared`＝全ユーザーが検索できる。無ければ private 扱い）を持つ。
ユーザー別になる前からある `<memory_dir>/{user,feedback,project,reference}/*.md` は「共通」のメモリー
（全ユーザーが読み書きできる）。`memory_dir` は
インスタンスごとに異なる（既定 `../Locohane_Agent-Settings/data/<インスタンス名>/memory`。`config.ini` の
`[paths] common_data_dir = ../Locohane_Agent-Settings/data/${instance}` 由来で、管理ツールの
`<instances_dir>/<name>/config_overrides.json` で上書きされている場合もある）。Locohane側のエージェントは `search_memory`
（キーワード部分一致のみ）で重複確認してから記録する運用だが、キーワードの言い換えや
低パラメータモデルの判断ミスで、同じテーマの改善案が別 name のメモリーとして分散して
しまうことがある。本スキルはこれをバックグラウンドで定期的に棚卸しし、重複・関連の強い
メモリー同士を1本に統合する。

## 状態ファイル

`.claude/state/consolidate-memory/state.json`（プロジェクトルート基準）:

```json
{"last_consolidated": "2026-09-04T09:00:00", "cron_job_id": "xxxxx"}
```

- `last_consolidated`: 前回統合チェックを完了した時刻（ISO形式、ローカル時刻）。
  ファイルが無い/壊れている場合は全件を対象に初回統合を行い、完了後に現在時刻で
  作成する。
- `cron_job_id`: このスキルが自己登録した `CronCreate` のジョブID。

## 手順

### 1. 自己スケジュールの確認・登録

1. `state.json` を読む（無ければ手順末尾の初期化を行う）。
2. `cron_job_id` があれば `CronList` で該当ジョブがまだ存在するか確認する。
3. ジョブが存在しない場合（初回起動、または登録から7日経過して自動失効した場合）
   のみ、以下の内容で `CronCreate` を呼び、返ってきたジョブIDを `state.json` の
   `cron_job_id` に書き込む。既にジョブが生きている場合は**絶対に再登録しない**
   （重複ジョブが並走してしまうため）。
   - `cron`: `"22 4 * * *"`（毎日、深夜4時22分。0分/30分ちょうどを避ける）
   - `recurring`: `true`
   - `durable`: `true`
   - `prompt`: `"/consolidate-memory を実行し、全インスタンスの永続メモリーの重複・関連を棚卸しして統合してください。"`
   - `reason`（分かればログ用に）: `"永続メモリーの重複統合のための自己スケジュール"`

**制約**: durable recurring ジョブは登録から7日で自動失効する（ツール仕様の上限）。
失効後は誰もこのスキルを起動しないため統合が止まる。ユーザーには「7日以上
Claude Code を開かない期間があると統合が止まるが、気づいたときに一度このスキルを
手動起動すれば自動的に再登録される」旨を、初回登録時と再登録時に一言添える。

### 2. 対象メモリーの列挙

1. `<instances_dir>`（`config.ini` の `[admin] instances_dir`。既定 `../Locohane_Agent-Settings/instances`。実際の場所はプロジェクトルートで `"<PYTHON_DIR>\Scripts\python.exe" -c "from src.config import resolve_instances_root; print(resolve_instances_root())"` で取得する） 直下のディレクトリ名（`admin_changes.log` 等のファイルと `_` で始まるフォルダは除く）を
   インスタンス一覧とする。`<instances_dir>` が無ければ `default` のみとする。
2. インスタンスごとに、実効の `memory_dir` を次のコマンドで取得する（`config.ini`・
   `config_overrides.json`・`${instance}` の解決を `load_config()` に任せる。パスを
   手で組み立てない）。`python_env.bat` の `PYTHON_DIR` のPython（`<PYTHON_DIR>\Scripts\python.exe`）を使い、プロジェクトルートで実行する:

   ```
   LOCOHANE_INSTANCE=<name> CONFIG_OVERRIDES_PATH=<instances_dir>/<name>/config_overrides.json "<PYTHON_DIR>\Scripts\python.exe" -c "from src.config import load_config; print(load_config().memory_dir)"
   ```

   複数インスタンスが同じ `memory_dir` を指す場合（`${instance}` を含まない固定パスに
   上書きして共有している場合）は、1回だけ処理する。存在しない `memory_dir` は飛ばす。
3. 以降（この手順2の4〜5、および手順3〜5）は `memory_dir` の中の**所有者ごと**（共通・各ユーザー）に
   独立して行う（**インスタンスをまたいで統合しない**。インスタンスごとに利用者・用途が異なるため。
   **所有者をまたいでも統合しない**。別ユーザー同士や、ユーザーのメモリーと共通のメモリーを1本にまとめると、
   個人用の内容が他のユーザーに見える場所へ移ってしまうため）。
4. 共通は `memory_dir/{user,feedback,project,reference}/*.md`、各ユーザーは
   `memory_dir/users/<ユーザー>/{user,feedback,project,reference}/*.md` を全件 `Glob`/`Read` で列挙し、
   各ファイルの frontmatter（`name`/`description`/`type`/`visibility`/`created`/`updated`）と
   本文を読む。
5. 対象が数十件を超える場合は、`state.json` の `last_consolidated` より `updated` が
   新しいメモリー（＝前回統合以降に新規作成・更新されたもの）を「起点」とし、それぞれ
   について同じ `type` の既存メモリー全件との類似性のみ確認する（type横断では統合しない。
   全件×全件の総当たりはしない）。`last_consolidated` が無い初回実行時は、type内で
   全件同士を比較する。

### 3. 重複・関連の判定（機械的な文字列一致だけで統合しない）

各起点メモリーについて、同じtype内の他メモリーと `description`・本文を実際に読み比べ、
次のいずれかに該当するか判断する。

- **完全な重複**: 同一の対象・同一の教訓を指している（nameや言い回しが違うだけ）。
- **包含関係**: 一方が他方の内容を包含し、かつ新しい情報を含む。
- **強い関連**: 同一の対象（同じスキル・同じコンポーネント）について、Why/How to apply
  の異なる側面を別々に記録しているだけで、1本にまとめた方が今後参照しやすい。

該当しない（単に同じキーワードを含むだけで話題が違う）場合は統合しない。判断に迷う場合は
統合を見送り、次回以降の判断に委ねる（誤統合による情報欠落を避ける）。

### 4. 統合の実行

統合すると判断したメモリー群について:

1. 最も内容が充実している、または最も新しい（`updated`が新しい）ものを残す側として
   選ぶ。
2. 残す側の本文に、他のメモリーが持っていた情報（Why/How to apply/具体例等）を欠落
   させず統合する。単純な結合ではなく、重複する記述は削り簡潔にまとめる（情報を
   失わないことを優先し、無理に短くしすぎない）。
3. 統合後の内容で対象ファイルを直接編集する（`Edit`/`Write`）。frontmatterの`updated`
   を現在時刻（UTC、`YYYY-MM-DDTHH:MM:SSZ`形式）に更新する。`name`/`description`/
   `type`/`created` は変更しない（`description`は必要なら統合後の内容に合わせて
   書き直してよいが、`name`は変更しない＝索引の一意性を壊さない）。
   `visibility` は、統合したもののどれか1つでも `private`（または `visibility` 無し）なら `private` にする
   （共有のものに個人用の内容を混ぜて他のユーザーへ見せないため）。
4. 吸収された側のファイルを削除する。

### 5. 索引の再構築

ファイル操作をした `memory_dir` ごとに、必ず `MEMORY.md` 索引を再構築する（Locohane
本体の `src/memory.py` の `rebuild_all_indexes()` をそのまま呼ぶ。共通の分と各ユーザーの分をまとめて作り直す。
索引を手で書き直さない）。
`<memory_dir>` には手順2で取得した絶対パスを入れる。`python_env.bat` の `PYTHON_DIR` のPython（`<PYTHON_DIR>\Scripts\python.exe`）を
使い、プロジェクトルートで実行する:

```
"<PYTHON_DIR>\Scripts\python.exe" -c "from pathlib import Path; from src.memory import rebuild_all_indexes; rebuild_all_indexes(Path(r'<memory_dir>'))"
```

### 6. 状態更新と報告

1. `state.json` の `last_consolidated` を今回のチェック終了時刻（現在時刻）で更新する。
2. インスタンスごとに、統合した件数・統合前後のname一覧を要約してユーザーへ報告する（手動起動時のみ。
   cron発火による自動実行時は、統合が1件でもあった場合のみ簡潔に報告し、無ければ
   何も報告しなくてよい）。

## 安全策

- ジョブの重複登録を避けるため、`CronCreate` は「`CronList` で確認して存在しない
  場合のみ」呼ぶ。
- 統合は必ず本文を実際に読み比べてから行う。nameやdescriptionの文字列類似度だけで
  機械的に統合しない。
- 統合により情報が失われないことを優先する（両方の記述を統合後の本文に残す。片方を
  無条件に削除しない）。
- 判断に迷うペアは統合せず見送る（1回の実行で無理に処理しきらない）。
- 各インスタンスの `memory_dir` 配下以外のファイル（アプリ本体のコード・`config.ini`・
  `<instances_dir>` 配下等）は一切変更しない。
- インスタンスをまたいだ統合・移動はしない。所有者（共通・各ユーザー）をまたいだ統合・移動もしない。
- 報告には、ユーザーの個人用（`private`）メモリーの本文を引用しない（name と件数だけにする）。
- git へのコミット・ステージングは行わない（データ置き場は git 管理外のため、
  統合・削除はgitでは復元できない。だからこそ判断に迷うペアは見送る）。
