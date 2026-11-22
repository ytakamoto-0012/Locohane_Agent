"use strict";
/* はじめてのチュートリアル（チュートリアル）。
 *
 * 初回起動（instances/ が無く default を作ったとき）と、本体インスタンスを新しく作ったときに、
 * 使い始めるのに要る設定を順番に説明しながら案内する。サーバーは instances/<名前>/.tutorial_pending の
 * 有無を一覧の tutorial_pending で返し（admin/instances.py）、ガイドを終える・スキップすると
 * POST /api/instances/{name}/tutorial/done で消す。
 *
 * 画面を新しく作らず、今のダッシュボードの画面（config.ini・ユーザー・ナレッジ・管理者設定・インスタンス一覧）を
 * ガイドが開き、説明のカード（右下）と、対象の場所の強調で案内する。config.ini の手順では、その手順の設定だけを
 * 絞り込んで表示する（admin.js の activePreset に仮のプリセットを入れる）。変更は最後の「保存」の手順でまとめて保存する。
 * admin.js の後に読み込む（$・api・renderInstanceDetail 等を使う）。
 */

// 対象の高さが画面のこの割合を超えたら、中央ではなく先頭（見出し）が見えるようにスクロールする。
const SETUP_GUIDE_TALL_RATIO = 0.6;
// 強調する場所を探し直す間隔（ms）。画面の作り直し（renderKeyList 等）で要素が入れ替わっても強調を付け直すため。
const SETUP_GUIDE_POLL_MS = 600;
// 一度出したインスタンス（ページを開いている間は、同じインスタンスで自動では出し直さない）。
const setupGuideOffered = new Set();
let setupGuide = null; // { name, steps, index, card, timer, targets, scrolled, clickHandler }

/** 初回起動・新しく作ったインスタンスなら、ガイドを始める（renderInstances から呼ぶ）。 */
function maybeStartSetupGuide() {
  if (setupGuide) return;
  const pending = instanceCache.filter((i) => i.tutorial_pending && i.kind !== "skill_lab" && !setupGuideOffered.has(i.name));
  if (!pending.length) return;
  const target = pending.find((i) => i.is_default) || pending[0];
  startSetupGuide(target.name);
}

function setupGuideLabel(name) {
  return instanceLabel(
    instanceCache.find((i) => i.name === name),
    name,
  );
}

function setupGuideConfigSteps() {
  return [
    {
      title: _t("LLM の接続先（[llm] main_url）"),
      body: _t("チャットの回答を作る推論サーバー（llama.cpp の llama-server・vLLM 等の OpenAI 互換サーバー）の接続先です。base_url（例: http://localhost:8080/v1）と model（サーバーの --model / --alias に合わせた名前）を、使うサーバーに合わせてください。provider は llama-server なら llama_cpp、vLLM なら vllm にします。\n複数書くと [llm] main_routing_strategy の順で振り分けます。サブエージェント用の [llm] sub_url は既定で空（main_url をそのまま使う）なので、別のサーバーへ分けたいときだけ設定します。"),
      keys: ["llm.main_url"],
    },
    {
      title: _t("トークンの上限（[graph] token_guard_soft_threshold）"),
      body: _t("1回のリクエストのトークン数がこの値に達すると、AI に状況の報告と引き継ぎの文を書かせて、安全に処理を終えます。推論サーバーのコンテキスト長（llama-server の --ctx-size）から、ツールの結果1往復ぶんの余裕を引いた値にしてください。\n[token_ratio] enabled が true の間は、この値を変えると、サブエージェントの上限やコンテキスト圧縮の閾値にも比率をかけた値が自動で入ります（「比率で再計算」でも一括で入れられます）。"),
      keys: ["graph.token_guard_soft_threshold"],
      tokenRatio: true,
    },
    {
      title: _t("並列数（左の「並列数」ボタン）"),
      body: _t("同時に動かせる数の設定です。左の「並列数」ボタンを押すと、いつでもこの3つだけを表示できます。\n・[llm] max_concurrent_requests: LLM に同時に生成させる数（全セッションの合計）。llama-server の --parallel（複数台なら合計）と揃えます。\n・[graph] max_parallel: 1つの会話で、メインエージェントのツールを同時に実行する数。\n・[subagent] max_parallel: 1つの会話で、サブエージェントを同時に動かす数。\n推論サーバーのスロットが1つなら、すべて 1 のままで構いません。"),
      preset: _t("並列数"),
    },
    {
      title: _t("データの保存先（[paths] common_data_dir）"),
      body: _t("会話履歴・永続メモリー・アップロード・ログ等の保存先です。既定の ../Locohane_Agent-Settings/data/${instance} なら、${instance} がインスタンス名に置き換わり、インスタンスごとに別のフォルダへ分かれます。\nローカルディスクのパスにしてください（ネットワークドライブ・UNC パスは使えません）。ほかのインスタンスと同じ場所は指定できません。"),
      keys: ["paths.common_data_dir"],
    },
    {
      title: _t("スキルの置き場所（[paths] project_locohane_dir）"),
      body: _t("同梱のほかに読み込むスキル（skills/<スキル名>/SKILL.md）・サブエージェントのフォルダの一覧です。社内で作ったスキルを置くフォルダがあれば足します。\n足したスキルのスクリプトを AI に実行させるには、続く3つの手順の一覧にも登録します。"),
      keys: ["paths.project_locohane_dir"],
    },
    {
      title: _t("サブエージェントに許可するスクリプト（[subagent] agent_type_run_script_allowlist）"),
      body: _t("サブエージェントの種類（agent_type。explore 等）ごとに、run_script で実行してよいスキル・スクリプトを [agent_type, [\"スキル名\", \"スクリプトファイル名\"]] の形で登録します。登録のある種類は、ここにあるスクリプトしか実行できません。\n既定値の項目は消せません。ここで保存した項目は既定値に足されます。"),
      keys: ["subagent.agent_type_run_script_allowlist"],
    },
    {
      title: _t("メインエージェントに許可するツール（[main_agent_tool_guard] allow_entries）"),
      body: _t("メインエージェントが直接呼んでよいツール・スキルのスクリプトの一覧です（[対象, 1ターンの上限回数]。-1 は無制限、0 は禁止）。ここに無いものは、メインエージェントからは呼べません（サブエージェントへ委譲すれば使えます）。新しいスキルのスクリプトをメインエージェントから直接実行させたいときに足します。\n既定値の項目は消せません。ここで保存した項目は既定値に足されます。"),
      keys: ["main_agent_tool_guard.allow_entries"],
    },
    {
      title: _t("計画の承認を省くスクリプト（[plan] plan_approval_exempt_scripts）"),
      body: _t("計画の承認（Plan Mode）を待たずに実行してよい、読み取り専用のスクリプトの一覧です（[\"スキル名\", \"スクリプトファイル名\"]）。書き込みをするスクリプトを足すと、承認なしで書き込みが行われるため、読み取りだけのスクリプトに限ってください。\n既定値の項目は消せません。ここで保存した項目は既定値に足されます。"),
      keys: ["plan.plan_approval_exempt_scripts"],
    },
    {
      title: _t("ベクターDB（社内文書の意味検索）を使うか（[vector_db] enabled）"),
      body: _t("true にすると、「ナレッジ」で取り込んだ社内文書を AI が意味で検索できるようになります。使わないなら false のままにして、ベクターDB の手順は「次へ」で進めてください。"),
      keys: ["vector_db.enabled"],
    },
    {
      title: _t("ベクターDB の保存方式（[vector_db] backend・qdrant_url・qdrant_preset）"),
      body: _t("backend は保存の方式です。sqlite_vec（既定）は追加のサーバーが要らず、このインスタンスのデータフォルダに保存します。文書が多い（チャンクが数十万件を超える）場合は qdrant にして、Qdrant サーバーを用意します。\nqdrant_url は backend = qdrant のときの Qdrant サーバーの URL（例: http://localhost:6333）、qdrant_preset はチャンク件数に合わせた索引・量子化の設定です（〜100万件なら none）。sqlite_vec なら、この2つは空のままで構いません。"),
      keys: ["vector_db.backend", "vector_db.qdrant_url", "vector_db.qdrant_preset"],
    },
    {
      title: _t("取り込む文書の種類（[vector_db] include_extensions・text_extensions・image_input_extensions）"),
      body: _t("include_extensions は取り込む拡張子です。text_extensions は、対応形式のほかに中身をテキストとして読む拡張子です（例: .step。取り込むには include_extensions にも書きます）。image_input_extensions は、[vector_db] image_input = true のときにページを画像としても埋め込む文書の形式です（既定は .pdf）。"),
      keys: ["vector_db.include_extensions", "vector_db.text_extensions", "vector_db.image_input_extensions"],
    },
    {
      title: _t("埋め込みサーバー（[vector_db] embedding_url）"),
      body: _t("文書と検索語をベクターに変える埋め込みサーバー（OpenAI 互換の /v1/embeddings）の接続先です。空（[]）なら、内蔵の埋め込み（CPU）を使います。複数書くときは、すべて同じモデルにしてください。\n埋め込みのモデルを後から変えると、取り込んだ文書は「再構築」が要ります。"),
      keys: ["vector_db.embedding_url"],
    },
    {
      title: _t("Office 文書の中の画像（[vector_db] embedded_images）"),
      body: _t("true（既定）なら、Word・PowerPoint・Excel の本文・スライド・シートに置いた画像も、画像として埋め込みます。false なら Office ファイルは本文だけを埋め込みます（取り込みが速くなります）。"),
      keys: ["vector_db.embedded_images"],
    },
    {
      title: _t("チャット画面のログイン（[auth] enabled・require_password）"),
      body: _t("enabled を true にすると、チャット画面にログインが要るようになり、会話・メモリーがユーザーごとに分かれます。ユーザーは、後の手順の「ユーザー」タブで登録します。\nrequire_password が false の間は、登録したユーザー名だけで（パスワードは何でも）ログインできます。社内 LAN の外から使える場合は true にしてください。"),
      keys: ["auth.enabled", "auth.require_password"],
    },
  ];
}

