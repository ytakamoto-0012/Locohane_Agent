---
name: monitor-app-log
description: Locohane の data/<インスタンス名>/logs/app_*.log（監視対象インスタンスは手動起動時にユーザーへ質問して選ぶ）を5分おきに定期監視し、WARNING/ERROR/CRITICALログおよびコンテキスト圧縮（src.context_compaction）の発火を検知したら、issue/ ディレクトリ配下にケースごとのMarkdownファイルとして自動起票・追記する。スキル自身がCronCreateで5分間隔・durable:trueの自己スケジュールを登録し、以後はcron発火のたびに自分自身を再度呼び出す。「app.logを監視して」「ログからissueを起票して」「/monitor-app-log」等で使う。ユーザーが手動で /loop を回す運用ではなく、このスキル自身が定期実行を管理する点が他スキルと異なる。
---

# monitor-app-log: app*.log の定期監視によるissue自動起票

`Locohane` は `<log_dir>/app_YYYYMMDD_HHMMSS_N.log` に
`%(asctime)s %(levelname)s %(name)s: %(message)s` 形式でアプリログを出力する
（`app.py` の `initialize()`、`src/log_rotation.py`）。`log_dir` はインスタンス
ごとに異なる（既定 `./data/<インスタンス名>/logs`。`config.ini` の
`[paths] common_data_dir = ./data/${instance}` 由来で、管理ツールの
`instances/<name>/config_overrides.json` で上書きされている場合もある）。このスキルは新規に
出力されたログを5分おきに確認し、バグ・改善点・問題点をプロジェクト直下
`issue/` ディレクトリに Markdown として書き残す。

Qwen Code はこれまで `issue.md`（単一ファイル）に「症状／発生条件／
推定原因／対応／追記」の形式で手動記録してきた。本スキルはその運用を
踏襲しつつ、ケースごとに個別ファイル化して自動化する。

## 状態ファイル

`.qwen/state/monitor-app-log/state.json`（プロジェクトルート基準。過去の
記述ミスで「このSKILL.mdと同じディレクトリ」としていたが、実際の運用では
このパスが使われている。2026-08-12訂正）:

```json
{"last_checked": "2026-08-01T22:50:00", "cron_job_id": "xxxxx", "instances": ["default"]}
```

- `last_checked`: 前回スキャン済みのログ時刻（ISO形式、ローカル時刻）。
  ファイルが無い/壊れている場合は**現在時刻**で初期化する
  （過去ログの一括バックフィルはしない。監視開始時点より先のみが対象）。
- `cron_job_id`: このスキルが自己登録した `CronCreate` のジョブID。
- `instances`: 監視対象のインスタンス名の一覧（手順0で決める）。

## 手順

### 0. 監視対象インスタンスの決定

- **ユーザーが手動で起動した場合**: `instances/` 直下のディレクトリ名
  （`admin_changes.log` 等のファイルは除く。`instances/` が無ければ `default` のみ）
  を選択肢にして、必ず `AskUserQuestion`（`multiSelect: true`）で監視対象を選ばせる。
  `state.json` に `instances` があれば、その現在値も質問文に添える。
  選ばれた一覧を `state.json` の `instances` に保存する。
- **cron発火による自動起動の場合**: 質問しない（無人実行のため応答が返らない）。
  `state.json` の `instances` をそのまま使う。`instances` が無い場合は
  `["default"]` とみなす。
- 保存済みのインスタンスが `instances/` から消えている（削除された）場合は、
  そのインスタンスを飛ばす。

### 1. 自己スケジュールの確認・登録

1. `state.json` を読む（無ければ手順末尾の初期化を行う）。
2. `cron_job_id` があれば `CronList` で該当ジョブがまだ存在するか確認する。
3. ジョブが存在しない場合（初回起動、または登録から7日経過して自動失効した
   場合）のみ、以下の内容で `CronCreate` を呼び、返ってきたジョブIDを
   `state.json` の `cron_job_id` に書き込む。既にジョブが生きている場合は
   **絶対に再登録しない**（重複ジョブが並走してしまうため）。
   - `cron`: `"*/5 * * * *"`
   - `recurring`: `true`
   - `durable`: `true`
   - `prompt`: `"/monitor-app-log を実行し、app_*.log の新規WARNING/ERROR/CRITICALおよびコンテキスト圧縮の発火を確認して issue/ に起票・追記してください。"`
   - `reason`（分かればログ用に）: `"app*.log を5分おきに監視するための自己スケジュール"`

