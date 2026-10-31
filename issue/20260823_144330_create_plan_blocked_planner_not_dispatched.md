# create_planがplanner未委譲を理由にブロックされた

- **区分**: 問題点
- **検知日時**: 2026-08-23 14:04:44
- **対象ログファイル**: data/logs/app_20260823_135730.log

## 経緯

メインエージェントが`dispatch_agent(agent_type="planner")`を経ずに直接
`create_plan`を呼び出し、ガードにより拒否された。これは
2026-08-14の矛盾指示インシデントの再発防止のために導入された意図的な
ガード（`create_plan`の前に必ずplannerへ計画草案を作らせる）であり、
設計通りの動作。この直後にメインエージェントは正しく`dispatch_agent`
（`agent_type: read_skill`等）へ切り替えており実害は無い。

## ログ引用

```
2026-08-23 14:04:44,372 WARNING src.tools: tool_result: name=create_plan content='エラー: create_planの前にdispatch_agent(agent_type="planner")を呼んでください。調査で得た具体的事実とユーザー要求をplannerへ過不足なく伝え、計画の草案を作らせてからcreate_planを呼び直すこと（自分の記憶・推測だけでsteps/detail_markdownを構成しない）。'
```

## 推定原因

ガードは意図通り動作している。再発自体は「LLMが計画を作る前に
plannerへの委譲を省略しがち」という傾向によるもので、Locohane側の
不具合ではない。

## 追記（2026-08-23 20:12）

対象ログファイル: data/logs/app_20260823_195217.log

```
2026-08-23 20:11:53,782 WARNING src.tools: tool_result: name=create_plan content='エラー: create_planの前にdispatch_agent(agent_type="planner")を呼んでください。調査で得た具体的事実とユーザー要求をplannerへ過不足なく伝え、計画の草案を作らせてからcreate_planを呼び直すこと（自分の記憶・推測だけでsteps/detail_markdownを構成しない）。'
```

再発。原因・対応は上記から変わらず。

## ユーザー回答

ここにはユーザーの回答が記述される