function setupGuideAllConfigKeys() {
  const ids = [];
  for (const step of setupGuideConfigSteps()) {
    const keys = step.keys || (CONFIG_PRESETS.find((p) => p.name === step.preset) || { keys: [] }).keys;
    for (const id of keys) if (!ids.includes(id)) ids.push(id);
  }
  return ids;
}

function setupGuideSteps(name) {
  const label = setupGuideLabel(name);
  const configSteps = setupGuideConfigSteps().map((step) => ({
    ...step,
    enter: () => setupGuideShowConfig(name, step),
    target: () => setupGuideConfigTargets(step),
  }));
  return [
    ...setupGuideInstanceListSteps(name, label),
    {
      title: _t("config.ini の画面"),
      body: _t("インスタンス「{0}」の設定画面が開きました。ここから、使い始めるのに要る設定を順番に案内します。各手順では、その設定だけを絞り込んで表示します。値を確かめ、必要なら変えてから「次へ」を押してください。各設定の下の説明もあわせて読んでください。\n変更は、設定の手順の最後でまとめて保存します（それまでは保存されません）。", label),
      enter: () => setupGuideOpenSubview(name, "config"),
      target: () => [$('.subtab-btn[data-subview="config"]')],
    },
    ...configSteps,
    {
      title: _t("変更を保存する"),
      body: _t("ここまでの変更を保存します。「変更を確認」で変更の内容を確かめてから「この内容で保存」を押してください（「保存」で直接保存もできます）。保存した値はこのインスタンスだけに効き、config.ini 自体は書き換えません。\n変更を反映するには、インスタンスの再起動が要ります（このあとの手順で案内します。保存後に出る「今すぐ再起動」でも再起動できます）。\n何も変えていなければ、そのまま「次へ」で進めます。"),
      enter: () => setupGuideShowConfig(name, { keys: setupGuideAllConfigKeys() }),
      target: () => [$("#preview-btn"), $("#save-btn"), $("#restart-banner:not(.hidden)")],
      beforeLeave: () => {
        if (!$("#config-keys")) return true;
        const count = Object.keys(dirtyEdits).length + resetKeys.size;
        return count === 0 || confirm(_t("保存していない変更が {0} 件あります。保存しないまま進むと、変更は消えます。進みますか？", count));
      },
    },
    {
      title: _t("「ユーザー」タブ"),
      body: _t("チャット画面にログインするユーザーを管理します（[auth] enabled が true のときに使います）。下の欄にユーザー名とパスワードを入れて「追加」を押します。[auth] require_password が false なら、パスワードは空欄でも構いません。\nこのインスタンスのユーザーをまだ1人も追加していない間は、プロジェクト直下の .env の AUTH_USERS を引き継いで表示します（ここでは変えられません）。1人追加すると、このインスタンス専用のユーザーに切り替わります。\nランクは、内容が食い違う永続メモリーをどのユーザーのものに従わせるかの優先度です（数が大きいほど上位）。"),
      enter: () => setupGuideOpenSubview(name, "users"),
      target: () => [$('.subtab-btn[data-subview="users"]'), $("#add-user-form")],
    },
    {
      title: _t("「表示設定」タブ"),
      body: _t("チャット画面の見た目を、このインスタンス専用に変えます。ヘッダーの文言（header.md）・ブラウザのタブのタイトル（tab_title.md）・チャットを始めたときのウェルカムメッセージ（welcome.md。{skills} はスキルの一覧に置き換わります）・アイコン・favicon を、それぞれ「保存」「アップロード」で変えます。\nここで変えていない項目は、上部の「表示設定」タブの全インスタンス共通の設定を使います。「共通に戻す」でこのインスタンス専用の設定を消せます。変更は再起動しなくても、ブラウザの再読み込み（ウェルカムメッセージは次のチャットの開始）で反映されます。"),
      enter: () => setupGuideOpenSubview(name, "display"),
      target: () => [$('.subtab-btn[data-subview="display"]')],
    },
    {
      title: _t("「ナレッジ」タブ"),
      body: _t("社内文書を取り込んで、AI が意味で検索できるようにする画面です（[vector_db] enabled が true のときに使います）。「コレクションを追加」で名前・説明・取り込み元のフォルダを入れ、「取り込み（差分）」を押すと文書を読み込みます。時刻を決めた自動の取り込みも設定できます。\nAI はコレクションの説明を見て検索先を選ぶため、説明には何の文書かを具体的に書いてください。"),
      enter: () => setupGuideOpenSubview(name, "knowledge"),
      target: () => [$('.subtab-btn[data-subview="knowledge"]')],
    },
    {
      title: _t("「メモリー」タブ"),
      body: _t("AI が会話をまたいで覚えている内容（永続メモリー）を見て、直す画面です。AI は会話の中で、利用者の好み・決まりごと・用語などをメモリーに残し、次の会話から使います。\nメモリーはユーザーごとにあり、AI は自分のもの・共通のもの・ほかのユーザーが「共有」にしたものを探します（ほかのユーザーの「個人用」は探しません）。間違った内容や古い内容は、ここで書き直す・削除できます。いつも守らせたい少数の決まりは「システムプロンプトに表示」にすると、AI が毎回従います。\n変更は次の会話から効きます。書き換え・削除の前の内容はバックアップに残ります。"),
      enter: () => setupGuideOpenSubview(name, "memory"),
      target: () => [$('.subtab-btn[data-subview="memory"]')],
    },
    {
      title: _t("「管理者設定」タブ"),
      body: _t("全インスタンス共通の設定です（インスタンスごとには変えられません）。ナレッジの取り込みと、右上の「AIに相談」の設定を、上から順に説明します。各項目の下の説明もあわせて読んでください。\n値を変えたら、最後の手順で画面の下の「保存」を押します。保存した値は、管理ツール・インスタンスを再起動しなくても、次の取り込み・次の相談から効きます。"),
      enter: () => setupGuideOpenAdminSettings(),
      target: () => [$('.tab-btn[data-view="admin-settings"]')],
    },
    ...setupGuideAdminSettingSteps(),
    {
      title: _t("管理者設定を保存する"),
      body: _t("ここまでの管理者設定の変更を、画面の下の「保存」で保存してください。「config.ini の値に戻す」を押すと、この画面で保存した値をすべて消します。\n何も変えていなければ、そのまま「次へ」で進めます。"),
      enter: () => setupGuideOpenAdminSettings(),
      target: () => [$(".admin-settings-actions")],
      beforeLeave: () => {
        const box = $(".admin-settings");
        return !box || !box.dataset.dirty || confirm(_t("管理者設定に保存していない変更があります。保存しないまま進むと、変更は消えます。進みますか？"));
      },
    },
    {
      title: _t("インスタンスを再起動する"),
      body: _t("インスタンスの一覧に戻りました。保存した設定は、インスタンスを再起動すると効きます。「{0}」のカードの「再起動」を押してください（状態が「停止中」なら「起動」を押します）。\n状態が「稼働中」になったら「次へ」を押します。設定を何も変えていなければ、再起動せずに進んで構いません。", label),
      enter: () => setupGuideTopTab("instances"),
      // 稼働中なら「再起動」、止まっていれば「起動」（押せる方）を強調する。
      target: () => {
        const card = $(`.instance-card[data-instance="${CSS.escape(name)}"]`);
        if (!card) return [];
        const restart = card.querySelector(".btn-restart");
        const start = card.querySelector(".btn-start");
        return [restart && !restart.disabled ? restart : start && !start.disabled ? start : restart, card.querySelector(".state-label")];
      },
    },
    ...setupGuideAssistantSteps(name),
    {
      title: _t("チャット画面を開く"),
      body: _t("「{0}」のカードの「開く ↗」を押すと、チャット画面が新しいタブで開きます（押すとチュートリアルは終わります）。\n開けないときは、状態が「稼働中」になっているかを確かめてください。\nログインを求められたら、「ユーザー」タブで登録したユーザー名でログインします。", label),
      enter: async () => {
        await setupGuideTopTab("instances");
        setupGuideWatchOpenLink(name);
      },
      target: () => [$(`.instance-card[data-instance="${CSS.escape(name)}"] .open-link`)],
    },
  ];
}