**制約**: durable recurring ジョブは登録から7日で自動失効する（ツール仕様の
上限）。失効後は誰もこのスキルを起動しないため監視が止まる。ユーザーには
「7日以上 Qwen Code を開かない期間があると監視が止まるが、気づいたときに
一度このスキルを手動起動すれば自動的に再登録される」旨を、初回登録時と
再登録時に一言添える。

### 2. 設定読み込み

監視対象インスタンスごとに、実効の `log_dir` を次のコマンドで取得する
（`config.ini`・`config_overrides.json`・`${instance}` の解決を
`load_config()` に任せる。パスを手で組み立てない）。`python_env.bat` の `PYTHON_DIR` の
Python（`<PYTHON_DIR>\Scripts\python.exe`）を使い、プロジェクトルートで実行する:

```
LOCOHANE_INSTANCE=<name> CONFIG_OVERRIDES_PATH=instances/<name>/config_overrides.json "<PYTHON_DIR>\Scripts\python.exe" -c "from src.config import load_config; print(load_config().log_dir)"
```

### 3. 新規ログ抽出

以下は監視対象インスタンスごとに行う（抽出した行には、どのインスタンスの
ログかを覚えておく）。

1. `log_dir` 配下の `app_*.log` を対象に、`last_checked` より新しい
   タイムスタンプの行のうち **`WARNING` / `ERROR` / `CRITICAL`** を抽出する
   （Bashのgrep等で `%(levelname)s` 部分を条件に絞り込み、行頭の
   `%(asctime)s` と `last_checked` を比較する）。
2. **特例**: ログレベルに関わらず、logger名 `src.context_compaction` の
   行（会話履歴の自動圧縮に関するログ。例:
   `会話履歴を圧縮しました: ...`〈INFO〉、
   `会話履歴の自動要約結果が空だったため...`〈WARNING〉）も常に抽出対象に
   含める。圧縮の発火自体はエラーではないが、メインエージェントの会話が
   トークン上限に近づいている兆候であり、閾値・プロンプト設計の改善余地を
   示すシグナルとして扱う。

**トークン上限越え系問題の調査指針**: `src.context_compaction` や
`Token limit reached` / `context compaction failed` 等のメッセージは、
トークン上限到達そのものが問題なのではなく、**なぜそれだけコンテキストが
膨張したか**が発生源である。該当イベントの直前に「どのようなツール呼び出し・
サブエージェント実行・長い出力の積み重ね」があったかを、`last_checked` 以降
のログ範囲で遡って調査し、issue起票時に「トークン上限超えのトリガーとなった
具体的な操作・出力」を `## 推定原因` に記載する。上限自体の調整は最終手段とし、
まずはプロンプトの圧縮・ツール結果の省略・会話設計の見直し等の根本対策を
検討すべきである。
3. **WARNING行がエラー内容を欠落させている場合の実体確認**: `src.subagent` /
   `src.tools` の `subagent tool=...` / `tool_result: name=...` 系WARNING行は、
   同一イベントに対して `WARNING` と `DEBUG` の両方が出力されるが、**`WARNING`
   側は内容が途中で切り詰められ、`DEBUG`側にのみ完全な内容が入っていることが
   ある**（実例: `run_script(edit_excel.py)` 失敗時、WARNING行は
   `-> [終了コード] 1` のみで終わり、直後・同時刻の同一 `args=` を持つDEBUG行に
   `[終了コード] 1\n[標準エラー]\nTraceback ...` というTraceback全文が入って
   いた。2026-08-12調査、[issue/20260812_121000_excel_edit_mergedcell_attributeerror.md](../../../issue/20260812_121000_excel_edit_mergedcell_attributeerror.md)参照）。
   WARNING行の `->` 以降が `[終了コード] N`（N≠0）で終わっている、または
   内容が不自然に短く実際のエラー本文が読み取れない場合は、**同一ファイル内で
   同時刻・同一 `args=` プレフィックスを持つ `DEBUG` レベルの重複行**を探し、
   そちらの完全な内容を `## ログ引用`・`## エラー原文` に使う（`config.ini`
   の `[log].level` がDEBUG以上であれば同一ファイルに存在する）。これを怠ると、
   エラーの実体（Traceback等）を欠いたまま `## 推定原因` が「未検証」で
   埋没してしまう。
4. 該当行が一つも無ければ、issueファイルは作成せずそのまま終了する
   （`state.json` の `last_checked` 更新は行う）。

### 4. 事案のグルーピング

