# execute_python_code ツールで「計画が未承認」エラーが発生

- **区分**: 問題点
- **検知日時**: 2026-08-02 02:51:00
- **対象ログファイル**: data/logs/app_20260802_02_2.log

## 経緯

画像ファイルの一覧取得とmdファイルの既存チェックを行うPythonコードが
`execute_python_code` ツールで実行されたが、`create_plan` で計画を作成した
直後ではなく、`approve_plan` を呼び出す前に実行されていたため、
「計画が未承認のため実行できません」というエラーで失敗した。

その後、`create_plan` の直後に `approve_plan` を呼ばずに他のツールを
実行しようとした際にも同様のエラーが発生した。

## ログ引用

```
2026-08-02 02:49:02,123 WARNING src.tools: tool_result: name=execute_python_code content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
2026-08-02 02:49:18,612 WARNING src.tools: tool_result: name=execute_python_code content='エラー: create_planの直後はapprove_planを呼んでください（他のツールは実行されませんでした）。'
```

## 推定原因

LLMが `create_plan` の後、`approve_plan` でユーザー承認を得る前に
`execute_python_code` を実行しようとした。`create_plan` → `approve_plan`
→ 実際のツール実行、という順序を守る必要があり、この順序が守られなかった。

## 追記（2026-08-02 10:37）

同一パターン（execute_python_code で計画未承認エラー）が再発。
今回は画像ファイルの一覧をPythonコードでリスト化しようとした際、
approve_plan を飛ばして execute_python_code を実行していた。

```
2026-08-02 10:37:03,709 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\n\n# Image files\njpg_files = [\n    "IMG_2197.JPG", "IMG_2214.JPG", ...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
```

前2回（02:49）との違い: 前回は `args_code` 内にPythonコードの断片が
省略されていたが、今回は画像ファイル名の一覧が実際にコードとして
記載されていた。LLMが画像ファイル名を直接コード中に列挙しようとする
挙動が確認できる。

## 追記（2026-08-02 12:27）

同一パターン（execute_python_code で計画未承認エラー）が再発。
画像ファイルの一括処理中に`create_plan`の直後に`approve_plan`を
呼ばずに他のツールを実行しようとした。

```
2026-08-02 12:27:38,257 WARNING src.tools: tool_result: name=Glob content='エラー: create_planの直後はapprove_planを呼んでください（他のツールは実行されませんでした）。'
```

前3回（02:49, 10:37）との違い: 前回は`execute_python_code`での
エラーが主だったが、今回は`Glob`ツールでの発生。計画承認フローの
違反がツール種別を問わず検知されている。

## 追記（2026-08-03 00:35）

同一パターンが再発。今回は画像処理のバッチタスク中に複数回発生。
画像ファイルの一覧取得や残画像チェックのPythonコードを実行する際に、
必ず `approve_plan` を飛ばして `execute_python_code` を実行しようとしていた。

```
2026-08-03 00:35:25,670 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\n\nimages_dir = r"E:\\user_a\\レシピ\\images"...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
2026-08-03 00:37:48,432 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\n\nimages_dir = r"E:\\user_a\\レシピ\\images"...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
2026-08-03 00:38:04,479 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\n\nimages_dir = r"E:\\user_a\\レシピ\\images"...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
2026-08-03 00:40:25,614 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\n\nmd_dir = r"E:\\user_a\\レシピ\\md"...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
2026-08-03 00:44:49,830 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\n\nimages_dir = r"E:\\user_a\\レシピ\\images"...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
2026-08-03 00:45:12,088 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\n\nimages_dir = r"E:\\user_a\\レシピ\\images"...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
2026-08-03 00:45:39,801 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\n\nimages_dir = r"E:\\user_a\\レシピ\\images"...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
```

前4回（02:49, 10:37, 12:27）との違い: 前回は単発または数回だったが、
今回は1回のタスク内で7回連続して発生。LLMが計画承認フローを
体系的に無視している傾向が顕著になっている。

## 追記（2026-08-03 02:02）

同一夜間のセッションで、この計画未承認エラーの連発と並行してThinkingLoopGuard
も頻発しており、最終的に画像レシピ抽出バッチがステップ11（画像111〜120件目）
で未完遂のまま停止した。両者は「大量画像バッチをどう分割・委譲するか」という
同じ混乱から生じている可能性が高い。根本原因の分析と修正案（`create_plan`
再呼び出し時の`plan_approved`リセット回避など）は`issue.md`の**ISSUE-004**に
集約して記録した。

## 追記（2026-08-03 23:04）

同一パターン（execute_python_code で計画未承認エラー）が再発。
画像ファイルのプレフィックス抽出コードを実行する際に、approve_plan
を飛ばして execute_python_code を実行していた。

```
2026-08-03 23:04:02,680 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\nimport re\n\n# 既存mdファイルから画像名のプレフィックスを抽出\nmd_dir = r"E:\\user_a\\レシピ\\md"...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
```

前8回（08/02 02:49〜08/03 02:02）との違い: 同日（08/03）の夜間セッション
（23:04）でも再発。LLMの計画承認フロー違反パターンは解消されていない。

## 追記（2026-08-03 23:25）

同一パターン（execute_python_code で計画未承認エラー）が再発。
画像ファイルの一覧取得コードを実行する際に、approve_plan を飛ばして
execute_python_code を実行していた。

```
2026-08-03 23:25:45,742 WARNING src.subagent: subagent tool=execute_python_code args={'code': 'import os\nimport re\n\noutput_dir = r"E:\\user_a\\レシピ\\md"...'} content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
```

前9回（08/02 02:49〜08/03 23:04）との違い: 同日（08/03）の夜間セッション
（23:25）でも再発。画像レシピ抽出バッチの最終段階で発生。

## 追記（2026-08-03 23:34）

同一パターン（execute_python_code で計画未承認エラー）が再発。
images/mdフォルダの一覧取得コードを実行する際に、approve_plan を
飛ばして execute_python_code を実行していた。

```
2026-08-03 23:34:07,282 WARNING src.tools: tool_result: name=execute_python_code args_code='import os\n\n# imagesフォルダ内の全ファイル一覧（既に取得済み）...' content='エラー: 計画が未承認のため実行できません。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。'
```

前10回（08/02 02:49〜08/03 23:25）との違い: 同日（08/03）の夜間セッション
（23:34）でも再発。23:25の追記から約9分で再発。

## ユーザー回答

ここにはユーザーの回答が記述される
