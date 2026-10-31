# トークン上限超過エラー（160,468トークン、507,527トークン）→ コンテキスト圧縮失敗

- **区分**: バグ
- **検知日時**: 2026-08-08 20:50:00
- **対象ログファイル**: data/logs/app_20260808_172838.log

## 経緯

レシピ画像変換バッチタスク（IMG_2299〜IMG_2358のMDファイル作成）において、メインエージェントのコンテキストが128,000トークンの上限を大幅に超えるまで膨張した。最初の失敗では160,468トークンでコンテキストサイズ超過エラーが発生し、その後コンテキスト圧縮（context_compaction）を試みたが、これも507,527トークンで再び上限超過エラーが発生して失敗した。

## ログ引用

```
2026-08-08 17:31:17,612 ERROR src.tools: dispatch_agent_background 失敗 (run_id=92cd1f48fdb54eaf87ff025ed53828a9)
2026-08-08 17:31:17,619 WARNING src.tools: tool_result: name=dispatch_agent_background content="エラー: サブエージェントの実行に失敗しました: Error code: 400 - {'error': {'code': 400, 'message': 'request (160468 tokens) exceeds the available context size (128000 tokens), try increasing it', 'type': 'exceed_context_size_error', 'n_prompt_tokens': 160468, 'n_ctx': 128000}}"
2026-08-08 17:31:21,320 ERROR src.context_compaction: 会話履歴の自動要約に失敗しました。今回は圧縮をスキップします
2026-08-08 17:31:21,320 openai.BadRequestError: Error code: 400 - {'error': {'code': 400, 'message': 'request (507527 tokens) exceeds the available context size (128000 tokens), try increasing it', 'type': 'exceed_context_size_error', 'n_prompt_tokens': 507527, 'n_ctx': 128000}}
```

## エラー原文

```
openai.BadRequestError: Error code: 400 - {'error': {'code': 400, 'message': 'request (160468 tokens) exceeds the available context size (128000 tokens), try increasing it', 'type': 'exceed_context_size_error', 'n_prompt_tokens': 160468, 'n_ctx': 128000}}

openai.BadRequestError: Error code: 400 - {'error': {'code': 400, 'message': 'request (507527 tokens) exceeds the available context size (128000 tokens), try increasing it', 'type': 'exceed_context_size_error', 'n_prompt_tokens': 507527, 'n_ctx': 128000}}
```

## 推定原因

1. **大規模バッチタスクの負荷**: 20件×2グループ（計40件）の画像からMDファイルを作成する大規模バッチタスクにおいて、画像認識結果、栄養情報、Pythonコード、ツール呼び出しの結果などが会話履歴に累積し、コンテキストが爆発的に膨張した。
2. **コンテキスト圧縮のタイミング遅れ**: 160,468トークンに達した時点でコンテキスト圧縮が触发されなかった、または圧縮処理自体が会話履歴の大きさを十分に把握できていなかった可能性がある。
3. **圧縮時の再失敗**: コンテキスト圧縮処理自体がLLM呼び出しを含むが、その際にも507,527トークンという巨大なコンテキストが指定され、圧縮処理自体が上限超過エラーで失敗した。

## 追記（2026-08-08 20:50）

初回検知。トークン上限超過が2回連続で発生（160,468トークン、507,527トークン）。両方とも128,000トークンの上限を大幅に超えている。

## 追記（2026-08-23 20:05）— 根本原因を確定

対象ログファイル: data/logs/app_20260823_195217.log

「E:\共有\写真」配下の魚の写った画像を探すタスクで、`worker`系サブエージェントが
`analyze_image`を約96回連続で呼び出した（iter=3の時点で既に`total_tokens=114866`と
softしきい値81920に肉薄）。その後の自動コンテキスト圧縮が、要約プロンプト自体が
**17,954,934トークン**（上限128,000の約140倍）という桁違いの規模になり失敗。
圧縮スキップ後、そのまま送った通常のLLM呼び出しも184,392トークンで上限超過し、
`dispatch_agent`タスク全体が失敗で終了した（前回2026-08-08の507,527トークンを
大きく更新する規模）。

```
2026-08-23 20:05:31,082 WARNING src.subagent: subagent: トークン使用量が閾値(81920)に近づいたため注意メッセージを注入(iter=3, total_tokens=114866)
2026-08-23 20:05:30,940 ERROR src.context_compaction: 会話履歴の自動要約に失敗しました。今回は圧縮をスキップします
openai.BadRequestError: Error code: 400 - {'error': {'code': 400, 'message': 'request (17954934 tokens) exceeds the available context size (128000 tokens)...', 'n_prompt_tokens': 17954934, 'n_ctx': 128000}}
2026-08-23 20:05:37,058 ERROR src.tools: dispatch_agent 失敗 (run_id=81a6731c8f574859a57e1c91de06a8f1)
openai.BadRequestError: Error code: 400 - {'error': {'code': 400, 'message': 'request (184392 tokens) exceeds the available context size (128000 tokens)...', 'n_prompt_tokens': 184392, 'n_ctx': 128000}}
```

**根本原因（コード上で特定済み）**: 会話履歴の切り詰め・要約処理が、
`content`が文字列である前提で書かれており、`analyze_image`が返す
マルチモーダルcontent（`[{"type": "text", ...}, {"type": "image_url",
"image_url": {"url": "data:image/...;base64,..."}}]`のようなlist型）を
一切トリムせずそのまま素通ししている。

1. [src/context_trim.py:70](../src/context_trim.py#L70)
   `trim_old_tool_messages()`:
   ```python
   if i in keep or not isinstance(m, ToolMessage) or not isinstance(m.content, str):
       result.append(m)  # content がstr以外（listを含む画像付き等）なら無条件で素通し
       continue
   ```
   `max_chars`による切り詰めは`isinstance(m.content, str)`が真の場合にしか
   適用されないため、画像を含むToolMessageは何トークンあろうと一切トリムされない。

2. [src/context_compaction.py:223](../src/context_compaction.py#L223)
   `_messages_to_text()`:
   ```python
   content = m.content if isinstance(m.content, str) else str(m.content)
   ```
   1.でトリムされずに残った画像付きmessage（base64データを含むlist）を
   `str()`でPythonのrepr文字列化してそのまま要約プロンプトへ連結している。
   base64画像データがそのままテキスト化されるため、画像を多数含む会話ほど
   要約プロンプト自体が指数的に肥大化する（今回の17.9Mトークンはこれが原因）。

この2箇所はいずれも「テキスト量が多い場合に切り詰める」という設計意図を
持ちながら、`analyze_image`等が使うマルチモーダルcontent（list型）を
検知できておらず、画像を扱うタスクに対してだけ機能しない状態になっている。
`analyze_image`を多数回呼ぶタスク（今回のような大量画像判定・前回2026-08-08の
大量画像→MD変換バッチ）で繰り返し再現しており、単発の偶発事象ではない。

**改善案（未実装）**: 上記2箇所で`content`がlist型の場合、画像ブロック
（`image_url`等）を除去またはプレースホルダ文字列（例:
`[image omitted, N bytes]`）に置換してから文字数カウント・切り詰め・
テキスト化を行うようにする。

## ユーザー回答