抽出した行を原因（例外クラス名・エラーメッセージの特徴的な部分、または
「コンテキスト圧縮の発火」というイベント種別）ごとにまとめ、1事案＝1候補と
する。コンテキスト圧縮関連の行は他のWARNING/ERROR事案とは別グループとし、
区分は原則「改善点」とする（要約結果が空でリトライ不能だった等、実害が
出ている場合のみ「バグ」を検討する）。

**`execute_python_*` 系ツールのWARNING**: `src.tools` / `src.subagent` loggerの
`tool_result` / `subagent tool=execute_python_code` 警告は、LLMが生成した
Pythonコードの実行結果を記録するためのWARNINGであり、**成功・失敗を問わず
必ず発生する**。これらはバグではなく「今後の機能開発のアイデア」として扱う。
- **成功時**: ツールが正常に終了し、結果を返している場合 → 区分「改善点」
- **失敗時**: ツールがエラーメッセージを返している場合 → 区分「問題点」
- 区分の判定は、ログのcontentフィールドにエラー文言（「エラー:」や
  「Exception」等）が含まれるかで判断する。正常な出力（終了コード0、
  処理結果の列挙等）が含まれる場合は成功とみなす。

**`execute_python_code` のコード記載**: `execute_python_code` のWARNINGログには
LLMが生成したPythonコード（`code`）が含まれている。ログフォーマットは以下の2種類。
- **subagent経由**: `WARNING src.subagent: subagent tool=execute_python_code args={'code': '...'} -> [content]`
- **メイングラフ経由（修正後）**: `WARNING src.tools: tool_result: name=execute_python_code args_code='...' content='...'`

issue起票時には、該当ログから `code` フィールドを抽出し、ログ引用セクションの
後に以下の節を追加して記載する。

\`\`\`
## 実行コード

\`\`\`python
（codeフィールドの内容をそのまま記載。500文字を超える場合は省略しない。
末尾の改行は除去する）
\`\`\`
\`\`\`

### 5. 重複チェックと起票／追記

1. 各候補について `issue/*.md` を Grep し、同一原因を示す既存ファイルが
   ないか確認する（例外クラス名や特徴的なメッセージ断片で検索）。
2. 一致するファイルがあれば、そのファイル末尾に
   `## 追記（YYYY-MM-DD HH:MM）` セクションを追加し、新しいログ引用と
   経緯を追記する（既存 `issue.md` の運用を踏襲）。別インスタンスで同じ原因が
   出た場合も、新規ファイルにせず追記し、追記内にインスタンス名を書く。
3. 一致するファイルが無ければ、`issue/` ディレクトリが無ければ作成した上で
   `issue/YYYYMMDD_HHMMSS_<内容を表す短い英語slug>.md` として新規作成する。

新規作成・追記いずれも、以下のフォーマットに従う。

```markdown
# <現象を要約したタイトル>

- **区分**: バグ | 改善点 | 問題点
- **検知日時**: YYYY-MM-DD HH:MM:SS
- **インスタンス**: <インスタンス名>
- **対象ログファイル**: data/<インスタンス名>/logs/app_YYYYMMDD_HHMMSS_N.log（プロジェクトルート相対）

## 経緯

（ログから読み取れる時系列で、どのような操作・リクエストの流れでこの
現象が発生したかを説明する）

## ログ引用

\`\`\`
（該当ログ行を実際の文字列のまま引用する）
\`\`\`

## エラー原文

（区分が「バグ」でエラー/例外が発生している場合のみ記載。
Traceback等の原文をそのまま貼る。改善点・問題点の場合はこの節自体を省く）

\`\`\`
...
\`\`\`

## 推定原因

（分かる範囲で。不明なら「未検証」と明記する）

## 追記（YYYY-MM-DD HH:MM）

（同一原因の問題が再検知されるたびに、ここに追記を積み重ねていく）

## ユーザー回答

ここにはユーザーの回答が記述される

```

### 6. 状態更新

`state.json` の `last_checked` を今回のスキャン終了時刻（現在時刻）で
更新する。

## 安全策

- ジョブの重複登録を避けるため、`CronCreate` は「`CronList` で確認して
  存在しない場合のみ」呼ぶ。
- 該当ログが無いターンでは何も書き込まない（空のissueファイルを作らない）。
- 同一原因の繰り返しは新規ファイルを乱立させず、既存ファイルへの追記で
  対応する（`issue/` の肥大化・重複を防ぐ）。
- `issue/` 配下以外のファイル（アプリ本体のコード・`config.ini`・
  `instances/` 配下等）は一切変更しない（`state.json` の更新は除く）。
- cron発火による自動起動時は `AskUserQuestion` を呼ばない。
- git へのコミット・ステージングは行わない。
