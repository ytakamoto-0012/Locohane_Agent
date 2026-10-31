# execute_python_codeのcwdがrun_scriptの作業ディレクトリと食い違い、SKILL.mdおよびツール本体のdocstringの誤った記述と相まって混乱・不具合を誘発していた

- **区分**: バグ（ドキュメント） → SKILL.md・ツールdocstring修正済み（コード自体は意図的な既存設計として維持）
- **検知日時**: 2026-08-22 21:50頃（ユーザー指摘を受けて調査。実際の発生は本日21:01前後〜22:03継続）
- **対象ログファイル**: data/logs/app_20260822_203542.log

## 経緯

ユーザーから「作業ディレクトリが引き継がれてないバグがあるかも」との指摘を受け、
本日のapp.logと`src/tools.py`の実装を確認した。

`cwd=`を含む行を集計すると、同一セッション内で2種類の値が混在していた。

```
$ grep -oE "cwd=[^ ]*" app_20260822_203542.log | sort | uniq -c
     34 cwd=C:\DT_Python\Locohane\data\temp\_tmp_31b2db95-8fd8-4735-924b-9d805f48fc01
     29 cwd=E:\user_b\vba-test
```

`run_script`呼び出し（29件）は常にユーザー設定の作業ディレクトリ
（`E:\user_b\vba-test`、config.iniまたはChatSettingsで設定）を`cwd`に使う一方、
`execute_python_code`/`execute_python_code_readonly`呼び出し（34件）は常に
`C:\DT_Python\Locohane\data\temp\_tmp_<thread_id>`（default_workdir配下の
セッション専用スクラッチフォルダ）を`cwd`に使っていた。

このcwdの食い違いに、サブエージェント自身のLLMが実際に困惑し長時間の
試行錯誤を招いていたログが残っている（21:01:38）：

```
opsファイルが作業ディレクトリ外にあるため、run_scriptから読めません。
execute_python_codeが実行されるディレクトリは`_tmp_<thread_id>`のサブ
ディレクトリなので、その中にopsファイルを書く必要があります。しかし、
委譲元のtask文では「E:\user_b\vba-test」に出力するとあるので、この
ディレクトリにファイルを書く必要があります。問題は、run_scriptがサンド
ボックス制約で`_tmp_<thread_id>`以外のディレクトリに書き込んだファイルを
`--ops-file`で渡せないことです。
```

この混乱の結果、サブエージェントは`E:\user_b\vba-test\_tmp_ops.json`の
ようにユーザーの作業ディレクトリ直下へ`_tmp_`プレフィックス付きの
絶対パスで書き込む方式を選び、それが
[issue/20260822_202048_foreign_tmp_dir_guard_false_positive_on_file.md](20260822_202048_foreign_tmp_dir_guard_false_positive_on_file.md)
で報告・修正した「他セッションの一時ディレクトリ誤検知」バグを誘発する
直接の引き金になっていた。

## 推定原因（コードレベルで特定・意図的設計と確認）

- `run_script`のcwd: `_prepare_script_execution()`（`src/tools.py:2277`）が
  `_restrict_default_workdir(_resolve_workdir(need_write=True))`を使う。
  `_resolve_workdir()`はユーザー設定の作業ディレクトリ（未設定/書き込み
  不可ならdefault_workdirへフォールバック）を返す。
- `execute_python_code`のcwd: `_resolve_exec_workdir()`（`src/tools.py:1495`）が
  常に`_DEFAULT_WORKDIR / f"_tmp_{thread_id}"`を返す。ユーザー設定の作業
  ディレクトリが何であっても**常にdefault_workdir基準**。

この非対称自体は`_resolve_exec_workdir()`のdocstringに明記された**意図的な
既存設計**だった（「以前はユーザー指定の work_dir 直下に作っていたが、
生成中に別スレッドへ切り替えるとソケット切断（on_chat_end）で即座に
rmtreeされ、裏で継続中の処理と競合する問題があった」ため、
default_workdir固定＋日数ベース自動削除に変更した経緯がコード内に記録
されている）。したがって**コード自体を「作業ディレクトリに合わせる」方向へ
戻すのは過去に踏んだ地雷を再び踏むことになるため見送った**。

一方、`_exec_guard_roots()`（execute_python_code の書き込みガード）は
実際にはユーザーの作業ディレクトリと`_tmp_<thread_id>`の**両方**を
書き込み許可対象に含めている（`src/tools.py:2886`）。つまり
`execute_python_code`から見て「作業ディレクトリへ直接書く」も
「`_tmp_<thread_id>`（＝execute_python_code自身のcwd）へ相対パスで書く」も
どちらも技術的には可能で、後者の方が単純かつ安全（ファイル名衝突・
作業ディレクトリの汚染・`_tmp_`プレフィックス絡みの誤検知を避けられる）
だった。

