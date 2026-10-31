# exploreサブエージェントがrun_script経由でdocx-readスキルを呼び出そうとし、許可スキル外として拒否

- **区分**: 問題点
- **検知日時**: 2026-08-23 18:17:35
- **対象ログファイル**: data/logs/app_20260823_175334.log

## 経緯

`explore`サブエージェント（agent_type=explore）が、生成済みdocxファイルの
内容確認のため`run_script`で`docx-read`スキルの`read_docx.py`を呼び出そうと
した。しかし`explore`エージェント種別が`run_script`経由で呼べるスキルは
`web-search`のみに制限されており、エラーで拒否された。エラーメッセージは
「office文書ならanalyze-docs、書き込みが要るならworkerへ委譲するよう
委譲元に伝えてください」と適切な誘導を含んでいる。

## ログ引用

```
2026-08-23 18:17:35,992 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'docx-read', 'script_filename': 'read_docx.py', 'script_args': ['E:\\user_b\\テスト\\○○子ども会_過去実績報告書.docx']} -> エラー: agent_type="explore" から呼び出せる run_script のスキルは ['web-search'] に限定されています（skill=docx-read は対象外）。ファイルの内容確認が必要な場合は、委譲元に対応するサブエージェント（office文書/PDFなら analyze-docs、書き込みが要るなら worker）へ改めて委譲するよう伝えてください。
```

## 推定原因

エージェント種別ごとの`run_script`許可スキルのホワイトリスト
（`agents/explore.md`等）により意図通り拒否されている。ガード自体は
正しく機能しており、拒否メッセージも次にとるべき行動（analyze-docsへの
再委譲）を明示できている。実害は1往復分のトークン・ターン浪費のみ。

メインエージェント側が「生成物の存在確認」を依頼する際に、対象が
office文書（docx/xlsx/pptx/pdf）であれば最初から`analyze-docs`へ委譲する、
という判断がプロンプト上でより明確になれば、この種の誤委譲・再委譲往復を
減らせる可能性がある（未実装・要検討）。

## 追記（YYYY-MM-DD HH:MM）

## ユーザー回答

ここにはユーザーの回答が記述される