/* ---------------------------------------------------------- インスタンス一覧の説明 */

function setupGuideCard(name) {
  return $(`.instance-card[data-instance="${CSS.escape(name)}"]`);
}

/** ガイドの始まり: インスタンス一覧の画面構成を説明し、カードの「設定」を押してもらう（押して設定画面が開くと進む）。 */
function setupGuideInstanceListSteps(name, label) {
  const openList = () => setupGuideTopTab("instances");
  return [
    {
      title: _t("はじめてのチュートリアル"),
      body: _t("インスタンス「{0}」を使い始めるまでを、画面を順に開きながら案内します。光っている所を見ながら「次へ」で進めてください。\nまず、いま開いている「インスタンス」の画面の見方を説明し、そのあと設定画面で、使い始めるのに要る設定を1つずつ確かめます。\n「スキップする」でいつでも終えられます。あとから、インスタンスの一覧の「はじめてのチュートリアル」でもう一度始められます。", label),
      enter: openList,
      target: () => [$('.tab-btn[data-view="instances"]')],
    },
    {
      title: _t("インスタンスの一覧"),
      body: _t("「インスタンス」は、チャット画面（AI）の1つ分です。インスタンスごとに、設定・ログインユーザー・会話の履歴・メモリー・ナレッジが分かれます（部署ごとに分けて使う、等）。\n最初は「default」が1つだけあります。ここに、インスタンスごとのカードが並びます。"),
      enter: openList,
      target: () => [$("#instance-cards")],
    },
    {
      title: _t("インスタンスのカード"),
      body: _t("カードの上には、表示名と、チャット画面のアドレス（ホスト:ポート）、今の状態（稼働中・停止中・外部で起動中・異常終了）が出ます。左の丸の色でも状態が分かります。\n「既定」の印が付いているのが default です（削除できません）。"),
      enter: openList,
      target: () => [setupGuideCard(name) && setupGuideCard(name).querySelector(".card-top"), setupGuideCard(name) && setupGuideCard(name).querySelector(".card-meta")],
    },
    {
      title: _t("カードの稼働状況"),
      body: _t("動いている間は、接続中のユーザー・接続しているブラウザの数・回答を作っている会話の数・24時間の警告とエラーの数が出ます（5秒ごとに更新）。回答を作っている間は、会話の長さ（トークン数）のグラフも出ます。\n利用者から「遅い」「エラーになる」と言われたときは、まずここを見ます。"),
      enter: openList,
      target: () => [setupGuideCard(name) && setupGuideCard(name).querySelector(".card-activity"), setupGuideCard(name)],
    },
    {
      title: _t("カードのボタン"),
      body: _t("・開く ↗: チャット画面を新しいタブで開く\n・起動・停止・再起動: インスタンスを動かす・止める（設定を変えた後は「再起動」で効かせる）\n・編集: 表示名・ホスト・ポート・自動起動を変える\n・モニター: 接続中のユーザー・会話・ログを見る\n・研究テーマ: スキル研究室で、このインスタンスのスキルを仕上げる\n・ナレッジ: 社内文書を取り込む\n・設定: config.ini の設定・ユーザー・表示設定などを変える\n・削除: インスタンスを消す（default と稼働中のものは消せない）"),
      enter: openList,
      target: () => [setupGuideCard(name) && setupGuideCard(name).querySelector(".card-actions")],
    },
    {
      title: _t("インスタンスを追加する"),
      body: _t("部署ごと・用途ごとに別のチャット画面を用意したいときは、「＋ インスタンス追加」で増やします。作ると、そのインスタンスでもこのチュートリアルが始まります。\n今は「{0}」の設定を進めます。", label),
      enter: openList,
      target: () => [$("#add-instance-btn")],
    },
    {
      title: _t("「設定」を押す"),
      body: _t("「{0}」のカードの、光っている「設定」を押してください。設定画面が開くと、自動で次に進みます。", label),
      waitLabel: _t("「設定」を押すと進みます"),
      enter: openList,
      target: () => [setupGuideCard(name) && setupGuideCard(name).querySelector(".btn-config")],
      autoNext: () => Boolean($("#config-keys")) && currentInstanceName === name,
    },
  ];
}