真の問題は**SKILL.mdの指示文言**にあった。`excel-edit`/`excel-vba-edit`の
SKILL.mdはいずれも「`execute_python_code`で...`json.dump`で**作業ディレクトリ
配下**の一時ファイルへ書き出し」と明記しており、これは`execute_python_code`の
実際のcwdと矛盾する誤った指示だった。この文言に従おうとしたLLMが
「execute_python_codeの実際のcwdは_tmp_<thread_id>なのに、指示は作業
ディレクトリ配下と言っている」という矛盾に気づき、上記の通り混乱・
回り道をした。

同種の文言（「作業ディレクトリ配下の...`_tmp_<thread_id>`」）は
`pdf-tools`/`docx-render`/`pptx-render`のSKILL.mdにも存在したが、これらは
Locohane側が生成するPNGの保存先を説明しているだけでLLMが自分でパスを
組み立てる必要はなく、実害は無かった（ただし同じ不正確さのため、ついでに
訂正した）。

## 対応（修正済み）

以下5ファイルのSKILL.mdの文言を修正:

1. `skills/excel-edit/SKILL.md` — 「作業ディレクトリ配下の一時ファイルへ
   書き出し」の記述を削除し、「`execute_python_code`のcwdは`run_script`の
   作業ディレクトリとは別物（セッション専用の`_tmp_<thread_id>`）。ops
   ファイルはユーザーの作業ディレクトリを狙わず単純に相対パスで書き、
   `os.path.abspath()`で得た絶対パスをそのまま`--ops-file`に渡せばよい」
   という正確な説明に置き換え。
2. `skills/excel-vba-edit/SKILL.md` — 同様の修正（excel-editのSKILL.mdを
   参照する形で簡潔に）。
3. `skills/pdf-tools/SKILL.md`・`skills/docx-render/SKILL.md`・
   `skills/pptx-render/SKILL.md` — 「作業ディレクトリ配下のセッション専用
   一時フォルダ」という表現を「セッション専用一時フォルダ（ユーザーが
   設定した作業ディレクトリとは別で、`default_workdir`配下）」に訂正。

コード自体（`_resolve_exec_workdir()`のdefault_workdir固定方式）は
意図的な既存設計であり、過去の障害（on_chat_endでのrmtree競合）を
回避するためのものと判断し変更していない。

検証: `pytest tests/` 356件全通過（今回はドキュメントのみの変更のため
既存テストへの影響なし）。

## 追記（2026-08-22 22:03）— より根本的な誤情報源をツール本体のdocstringで発見

SKILL.md修正後もタスクは混乱を継続した（22:01〜22:03）。`provide_download`で
以下2連続のエラーが発生:

```
2026-08-22 22:02:58,653 WARNING src.tools: tool_result: name=provide_download content='エラー: ファイルが見つかりません: E:\\user_b\\vba-test\\収支計算表_final.xlsm'
2026-08-22 22:03:45,276 WARNING src.tools: tool_result: name=provide_download content='エラー: ファイルが見つかりません: @9'
```

直前の`execute_python_code`（22:01:22）を確認すると、`work_dir = os.getcwd()`
で取得した値を使い`収支計算表_final.xlsm`を正しく`_tmp_<thread_id>`へ生成
できていた（`Final file exists: True`で確認済み）。しかしその後
`provide_download`を`E:\user_b\vba-test\収支計算表_final.xlsm`という
**存在しない**絶対パスで呼び出しており、サブエージェントが「生成した
ファイルは作業ディレクトリにあるはず」と誤認していたことがわかる。
SKILL.mdの修正（excel-edit/excel-vba-edit限定）は、この会話でこれから新規に
読み込まれるスキルにしか反映されず、`provide_download`はスキル固有の
ドキュメントを持たないネイティブツールのため、根本原因はまだ別にある
と判断し、`src/tools.py`のツール本体のdocstringを調査した。

**発見**: `execute_python_code`関数自体のdocstring（`src/tools.py:3133`
付近、LLMへ常時見えるツールのスキーマ説明文そのもの。SKILL.mdと異なり
会話開始時にキャッシュされるものではなく毎回動的に注入される）が、
次のように**事実と異なる**記述をしていた:

