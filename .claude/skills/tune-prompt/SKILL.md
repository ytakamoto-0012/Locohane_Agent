---
name: tune-prompt
description: Locohane の system_prompt.md・SKILL.md・tool docstring 等のプロンプト資産を、実際のローカルLLM（llama.cpp）を動かして自動評価し、失敗があれば ClaudeCode 自身が修正して再評価するループを回す。「system_promptをチューニングして」「evalを回して」「プロンプトをループテストして」「/tune-prompt」等で使う。evals/ 配下の評価ハーネス（evals/run_all.py, evals/cases/）とセットで使う。
---

# tune-prompt: プロンプト資産の自動ループテスト・チューニング

`system_prompt/system_prompt.md`・`skills/*/SKILL.md`・`src/tools/`配下の
ツール docstring を、ローカル LLM で自動評価し、失敗があれば最小限の修正を
加えて再評価する、というループを回す。

ケース群（`evals/cases/<target>/`ディレクトリ名）と、既定のチューニング
対象ファイルの対応表:
- `system_prompt` → `system_prompt/system_prompt.md`
- `system_prompt_scale` → 同上。実データ規模の重量級ケース専用。
  ユーザーが手順0で明示的に選んだ時だけ実行する。
- `excel-skills` → `skills/excel-read/SKILL.md`
- `config_timeouts` → このスキルの対象外（プロンプト資産ではなく
  timeout数値の実測用）。手順0の選択肢には出さない。ユーザーが明示指定
  してきた場合も対象外である旨を伝えて終了する。

実際に修正してよいファイル（チューニング対象）は、手順0でユーザーに
質問して決める。上の対応表は既定の候補にすぎない。

新しいケースの追加は`create-eval-case`スキルを使う
（このスキルは実行・修正のみを担当し、ケース作成はしない）。

## 手順0: ケース群・インスタンス・ケース・チューニング対象を選ぶ（毎回必須・省略しない）

**スキル開始直後、他の作業（ファイル読み込み・疎通確認等）より先に行う。**
ユーザーの依頼文で対象が指定されていても、必ず質問して確認する。
例外: `apply-memory-to-skills`スキルから呼ばれた場合は、そのスキルが
決めた値を使い、手順0を省略する。

1. `evals/cases/`直下のディレクトリ名を見る（`config_timeouts`は除く）。
2. 必ず`AskUserQuestion`ツールを呼び、ディレクトリ名を選択肢
   （`multiSelect: true`）にして、評価に使うケース群を選ばせる。
3. 必ず`AskUserQuestion`ツールを呼び、`<instances_dir>`（`config.ini` の `[admin] instances_dir`。既定 `../Locohane_Agent-Settings/instances`。実際の場所はプロジェクトルートで `"<PYTHON_DIR>\Scripts\python.exe" -c "from src.config import resolve_instances_root; print(resolve_instances_root())"` で取得する） 直下のディレクトリ名
   （`admin_changes.log` 等のファイルと `_` で始まるフォルダは除く。`<instances_dir>` が無ければ
   `default`のみ）を選択肢（`multiSelect: false`）にして、評価に使う
   インスタンスを1つ選ばせる。選んだインスタンスの設定（LLM接続先・
   token上限・timeout等の`config_overrides.json`による上書き）で評価が
   動く。2と3は1回の`AskUserQuestion`呼び出しにまとめてよい。
4. 選ばれた対象ごとに、必ず`AskUserQuestion`ツールを呼び、
   `evals/cases/<target>/*.yaml`のファイル名（拡張子抜き）と
   「全ケース実行」を選択肢（`multiSelect: true`）にしてケースを選ばせる。
5. 選ばれたケース群ごとに、必ず`AskUserQuestion`ツールを呼び、修正して
   よいチューニング対象ファイルを選ばせる（`multiSelect: true`）。
   選択肢の例:
   - 対応表の既定ファイル（先頭に置き、ラベル末尾に「(Recommended)」）
   - `system_prompt/system_prompt.md`（既定と重複するなら出さない）
   - `src/tools/`配下のツール docstring
   - 修正せず評価のみ（失敗は報告して終了する）

   具体的なファイルパスは「Other」で入力してもらう。`src/tools/`等の
   ディレクトリが選ばれたら、失敗原因を見てからその中のファイルを
   1つに絞って直す。4と5は1回の`AskUserQuestion`呼び出しにまとめてよい。
6. ケース群が複数選ばれたら、1つ目で「手順1」〜「手順2」を最後まで
   終えてから、2つ目に進む（並行して進めない）。

以降、`<instance>`は手順0-3で選んだインスタンス名、「対象ファイル」は
手順0-5で選んだチューニング対象ファイルを指す。`<target>`はケース群名を
指す。退避先の`evals/history/<target>/`には、対象ファイルが既定以外の
ときファイル名を付けて退避する（例: `iter03_before_tools.py`）。

## 手順1: 前提を確認する

1. 選んだインスタンスの実効設定を次のコマンドで取得する（`python_env.bat` の `PYTHON_DIR` の
   Python（`<PYTHON_DIR>\Scripts\python.exe`）を使い、プロジェクトルートで実行する。`config.ini`を
   直接読んでも上書き分が反映されないため、必ずこのコマンドを使う）:
   ```
   "<PYTHON_DIR>\Scripts\python.exe" -m evals.instance <instance>
   ```