/* ---------------------------------------------------------- AIに相談の体験 */

// 体験の進み具合（送る前の相談の数・送った後のメッセージの数・失敗したときのメッセージの数）
const assistantGuideState = { userCount: 0, sentLength: 0, errorLength: 0 };

function assistantGuideMessages() {
  return typeof assistantState !== "undefined" ? assistantState.messages : [];
}

function assistantGuideLast() {
  const list = assistantGuideMessages();
  return list[list.length - 1] || null;
}

/** 回答が出終わった（送った後に AI の回答か失敗が1件増え、生成中でない）。 */
function assistantGuideAnswered() {
  const last = assistantGuideLast();
  return typeof assistantAbort !== "undefined" && !assistantAbort && last && last.role === "assistant" && assistantGuideMessages().length > assistantGuideState.sentLength;
}

/** 「AIに相談」が使えないとき（status が無効でボタンが出ていない）は、体験の手順を飛ばす。 */
function assistantGuideUnavailable() {
  const toggle = $("#assistant-toggle-btn");
  return !toggle || toggle.classList.contains("hidden");
}

/** 最後に「AIに相談」を実際に使ってみる手順。カードはパネル（右端）に被らないよう、その左へ寄せる。 */
function setupGuideAssistantSteps(name) {
  const skip = assistantGuideUnavailable;
  const cardClass = "setup-guide-card--left-of-assistant";
  return [
    {
      title: _t("AIに相談を開く"),
      body: _t("最後に、右上の「AIに相談」を実際に使ってみます。設定で困ったときに、どの画面のどの設定を変えればよいかを AI が助言します（AI は設定を読むだけで、変更・保存はしません）。\n光っている「AIに相談」を押してください。右に相談のパネルが開くと、自動で次に進みます。"),
      skip,
      waitLabel: _t("「AIに相談」を押すと進みます"),
      enter: () => setupGuideTopTab("instances"),
      target: () => [$("#assistant-toggle-btn")],
      autoNext: () => Boolean($("#assistant-panel")),
    },
    {
      title: _t("相談の対象"),
      anchor: () => $("#assistant-panel"),
      body: _t("「対象」で、どのインスタンスの設定について相談するかを選びます。ここでは「{0}」を選んでおきました（前の相談が残っていたら、消しておきました）。\nその下に、回答にどのインスタンスの LLM を使うかが出ます（管理者設定の「回答に使う LLM」）。", setupGuideLabel(name)),
      skip,
      cardClass,
      enter: () => {
        // 前に開いた相談がこのタブに残っていると、送った・回答が届いたの見分けがずれるため、先に消す（「会話をクリア」と同じ）。
        if (typeof assistantAbort !== "undefined" && assistantAbort) assistantAbort.abort();
        if (typeof assistantState !== "undefined" && assistantState.messages.length) {
          assistantState.messages = [];
          saveAssistantState();
          renderAssistantMessages();
        }
        const select = $("#assistant-instance");
        if (select && [...select.options].some((o) => o.value === name) && select.value !== name) {
          select.value = name;
          select.dispatchEvent(new Event("change"));
        }
      },
      target: () => [$("#assistant-panel .assistant-target"), $("#assistant-llm-note")],
    },
    {
      title: _t("相談を送ってみる"),
      anchor: () => $("#assistant-panel"),
      body: _t("入力欄に、例の相談を入れました。「送信」を押してください（文章は自由に変えて構いません。Enter でも送れます）。\n送ると、自動で次に進みます。"),
      skip,
      cardClass,
      waitLabel: _t("「送信」を押すと進みます"),
      enter: () => {
        const list = assistantGuideMessages();
        assistantGuideState.userCount = list.filter((m) => m.role === "user").length;
        assistantGuideState.sentLength = list.length;
        const input = $("#assistant-input");
        if (input && !input.value.trim()) {
          input.value = _t("チャット画面の応答が遅いときに、どの設定を見直せばよいですか？");
          input.focus();
        }
      },
      target: () => [$("#assistant-form"), $("#assistant-send-btn")],
      autoNext: () => {
        const list = assistantGuideMessages();
        const sent = list.filter((m) => m.role === "user").length > assistantGuideState.userCount;
        if (sent) assistantGuideState.sentLength = list.length;
        return sent;
      },
    },
    {
      title: _t("AI が考えています"),
      anchor: () => $("#assistant-panel"),
      body: _t("AI が、関係する設定と説明を探して回答を作っています。考えている間は経過の秒数が出て、回答は届いた所から少しずつ表示されます。思考するモデルでは、最初の文字が出るまで少しかかることがあります。\n止めたいときは「停止」を押します。回答が終わると、自動で次に進みます。"),
      skip,
      cardClass,
      noBack: true,
      waitLabel: _t("回答が終わると進みます"),
      target: () => [$("#assistant-messages"), $("#assistant-stop-btn:not(.hidden)")],
      autoNext: () => {
        if (!assistantGuideAnswered()) return false;
        assistantGuideState.errorLength = assistantGuideMessages().length;
        return true;
      },
    },
    {
      title: _t("回答が得られませんでした"),
      anchor: () => $("#assistant-panel"),
      body: _t("LLM から回答が得られませんでした（理由はパネルに出ています）。多くは「回答に使う LLM」の接続先が動いていないことが原因です。次の順に直してください。\n1. 上部の「管理者設定」タブを開き、「回答に使う LLM」で推論サーバーが動いている接続先のインスタンスを選ぶ\n2. 「接続を確認」で「✓ 接続できました」になるのを確かめる\n3. 画面の下の「保存」を押す\n4. 「AIに相談」の入力欄に、もう一度相談を書いて送る\n回答が届くと、自動で次に進みます（「次へ」で、直さずに先へ進むこともできます）。"),
      skip: () => skip() || !(assistantGuideLast() && assistantGuideLast().error),
      cardClass,
      noBack: true,
      target: () => [$('.tab-btn[data-view="admin-settings"]'), $('.admin-setting[data-setting="admin.assistant_llm_instance"]'), $("#assistant-form")],
      autoNext: () => {
        const last = assistantGuideLast();
        return assistantGuideAnswered() && !last.error && assistantGuideMessages().length > assistantGuideState.errorLength;
      },
    },
    {
      title: _t("回答が届きました"),
      anchor: () => $("#assistant-panel"),
      body: _t("回答の中で触れた設定には「[セクション] キー を開く」のボタンが付き、押すとそのキーを config.ini 画面で開きます。「参考にした説明」を開くと、AI が根拠にした説明が分かります。\nAI は助言するだけで、設定の変更・保存・起動停止はしません。回答は誤ることがあるので、変えるときは「変更を確認」で確かめてから保存してください。「会話をクリア」で相談をやり直せます。"),
      skip: () => skip() || Boolean(assistantGuideLast() && assistantGuideLast().error),
      cardClass,
      noBack: true,
      target: () => [$("#assistant-messages")],
    },
    {
      title: _t("会話をクリアする"),
      body: _t("相談の内容は、このタブを閉じるまで残り、続けて質問すると前のやり取りを踏まえて答えます。別の困りごとを相談するときは、前の話に引きずられないよう、会話をクリアしてから始めます。\n光っている「会話をクリア」を押してください。相談が消えると、自動で次に進みます。"),
      skip,
      cardClass,
      noBack: true,
      anchor: () => $("#assistant-panel"),
      waitLabel: _t("「会話をクリア」を押すと進みます"),
      target: () => [$("#assistant-clear-btn")],
      autoNext: () => assistantGuideMessages().length === 0,
    },
  ];
}