> 作業ディレクトリは run_script と同じ作業ディレクトリ配下の、この
> セッション専用のサブディレクトリになる。

実際には`_resolve_exec_workdir()`（`src/tools.py:1495`）が示す通り
`execute_python_code`のcwdは常に`default_workdir`配下（run_scriptの
cwdとは無関係）であり、この一文は明確な誤り。同関数の170行目付近には
書き込みガードの説明として正しい情報（`_tmp_<thread_id>`がこのコードの
cwdそのものである旨）も別途あったが、冒頭のこの誤った要約文の方が
先に読まれ、より強い誤解を与えていたと考えられる。

こちらはSKILL.mdよりも影響範囲が広く（全スキル・全会話に共通して
表示される）、かつキャッシュされないため**即座に全会話へ反映される**、
より根本的な訂正だった。

### 対応（修正済み）

`src/tools.py`の`execute_python_code`のdocstringを、「cwdはrun_scriptとは
異なりdefault_workdir配下の`_tmp_<thread_id>`である」「ユーザーの作業
ディレクトリを狙う必要はなく単純に相対パスで書けばよい」「run_script側も
この一時フォルダを読み取れるためcwdが違うことは問題にならない」という
正確な説明に書き換えた。`execute_python_code_readonly`/
`execute_python_code_background`のdocstringは「execute_python_codeと同じ」
と委譲する形で誤った独自記述を持っていなかったため修正不要と確認。

検証: `pytest tests/` 356件全通過。

## 追記（2026-08-22 22:05）— docstring修正後もこの会話内では混乱が継続（想定内）

`src/tools.py`のdocstring修正（22:03時点で適用済み・以後は毎ターン
LLMへ正しい説明が渡っている）にもかかわらず、22:01:38の`edit_excel.py`
再実行はやはり`Permission denied`で失敗（EXCEL.EXEロック未解消、
[issue/20260822_212800](20260822_212800_run_macro_msgbox_hang_and_excel_lock.md)
と同一原因）。さらに22:05:50に3回目の思考ループが検知され、直近テキストに
「The path for `ops.json` will be `E:...」とあり、**この会話内では
まだ作業ディレクトリ配下を想定した発想を続けている**ことが確認できた。

これはdocstring修正が効いていないのではなく、この会話が既に長時間
（21:01頃から）同じ誤った前提を土台に推論を積み重ねてきたため、ツール
説明文が正しくなっても会話内の既存の文脈・慣性の方が優先されている
と考えられる（LLMの一般的な傾向で、Locohane固有の不具合ではない）。
修正の効果は主に**新規会話**で発揮される見込み。この会話自体を
立て直すには、会話のリセットまたは明示的な訂正指示が必要と考えられる。

## 追記（2026-08-22 22:23）— 会話が立ち直り、cwd混乱は解消

22:21:17以降、サブエージェントは`os.getcwd()`に頼らず、明示的に
`_tmp_31b2db95-8fd8-4735-924b-9d805f48fc01`という実際のcwdを正しく
参照するコードを書くようになり、cwd混乱由来の失敗は発生しなくなった
（3回目のループ検知後、注意メッセージ注入で立て直った可能性が高い）。

`dispatch_agent`の最終回答（22:23:01）でタスクはほぼ完了：
format_table 3件・add_chart 2件・VBAボタン配置（modMainへの
CreateButtons追加）まで完了し、成果物は`_tmp_<thread_id>`配下に
`収支計算表_final.xlsm`として保存済み。

唯一残った失敗は、この成果物をユーザーの作業ディレクトリの元ファイル
`E:\user_b\vba-test\収支計算表.xlsm`へコピーバックする最終ステップで、
サブエージェント自身は「サンドボックスの書き込み制限」と誤診断していたが、
実際のトレースバックを確認すると`_guard_open`は元の`open()`をそのまま
呼び出しており、`PermissionError`はOS自体（Windows）が返したもの
（`_guard_orig_open`経由）。これは
[issue/20260822_212800](20260822_212800_run_macro_msgbox_hang_and_excel_lock.md)
のEXCEL.EXEロックがまだ解消されていないことによるもので、サンドボックス
ガードとは無関係。サブエージェントの誤診断はLLM側の解釈の問題であり、
Locohaneのバグではないと判断（エラーメッセージ自体はOS由来のため
これ以上分かりやすくしようがない）。

cwd不一致に起因する混乱は本issueの対応で解消したと判断し、これ以降は
EXCEL.EXEロック問題（別issue）の推移のみを追跡する。

## ユーザー回答

ここにはユーザーの回答が記述される
