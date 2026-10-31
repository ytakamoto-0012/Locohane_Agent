# Office系スキル（excel-*/docx-*/pptx-*）実装メモ

このファイルは `skills/SKILLS_README.md`（Agent Skills仕様全般の実装メモ）を前提に、
Excel/Word/PowerPointを扱うスキル群固有の実装上の注意点をまとめる。**office系
スキルは常に同一 `skills/` ディレクトリにまとめて配置される前提であり、他
フレームワークへ流用する場合も全部まとめてコピーするのが前提**（個別スキル
フォルダの単体持ち出しは実際の運用ケースではない）。

## 対象スキルとファミリー分け

| ファミリー | スキル |
|---|---|
| excel | `excel-read` / `excel-edit` / `excel-recalc` / `excel-render` / `excel-vba-read` / `excel-vba-edit` |
| docx | `docx-read` / `docx-create` / `docx-edit` / `docx-render` |
| pptx | `pptx-read` / `pptx-inspect` / `pptx-create` / `pptx-edit` / `pptx-render` |

各ファミリーは同一ライブラリ（`openpyxl`/`xlrd`、`python-docx`、`python-pptx`）に依存し、
共通のヘルパーコードをファミリー内で共有する。共有の実装方式が**2種類混在している**
点が本ファイルの主題。

## 1. ヘルパーの共有方式（2パターン）

### 1-A. 複製方式（`pdf-tools`/`skill-creator`/`web-search`の`_common.py`が該当）

`_common.py`（UTF-8標準入出力設定・パスメモリー登録・結果JSON書き出し等）は、
`pdf-tools` / `skill-creator` / `web-search` では**各スキルの`scripts/`配下へ
バイト単位で同一内容を複製**している。

理由: Agent Skills仕様はスキル間の相互import を想定しておらず、`run_script`も
「実行対象スキル自身の`scripts/`配下」以外への依存を前提としない設計のため、
各スキルフォルダを単独で配置した場合でも動くよう複製で持たせている。

**この複製を保証する自動チェックは現状存在しない**（手作業での同時更新に依存）。
いずれかの`_common.py`を修正した場合は、同じファミリーの全スキルへ同じ内容を
反映すること。

> **補足: 1-B方式へ移行したファイル**
> - `docx-edit/scripts/_track_changes.py` と `docx-read/scripts/_track_changes.py`
>   はかつて1-Aの複製関係にあったが、`docx-read/scripts/_track_changes.py` は
>   削除し、`read_docx.py` から `docx-edit/scripts/` を `sys.path` 経由で import
>   する 1-B 方式へ移行した（excel-read → excel-edit のパターンと同様）。
> - `excel-*`（6スキル）/ `docx-*`（4スキル）/ `pptx-*`（5スキル）の`_common.py`
>   も、かつては各ファミリー内で1-Aの複製関係にあった（ファミリー内ではバイト単位で
>   完全一致していた）。個別スキルフォルダの単体持ち出しは実際の運用ケースではない
>   （本ファイル冒頭参照）ため複製で保つ根拠が薄く、`excel-edit/scripts/
>   _excel_shared.py`（B1方式だった列グルーピングロジック）も含めて
>   `office_shared/excel_common.py` / `office_shared/docx_common.py` /
>   `office_shared/pptx_common.py` へ統合し、1-B方式（下記B2）へ移行した。
>   pptx-inspect限定だった`check_shape_overflow`もこの際`pptx_common.py`に
>   統合済み（他pptx系スキルからも参照可能になったが、現状は未使用）。

### 1-B. 相互import方式（兄弟ディレクトリへの依存）

office系スキルは同一 `skills/` ディレクトリに配置される前提のため、
`sys.path` 経由で兄弟ディレクトリ（または `office_shared/` 配下）のモジュールを
import できる。このパターンは2つ存在する。

**B1. 兄弟スキルの `scripts/` へ直接アクセス**

`docx-read/scripts/read_docx.py` は `docx-edit/scripts/_track_changes.py`
（`count_revisions`。docx-editが書き込む変更履歴と同一の解釈ロジックを
読み込み側でも使う必要があるため複製ではなく実体を1箇所に集約した）を、
**相対パスで `sys.path` に追加して import する**:

```python
_DOCX_EDIT_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "docx-edit" / "scripts"
sys.path.append(str(_DOCX_EDIT_SCRIPTS))
from _track_changes import count_revisions
```

**このため `docx-read` は `docx-edit` フォルダが兄弟ディレクトリとして存在しないと
動作しない。** 本リポジトリでは両スキルとも常にセットで同梱されるため実運用上
問題ないが、`docx-read` フォルダだけを他基盤へ個別に持ち出す場合は
`docx-edit/scripts/_track_changes.py` も一緒にコピーする必要がある。

**B2. `skills/office_shared/` への共用モジュール配置**

`office_shared/`（SKILL.mdを持たない非スキルディレクトリ）には以下が置かれている:

- `office_theme.py` — `pptx-create` / `pptx-edit` / `docx-create` / `docx-edit` /
  `excel-edit` が使うTHEMES（配色テーマ）と `resolve_theme()` 関数。
- `excel_common.py` — excel系6スキル共通のヘルパー（UTF-8標準入出力設定・
  パスメモリー登録・結果JSON書き出し・セル値/シート名解決・列幅計算・
  列グルーピングロジック）。