/** 「管理者設定」タブの項目の手順（画面の上から順）。setting は admin_settings.js の data-setting。 */
function setupGuideAdminSettingSteps() {
  const steps = [
    {
      setting: "vector_db.ingest_workers",
      title: _t("取り込みの並列数（[vector_db] ingest_workers）"),
      body: _t("全インスタンス・全コレクションの取り込みの合計で、同時に準備（読み取り・本文の抽出・画像化）するファイル数です。PC の CPU・メモリーに余裕があれば増やすと取り込みが速くなります。"),
    },
    {
      setting: "vector_db.ingest_processes",
      title: _t("本文の抽出・画像化の子プロセス数（[vector_db] ingest_processes）"),
      body: _t("0 なら子プロセスを使わず、取り込みの並列数のスレッドで行います。PDF・Excel 等が多く CPU に余裕があるときは、この PC のコア数（項目の説明に出ます）を目安に増やすと速くなります。1 以上にすると、同時に準備するファイル数もこの値になります。"),
    },
    {
      setting: "vector_db.ingest_embed_requests",
      title: _t("埋め込みのリクエストの並列数（[vector_db] ingest_embed_requests）"),
      body: _t("取り込みの合計で、埋め込みサーバーへ同時に送るリクエスト数です。埋め込みサーバー（llama-server なら --parallel）が同時に処理できる数を目安にします。0 なら取り込みの並列数と同じです。"),
    },
    {
      setting: "vector_db.scan_workers",
      title: _t("取り込み元フォルダの走査の並列数（[vector_db] scan_workers）"),
      body: _t("取り込み元のフォルダを調べるときに、同時に読むフォルダの数です。ファイルサーバー（ネットワーク共有）上のフォルダを取り込むときに効きます。ローカルのディスクだけなら既定のままで構いません。"),
    },
    {
      setting: "vector_db.max_files_per_collection",
      title: _t("1コレクションの最大ファイル数（[vector_db] max_files_per_collection）"),
      body: _t("1つのコレクションに取り込むファイル数の上限です。超えた分は取り込まず、ナレッジ画面に警告を出します。取り込みでは対象のファイルをメモリーに持つため、管理ツールのメモリーを使いすぎないための上限です。"),
    },
    {
      setting: "vector_db.max_file_mb",
      title: _t("1ファイルの上限サイズ（MB）（[vector_db] max_file_mb）"),
      body: _t("これより大きいファイルは取り込まず、失敗一覧に理由を載せます。大きな PDF・図面も取り込みたいときは大きくします。"),
    },
    {
      setting: "vector_db.office_timeout_seconds",
      title: _t("Office の PDF 書き出しの上限秒数（[vector_db] office_timeout_seconds）"),
      body: _t("Office ファイルのページを画像として取り込む（[vector_db] image_input = true で、image_input_extensions に Office の形式がある）ときに、PDF へ書き出す1回の上限秒数です。数百ページの文書で足りなければ大きくします。"),
    },
    {
      setting: "vector_db.ingest_temp_dir",
      title: _t("取り込みの一時ファイルの置き場所（[vector_db] ingest_temp_dir）"),
      body: _t("取り込みの間だけ使う一時ファイル（ページ画像・取り出した画像・Office から書き出した PDF）を置くフォルダです。数百ページの PDF ではページ画像が数百 MB になるため、システムドライブの空きが少ないときは別のローカルドライブのフォルダを指定します。"),
    },
    {
      setting: "vector_db.qdrant_max_retries",
      title: _t("Qdrant への送り直しの回数（[vector_db] qdrant_max_retries）"),
      body: _t("[vector_db] backend = qdrant のとき、Qdrant へ接続できない・タイムアウトした等のときに送り直す回数です。-1 なら成功するまで送り直し（取り込みはナレッジ画面の「停止」で止められます）、0 なら送り直しません。sqlite_vec なら関係ありません。"),
    },
    {
      setting: "admin.assistant_llm_instance",
      title: _t("AIに相談: 回答に使う LLM（[admin] assistant_llm_instance）"),
      body: _t("右上の「AIに相談」（管理アシスタント）の回答を作る LLM を、どのインスタンスの [llm] main_url（最初の手順で設定した、推論サーバーの接続先）から取るかです。ここが動いていないと「AIに相談」は答えられないため、次の手順から1つずつ確実に設定します。\n一覧の各インスタンスの横には「モデル名 @ 接続先の URL」が出ます。"),
    },
    {
      setting: "admin.assistant_llm_instance",
      title: _t("AIに相談: 「default」を選ぶ"),
      body: _t("光っている選択欄を押して、「default」を選んでください（default は、最初に作られる、いつも動かしておくインスタンスです）。\n選ぶと、自動で次に進みます。"),
      waitLabel: _t("「default」を選ぶと進みます"),
      target: () => [setupGuideLlmSelect()],
      autoNext: () => {
        const select = setupGuideLlmSelect();
        return Boolean(select) && select.value === "default";
      },
    },
    {
      setting: "admin.assistant_llm_instance",
      title: _t("AIに相談: 「接続を確認」を押す"),
      body: _t("光っている「接続を確認」を押してください。default の接続先へ問い合わせて（モデルは動かしません）、つながるかを確かめます。\n結果がボタンの下に出たら、自動で次に進みます。"),
      waitLabel: _t("「接続を確認」を押すと進みます"),
      target: () => [$('.admin-setting [data-guide="llm-check"]')],
      autoNext: () => setupGuideLlmCheckState() !== null,
    },
    {
      setting: "admin.assistant_llm_instance",
      title: _t("AIに相談: つながらないとき"),
      body: _t("「✗」と理由が出ました。default の接続先に LLM からの応答がありません。次を確かめてください。\n・推論サーバー（llama-server 等）が起動しているか\n・default の config.ini 画面の [llm] main_url の base_url（例: http://localhost:8080/v1）が、推論サーバーの待ち受けと合っているか（直したら保存する）\n直したら、もう一度「接続を確認」を押してください。「✓ 接続できました」になると、自動で次に進みます（「次へ」で、直さずに先へ進むこともできます）。"),
      skip: () => setupGuideLlmCheckState() !== "error",
      noBack: true,
      target: () => [$('.admin-setting [data-guide="llm-check"]'), $('.admin-setting [data-guide="llm-check-result"]')],
      autoNext: () => setupGuideLlmCheckState() === "ok",
      beforeLeave: () =>
        setupGuideLlmCheckState() === "ok" ||
        confirm(_t("まだ「✓ 接続できました」になっていません。このまま進むと、最後の「AIに相談」の体験で回答が得られないことがあります。進みますか？")),
    },
    {
      setting: "admin.assistant_llm_instance",
      title: _t("AIに相談: 接続できました"),
      body: _t("「✓ 接続できました」と出ました。「AIに相談」は default の LLM で回答します。\nこの設定は、この画面の最後の手順で「保存」すると効きます。"),
      skip: () => setupGuideLlmCheckState() !== "ok",
      noBack: true,
      target: () => [$('.admin-setting [data-guide="llm-check-result"]')],
    },
    {
      setting: "admin.assistant_embedding_instance",
      title: _t("AIに相談: 関係する設定を探す埋め込み（[admin] assistant_embedding_instance）"),
      body: _t("相談内容に近い設定・説明を探すのに使う埋め込みサーバーを、どのインスタンスの [vector_db] embedding_url から取るかです。空なら自動で選びます（回答に使う LLM のインスタンス → 相談対象 → ほかのインスタンスの順）。見つからないときは文字の重なりだけで探します。"),
    },
  ];
  return steps.map((step) => ({
    ...step,
    enter: () => setupGuideOpenAdminSettings(),
    target: step.target || (() => [$(`.admin-setting[data-setting="${CSS.escape(step.setting)}"]`)]),
  }));
}