2. 表示されたLLM接続先でllama.cpp serverが起動しているか確認する。
   起動していないと評価結果は`error: llm_unreachable`になる。
   その場合はループに入らず、ユーザーにserver起動を頼んで終了する。
3. `evals/README.md`を読み、ケース形式・実行方法を把握する。

## 手順2: 評価ループ

**上限10回。** 上限に達したら、直せた箇所と残っている失敗を報告して
終了する（無限ループにしない）。

### 2-0. 初回だけ: ベースライン退避・ログ初期化

- `evals/history/<target>/`が空なら、対象ファイルの現在の内容を
  `evals/history/<target>/iter00_baseline.md`にコピーする。
- 手順1で表示された`log_dir`配下の`evals.log`があれば空にする
  （前回までのループのログと混ざらないようにするため）。

### 2-1. 評価を実行する

必ず`--instance <instance>`を付けて実行する（付けないと`default`
インスタンスの設定で動いてしまう）。

全ケースが対象:
```
python evals/run_all.py <target> --instance <instance>
```

個別ケースが対象（選ばれたケースIDを空白区切りで指定する）:
```
python evals/run_all.py <target> <case_id1> <case_id2> ... --instance <instance>
```

サマリ先頭の`インスタンス:`行が選んだインスタンスになっていることを確認する。

合格が偶然でないか確かめたいとき（最終確認など）は`--repeat N`を付けてよい（スキル安定化トライアウト。各ケースをN回ずつ回し、サマリ末尾の判定が全回合格かを見る）。ドラフトスキルの正式化は`promote-skill`の担当で、このスキルでは行わない。

出力サマリと`evals/results/<target>/<最新timestamp>/results.json`を見る。

### 2-2. 判定する

- `rules_pass: false`のケース → 不合格。
- `judge`指示があるケース → `transcript`と`judge`指示文を自分で読んで
  合否を判断する（捏造・幻覚呼び出し・的外れな委譲は不合格寄りに見る）。
- `turn_cutoffs`（`recursion_limit`/`thinking_loop`による打ち切り）が
  あるケースは、それだけで即不合格にはしない。何が起きたかを一言
  `tuning_log.md`の根拠に含める。
- ケースID・合否・根拠1〜2行を`evals/tuning_log.md`に追記する
  （イテレーション番号の見出しの下にまとめる）。

### 2-3. 全ケース合格なら終了する

- 対象ファイルの現在の内容を`evals/history/<target>/iterNN_final.md`に
  退避する。
- `evals/tuning_log.md`に完了サマリ（イテレーション数、直した箇所）を
  追記する。
- ユーザーに完了報告して終了する。

### 2-4. 不合格があれば直す

手順0-5で「修正せず評価のみ」を選んだ場合は直さず、不合格ケースと
推定原因をユーザーに報告して終了する。

1. 不合格ケースの`transcript`と対象ファイルの記述を見比べ、根本原因を
   特定する（記述が曖昧、指示が矛盾している、例が無い、等）。
2. 直す前に対象ファイルを`evals/history/<target>/iterNN_before.md`
   （NNは今回のイテレーション番号）にコピーする。
3. 原因に対して最小限だけ直す（Editツール）。合格しているケースの挙動を
   壊さないよう、変更範囲を絞る。
   対象ファイルは低パラメータモデル（ローカルLLM）が読む前提で書く:
   一文を短くする、背景説明や複数の具体例を並べない、「ルール＋代替行動」
   の形にする、条件分岐は箇条書きにする。
4. `evals/tuning_log.md`に「### iterNN」の見出しで、ケース・原因・
   変更箇所・理由を短く追記する。
5. **振動検知**: 直近2〜3イテレーションで同じケースが同じ理由で合格と
   不合格を繰り返しているなら、修正方針が間違っている。それ以上繰り
   返さず、状況（何を試して何が起きたか）をユーザーに報告してループを
   止める。
6. 手順2-1に戻る。

## 安全策

- gitへのコミット・ステージングはしない（退避ファイルと`tuning_log.md`
  だけで変更履歴を追う）。
- 手順0-5で選んだ対象ファイル以外は編集しない。原因が選ばれていない
  ファイル（別のSKILL.md・ツール docstring等）にあると分かったら、
  直さずにユーザーへ報告し、対象に加えてよいか`AskUserQuestion`で
  確認する。原因が対象ファイルの記述ではなく
  `config.ini`側の数値（token上限・timeout等）だと分かった場合も、
  対象ファイルは直さずユーザーに報告して終了する（ユーザーから明示
  指示があれば`config.ini`を直してよいが、`tuning_log.md`に
  「ユーザー指示によるconfig値調整」と明記する）。選んだインスタンスの
  `config_overrides.json`に同じキーがあると`config.ini`の変更は効かない
  ため、その場合は管理ツール（`admin.bat`）で直すよう案内する。
  `<instances_dir>` 配下は直接編集しない。
- `evals/tuning_log.md`の各イテレーション見出しには、使ったインスタンス名
  も書く（インスタンスごとに接続先モデルが違うと結果が比較できないため）。
- 1イテレーションで複数箇所を同時に直さない（原因の切り分けと振動検知
  が効かなくなるため）。