- `docx_common.py` — docx系4スキル共通のヘルパー。
- `pptx_common.py` — pptx系5スキル共通のヘルパー（shape記述・境界はみ出し検出含む）。

各スキルは `sys.path` に `office_shared` を追加して import する:

```python
_OFFICE_SHARED = Path(__file__).resolve().parent.parent.parent / "office_shared"
if str(_OFFICE_SHARED) not in sys.path:
    sys.path.append(str(_OFFICE_SHARED))
from office_theme import THEMES, DEFAULT_THEME, resolve_theme
from excel_common import setup_utf8_stdio, ...  # スキルのファミリーに応じて excel_common/docx_common/pptx_common を使い分ける
```

`office_theme`と`{family}_common`の両方が必要なスキル（`pptx-create`/
`pptx-edit`/`docx-create`/`docx-edit`/`excel-edit`）は、`_OFFICE_SHARED`への
`sys.path.append`は1回だけ行い、そこから両方のモジュールをimportすればよい
（どちらも同じ`office_shared/`配下にあるため）。

この共有ディレクトリは SKILL.md を持たないため、`src/skills.py` のスキル走査では
自動的にスキップされる（SKILL.md 有無を確認する仕組みのため）。

## 2. Locohane固有の環境変数（ソフト依存）

`_common.py`の`register_output_path`/`write_json_result`は、`run_script`の
子プロセスに注入される以下の環境変数を使う（`src/tools/_subprocess_env.py`の`_subprocess_env()`）:

- `AGENT_SRC_DIR`: `src/path_memory.py`をimportするためのパス
- `AGENT_THREAD_ID` / `AGENT_PATH_MEMORY_DIR` / `AGENT_PATH_MEMORY_MAX_ENTRIES`: `@N`パスメモリー登録用（`register_output_path`）
- `AGENT_EXEC_TMP_NAME`: `_tmp_<name>/`（中間生成物置き場）のディレクトリ名（`write_json_result`）。未設定時は`AGENT_THREAD_ID`（生のthread_id。Locohaneが実際に作る名前は「作成時刻+thread_idの短縮ID」でこれとは異なる）へフォールバックする

いずれも**未設定時は例外を出さずNoneへフォールバックする**設計（`AGENT_THREAD_ID`/
`AGENT_EXEC_TMP_NAME`とも未設定時は`"_no_session"`扱い）。そのため他基盤でこれらの
環境変数を用意しなくてもスクリプト自体は単体で動作するが、`@N`によるパス参照の
簡略化機能だけが働かない（生成物のパスは`result_path`/`output_path`に絶対パスと
してそのまま出力される）。

## 3. read/inspect側がwrite側の全能力を読み返せるとは限らない

`pptx-edit`の`set_shape_position`は`left_cm`/`top_cm`/`width_cm`/`height_cm`で
shapeの位置・サイズを書き込めるが、修正前の`pptx-inspect`はこれらを一切
読み返せなかった（`describe_shape`が位置・サイズ情報を含んでいなかった）。
「書き込み側にある能力が読み込み側から見えない」ことに変わりはないため、
`describe_shape`（`pptx-inspect`/`pptx-edit`等5スキル共通の`_common.py`）へ
`left_cm`/`top_cm`/`width_cm`/`height_cm`を追加し、`set_shape_position`と
同じ単位で読み返せるようにした。**新しいwrite系opを追加する際は、対応する
read/inspect側が同じ情報を同じ単位で読み返せるかを必ず確認すること**
（read側の役割が「人間向け要約」か「編集アドレッシング用」かに関わらず、
write側が操作する値をread側のどこかが可視化できていなければ、LLMは
現在値を知る手段が無いまま書き込むしかなくなる）。

## 4. アドレッシング設計（拡張時に踏襲すべきパターン）

`excel-edit`（`insert_row_group`のアンカー解決）、`docx-edit`（`DocEditContext`）、
`pptx-edit`（`EditContext`）は、いずれも共通の設計原則に従う:

> **既存コンテンツを指す番号・indexは、常に「そのスクリプト呼び出しを開始した
> 時点（＝直前のread/inspect系スキルが見せていた状態）の番号」として解決し、
> 削除・挿入・複製で後続の番号がずれても呼び出し側（LLM）に手計算させない。**

これはread側（`group_by`クエリ、`docx-read`の段落index、`pptx-inspect`の
`slide`/`shape_index`）とedit側で**同一のアドレッシング契約**を保つための設計で、
食い違うと「読み込んだ内容と実際に書き込まれる位置がズレる」事故につながる
（詳細な経緯は`issue/20260812_162617_excel_verify_fix_loop_non_convergence.md`参照）。
他基盤向けにこれらのスクリプトを拡張・移植する場合も、このアドレッシング契約を
崩さないこと。

## 5. サードパーティ依存

| ファミリー | 主な依存パッケージ | バージョン（`requirements.txt`） |
|---|---|---|
| excel | `openpyxl`（xlsx/xlsm）、`xlrd`（レガシーxls読込のみ） | 3.1.5 / 2.0.2 |
| docx | `python-docx`（import名`docx`） | 1.2.0 |
| pptx | `python-pptx`（import名`pptx`） | 1.0.2 |

他基盤へ持ち出す場合、その環境のPythonにも同等バージョンの導入が必要
（`excel-render`/`docx-render`/`pptx-render`はさらにExcel/Word/PowerPoint本体
またはCOM自動化に依存する場合がある。各スキルの`SKILL.md`を参照）。