/** 管理者設定の「回答に使う LLM」の選択欄。 */
function setupGuideLlmSelect() {
  return $('.admin-setting[data-setting="admin.assistant_llm_instance"] select');
}

/** 「接続を確認」の結果（"ok" / "error"。まだ押していない・確認中なら null）。admin_settings.js が結果の欄に付けるクラスで見分ける。 */
function setupGuideLlmCheckState() {
  const result = $('.admin-setting [data-guide="llm-check-result"]');
  if (!result || !result.textContent.trim()) return null;
  if (result.classList.contains("ok-text")) return "ok";
  if (result.classList.contains("error")) return "error";
  return null;
}

/* ---------------------------------------------------------- 画面の切り替え */

/** 「管理者設定」タブを開く（もう開いていれば作り直さない。入力中の値を消さないため）。 */
async function setupGuideOpenAdminSettings() {
  setupGuideSetTopTabActive("admin-settings");
  if (!$(".admin-settings")) await navigate("admin-settings");
}

function setupGuideSetTopTabActive(view) {
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
}

async function setupGuideTopTab(view) {
  setupGuideSetTopTabActive(view);
  await navigate(view);
}

/** インスタンス詳細の指定のサブタブを開く（もう開いていれば作り直さない。編集中の config.ini の変更を消さないため）。 */
async function setupGuideOpenSubview(name, view) {
  setupGuideSetTopTabActive("instances");
  if (!$("#instance-subview") || currentInstanceName !== name) {
    await renderInstanceDetail(name, view);
    return;
  }
  const active = $(".subtab-btn.active");
  const opened = view === "config" ? !!$("#config-keys") : active && active.dataset.subview === view;
  if (!opened) await selectInstanceSubview(view);
}

/** config.ini の画面で、その手順の設定だけを表示する。 */
async function setupGuideShowConfig(name, step) {
  await setupGuideOpenSubview(name, "config");
  if (!$("#config-keys")) return;
  const search = $("#config-search");
  if (search) search.value = "";
  const changedOnly = $("#config-changed-only");
  if (changedOnly) changedOnly.checked = false;
  selectSection("");
  // 「並列数」は既存のプリセットボタンそのものを押した状態にする（ボタンの場所も覚えてもらうため）。
  const preset = step.preset ? CONFIG_PRESETS.find((p) => p.name === step.preset) : null;
  activePreset = preset || { name: "setup-guide", keys: step.keys || [], tokenRatio: !!step.tokenRatio };
  $$("#preset-list button").forEach((b) => b.classList.toggle("active", !!preset && b.textContent === preset.name));
  renderKeyList();
}

