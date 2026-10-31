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

## ユーザー回答

ここにはユーザーの回答が記述される