function setupGuideConfigTargets(step) {
  const keys = step.keys || (CONFIG_PRESETS.find((p) => p.name === step.preset) || { keys: [] }).keys;
  const targets = keys.map((id) => $(`#config-keys [data-key-id="${CSS.escape(id)}"]`));
  if (step.preset) targets.unshift($$("#preset-list button").find((b) => b.textContent === step.preset));
  if (step.tokenRatio) targets.push($("#config-keys .token-ratio-banner"));
  return targets;
}

/** 最後の手順: 「開く」を押したらガイドを終える（リンクはそのまま開く）。 */
function setupGuideWatchOpenLink(name) {
  if (!setupGuide || setupGuide.clickHandler) return;
  setupGuide.clickHandler = (ev) => {
    const link = ev.target.closest && ev.target.closest(".open-link");
    const card = link && link.closest(".instance-card");
    if (card && card.dataset.instance === name) finishSetupGuide("finished");
  };
  document.addEventListener("click", setupGuide.clickHandler, true);
}

/* ---------------------------------------------------------- ガイドの進行 */

function startSetupGuide(name) {
  setupGuideOffered.add(name);
  startGuide({
    name,
    heading: _t("はじめてのチュートリアル"),
    label: setupGuideLabel(name),
    skipTitle: _t("チュートリアルを終えます（次からは自動で出しません）"),
    steps: setupGuideSteps(name),
    onDone: (result) => markSetupGuideDone(name, result),
  });
}

/**
 * 説明のカードと強調で画面を案内するガイドを始める（はじめてのチュートリアルと、スキル研究室のチュートリアル skill_lab_tutorial.js が使う）。
 * opts: { name, heading（カードの見出し）, label（見出しの横に出す対象の名前）, skipTitle, steps, onDone(result) }。
 * steps の各要素: { title, body（\n で段落を分ける）, enter()（画面を開く。async 可）, target()（強調する要素の配列）, beforeLeave()（false で「次へ」を止める）,
 *   skip()（真なら飛ばす）, autoNext()（真になったら自動で次へ。強調の見直しのたびに確かめる）,
 *   waitLabel（あれば「次へ」を押せなくし、ボタンにこの文言を出す。autoNext で進む手順用）, noBack（「戻る」を押せない）,
 *   cardClass（説明のカードに足すクラス。AIに相談のパネルを開いている間はカードをその左へ寄せる等）,
 *   anchor()（カードを置く基準の要素。省略すると強調する最初の要素） }。
 */
function startGuide(opts) {
  if (setupGuide) closeSetupGuide();
  const card = el("div", { class: "setup-guide-card", role: "dialog" });
  document.body.appendChild(card);
  document.body.classList.add("setup-guide-open");
  setupGuide = { ...opts, index: 0, card, timer: null, targets: [], scrolled: false, clickHandler: null, collapsed: false, advancing: false };
  setupGuide.timer = setInterval(setupGuideRefreshHighlight, SETUP_GUIDE_POLL_MS);
  setupGuide.onResize = () => setupGuideRefreshHighlight();
  window.addEventListener("resize", setupGuide.onResize);
  setupGuideGo(0);
}

// dir: 進む向き（1 = 次へ、-1 = 戻る）。skip() が真の手順（スキル研究室チュートリアルの、ワーカーが既にあるときの作成手順等）は飛ばす。
async function setupGuideGo(index, dir = 1) {
  const guide = setupGuide;
  if (!guide) return;
  const skipped = (i) => i >= 0 && i < guide.steps.length && guide.steps[i].skip && guide.steps[i].skip();
  while (skipped(index)) index += dir;
  // 戻る向きで先頭まで飛ばした（先頭も飛ばす手順）なら、元の位置から次へ向かって探し直す。
  if (index < 0) {
    index = 0;
    while (skipped(index)) index += 1;
  }
  guide.index = Math.max(0, Math.min(index, guide.steps.length - 1));
  guide.scrolled = false;
  guide.advancing = false;
  setupGuideClearHighlight();
  if (guide.clickHandler) {
    document.removeEventListener("click", guide.clickHandler, true);
    guide.clickHandler = null;
  }
  setupGuideRenderCard();
  const step = guide.steps[guide.index];
  try {
    if (step.enter) await step.enter();
  } catch (e) {
    // 画面を開けなくても（インスタンスの設定エラー等）、説明は読めるようにする。
    console.warn("setup guide:", e);
  }
  if (setupGuide === guide) setupGuideRefreshHighlight();
}

function setupGuideRenderCard() {
  const guide = setupGuide;
  const step = guide.steps[guide.index];
  const last = guide.index === guide.steps.length - 1;
  const head = el("div", { class: "setup-guide-head" }, [
    el("strong", { text: guide.heading }),
    el("span", { class: "hint", text: `${guide.index + 1} / ${guide.steps.length}${guide.label ? `・${guide.label}` : ""}` }),
  ]);
  const collapseBtn = el("button", { type: "button", class: "link-btn", title: guide.collapsed ? _t("広げる") : _t("たたむ"), text: guide.collapsed ? "▴" : "▾" });
  collapseBtn.setAttribute("data-no-busy", "");
  collapseBtn.addEventListener("click", () => {
    guide.collapsed = !guide.collapsed;
    setupGuideRenderCard();
  });
  head.appendChild(collapseBtn);

  const body = el("div", { class: "setup-guide-body" }, [
    el("h4", { text: step.title }),
    ...step.body.split("\n").map((line) => el("p", { text: line })),
  ]);

  const skipBtn = el("button", { type: "button", class: "link-btn", text: _t("スキップする") });
  if (guide.skipTitle) skipBtn.title = guide.skipTitle;
  skipBtn.addEventListener("click", () => finishSetupGuide("skipped"));
  const backBtn = el("button", { type: "button", text: _t("戻る") });
  backBtn.disabled = guide.index === 0 || Boolean(step.noBack);
  backBtn.addEventListener("click", () => setupGuideGo(guide.index - 1, -1));
  const nextBtn = el("button", { type: "button", class: "primary", text: step.waitLabel || (last ? _t("完了") : _t("次へ")) });
  // 操作を待つ手順（送信・回答等）は、終わると autoNext で自動で進む。その間は「次へ」を押せない。
  nextBtn.disabled = Boolean(step.waitLabel);
  nextBtn.addEventListener("click", () => {
    if (step.beforeLeave && !step.beforeLeave()) return;
    if (last) finishSetupGuide("finished");
    else setupGuideGo(guide.index + 1);
  });
  for (const b of [skipBtn, backBtn, nextBtn]) b.setAttribute("data-no-busy", "");
  const actions = el("div", { class: "setup-guide-actions" }, [skipBtn, backBtn, nextBtn]);

  const kept = ["setup-guide-card--anchored", "setup-guide-card--behind"].filter((c) => guide.card.classList.contains(c));
  guide.card.className = ["setup-guide-card", step.cardClass, ...kept].filter(Boolean).join(" ");
  guide.card.classList.toggle("collapsed", guide.collapsed);
  guide.card.replaceChildren(head, body, actions);
}

function setupGuideClearHighlight() {
  if (!setupGuide) return;
  for (const t of setupGuide.targets) t.classList.remove("setup-guide-target");
  setupGuide.targets = [];
}

function setupGuideRefreshHighlight() {
  const guide = setupGuide;
  if (!guide) return;
  const step = guide.steps[guide.index];
  if (step.autoNext && !guide.advancing && guide.index < guide.steps.length - 1 && step.autoNext()) {
    guide.advancing = true;
    setupGuideGo(guide.index + 1);
    return;
  }
  const found = (step.target ? step.target() : []).filter(Boolean);
  // カードを置く基準の対象（「並列数」の手順は、左のボタンではなく最初の設定の行）
  const anchor = (step.anchor ? step.anchor() : found[found.length > 1 && step.preset ? 1 : 0]) || null;
  // 画面より高い対象（項目の多いリストの設定等）は、中央に合わせると見出し（キー名・説明）が画面の外に出て、
  // 何の設定か分からなくなるため、先頭が見えるようにスクロールする（カードは横に収まらないので右下に出て、
  // 画面の上の見出し・説明には被らない）。
  const tall = anchor ? anchor.getBoundingClientRect().height > window.innerHeight * SETUP_GUIDE_TALL_RATIO : false;
  setupGuidePlaceCard(guide.card, anchor);
  const same = found.length === guide.targets.length && found.every((t, i) => t === guide.targets[i]);
  if (same) return;
  setupGuideClearHighlight();
  for (const t of found) t.classList.add("setup-guide-target");
  guide.targets = found;
  if (anchor && !guide.scrolled) {
    guide.scrolled = true;
    anchor.scrollIntoView({ block: tall ? "start" : "center", behavior: "smooth" });
  }
}

// 説明のカードと対象の間・画面の端との間（px）。この幅以下の画面では、カードは右下の既定の位置のまま。
const GUIDE_CARD_GAP = 14;
const GUIDE_VIEWPORT_MARGIN = 12;
const GUIDE_NARROW_MAX_WIDTH = 900;

/**
 * 説明のカードを対象の近くへ置く。対象の下・上・右・左のうち、カードが画面に収まって対象と重ならない側を、
 * 空いている広さの順に選ぶ。対象が無い手順は画面の中央。どこにも収まらない（インスタンスの一覧全体等）・
 * 狭い画面なら、置き場所の指定を外して既定（右下。cardClass の指定があればそれ）に戻す。
 */
function setupGuidePlaceCard(card, target) {
  // ダイアログ（変更内容の確認等）が開いていて、案内している対象がその中に無いときは、ダイアログに被らないよう
  // ダイアログの横へ置く。横に収まらなければ、カードをダイアログの後ろへ回す（ダイアログを閉じると前に戻る）。
  const modal = $(".modal-backdrop:not(.hidden) .modal");
  const avoidModal = Boolean(modal) && !(target && modal.contains(target));
  if (avoidModal) target = modal;
  card.classList.remove("setup-guide-card--behind");
  const reset = () => {
    card.classList.remove("setup-guide-card--anchored");
    card.style.left = "";
    card.style.top = "";
    if (avoidModal) card.classList.add("setup-guide-card--behind");
  };
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  if (vw <= GUIDE_NARROW_MAX_WIDTH) return reset();
  const w = card.offsetWidth;
  const h = card.offsetHeight;
  const clamp = (v, min, max) => Math.max(min, Math.min(v, Math.max(min, max)));
  const put = (left, top) => {
    card.classList.add("setup-guide-card--anchored");
    card.style.left = `${Math.round(clamp(left, GUIDE_VIEWPORT_MARGIN, vw - w - GUIDE_VIEWPORT_MARGIN))}px`;
    card.style.top = `${Math.round(clamp(top, GUIDE_VIEWPORT_MARGIN, vh - h - GUIDE_VIEWPORT_MARGIN))}px`;
  };
  if (!target) return put((vw - w) / 2, (vh - h) / 2);
  const r = target.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return reset();
  const need = (size) => size + GUIDE_CARD_GAP + GUIDE_VIEWPORT_MARGIN;
  const sides = [
    { space: vh - r.bottom, fits: vh - r.bottom >= need(h), left: r.left + r.width / 2 - w / 2, top: r.bottom + GUIDE_CARD_GAP },
    { space: r.top, fits: r.top >= need(h), left: r.left + r.width / 2 - w / 2, top: r.top - GUIDE_CARD_GAP - h },
    { space: vw - r.right, fits: vw - r.right >= need(w), left: r.right + GUIDE_CARD_GAP, top: r.top + r.height / 2 - h / 2 },
    { space: r.left, fits: r.left >= need(w), left: r.left - GUIDE_CARD_GAP - w, top: r.top + r.height / 2 - h / 2 },
  ]
    .filter((side) => side.fits)
    .sort((a, b) => b.space - a.space);
  if (!sides.length) return reset();
  put(sides[0].left, sides[0].top);
}

function closeSetupGuide() {
  const guide = setupGuide;
  if (!guide) return;
  clearInterval(guide.timer);
  if (guide.onResize) window.removeEventListener("resize", guide.onResize);
  if (guide.clickHandler) document.removeEventListener("click", guide.clickHandler, true);
  setupGuideClearHighlight();
  guide.card.remove();
  setupGuide = null;
  document.body.classList.remove("setup-guide-open");
  // config.ini の画面に仮のプリセットが残っていたら外す（すべてのキーの表示に戻す）。
  if (activePreset && activePreset.name === "setup-guide" && $("#config-keys")) {
    clearPreset();
    renderKeyList();
  }
}

/** ガイドを終える（result: finished / skipped）。終えたあとの処理（onDone）を呼ぶ。 */
async function finishSetupGuide(result) {
  const guide = setupGuide;
  closeSetupGuide();
  if (guide && guide.onDone) await guide.onDone(result);
}

/** はじめてのチュートリアルを終えた印を付ける（次からは自動で出さない）。 */
async function markSetupGuideDone(name, result) {
  const meta = instanceCache.find((i) => i.name === name);
  if (meta) meta.tutorial_pending = false;
  try {
    await api(`/api/instances/${encodeURIComponent(name)}/tutorial/done`, { method: "POST", body: { result } });
  } catch (e) {
    console.warn("setup guide:", e);
  }
}
