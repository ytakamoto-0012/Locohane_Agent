"use strict";
/* 初期設定ガイド（チュートリアル）。
 *
 * 初回起動（instances/ が無く default を作ったとき）と、本体インスタンスを新しく作ったときに、
 * 使い始めるのに要る設定を順番に説明しながら案内する。サーバーは instances/<名前>/.setup_guide_pending の
 * 有無を一覧の setup_guide_pending で返し（admin/instances.py）、ガイドを終える・スキップすると
 * POST /api/instances/{name}/setup-guide/done で消す。
 *
 * 画面を新しく作らず、今のダッシュボードの画面（config.ini・ユーザー・ナレッジ・管理者設定・インスタンス一覧）を
 * ガイドが開き、説明のカード（右下）と、対象の場所の強調で案内する。config.ini の手順では、その手順の設定だけを
 * 絞り込んで表示する（admin.js の activePreset に仮のプリセットを入れる）。変更は最後の「保存」の手順でまとめて保存する。
 * admin.js の後に読み込む（$・api・renderInstanceDetail 等を使う）。
 */

// 強調する場所を探し直す間隔（ms）。画面の作り直し（renderKeyList 等）で要素が入れ替わっても強調を付け直すため。
const SETUP_GUIDE_POLL_MS = 600;
// 一度出したインスタンス（ページを開いている間は、同じインスタンスで自動では出し直さない）。
const setupGuideOffered = new Set();
let setupGuide = null; // { name, steps, index, card, timer, targets, scrolled, clickHandler }

/** 初回起動・新しく作ったインスタンスなら、ガイドを始める（renderInstances から呼ぶ）。 */
function maybeStartSetupGuide() {
  if (setupGuide) return;
  const pending = instanceCache.filter((i) => i.setup_guide_pending && i.kind !== "skill_lab" && !setupGuideOffered.has(i.name));
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
    {
      title: _t("初期設定ガイド"),
      body: _t("インスタンス「{0}」を使い始めるのに要る設定を、順番に案内します。各手順では、その設定だけを絞り込んで表示します。値を確かめ、必要なら変えてから「次へ」を押してください。各設定の下の説明もあわせて読んでください。\n変更は、設定の手順の最後でまとめて保存します（それまでは保存されません）。\n「スキップする」でいつでも終えられます。あとから、インスタンスの設定画面の「初期設定ガイド」でもう一度始められます。", label),
      enter: () => setupGuideOpenSubview(name, "config"),
      target: () => [],
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
    {
      title: _t("チャット画面を開く"),
      body: _t("「{0}」のカードの「開く ↗」を押すと、チャット画面が新しいタブで開きます（押すとガイドは終わります）。\n開けないときは、状態が「稼働中」になっているかを確かめてください。\nログインを求められたら、「ユーザー」タブで登録したユーザー名でログインします。", label),
      enter: async () => {
        await setupGuideTopTab("instances");
        setupGuideWatchOpenLink(name);
      },
      target: () => [$(`.instance-card[data-instance="${CSS.escape(name)}"] .open-link`)],
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
      body: _t("右上の「AIに相談」（管理アシスタント）の回答を作る LLM を、どのインスタンスの [llm] main_url から取るかです。空なら相談対象のインスタンス自身の LLM を使います。\nインスタンスを選んで「接続を確認」を押すと、その LLM につながるかを確かめられます。最初のセットアップでは相談対象の LLM がまだ動いていないことがあるため、動いている LLM を持つインスタンスを選んでおくと詰まりません。"),
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
    target: () => [$(`.admin-setting[data-setting="${CSS.escape(step.setting)}"]`)],
  }));
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
    heading: _t("初期設定ガイド"),
    label: setupGuideLabel(name),
    skipTitle: _t("ガイドを終えます（次からは自動で出しません）"),
    steps: setupGuideSteps(name),
    onDone: (result) => markSetupGuideDone(name, result),
  });
}

/**
 * 説明のカードと強調で画面を案内するガイドを始める（初期設定ガイドと、スキル研究室のチュートリアル skill_lab_tutorial.js が使う）。
 * opts: { name, heading（カードの見出し）, label（見出しの横に出す対象の名前）, skipTitle, steps, onDone(result) }。
 * steps の各要素: { title, body（\n で段落を分ける）, enter()（画面を開く。async 可）, target()（強調する要素の配列）, beforeLeave()（false で「次へ」を止める） }。
 */
function startGuide(opts) {
  if (setupGuide) closeSetupGuide();
  const card = el("div", { class: "setup-guide-card", role: "dialog" });
  document.body.appendChild(card);
  document.body.classList.add("setup-guide-open");
  setupGuide = { ...opts, index: 0, card, timer: null, targets: [], scrolled: false, clickHandler: null, collapsed: false };
  setupGuide.timer = setInterval(setupGuideRefreshHighlight, SETUP_GUIDE_POLL_MS);
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
  backBtn.disabled = guide.index === 0;
  backBtn.addEventListener("click", () => setupGuideGo(guide.index - 1, -1));
  const nextBtn = el("button", { type: "button", class: "primary", text: last ? _t("完了") : _t("次へ") });
  nextBtn.addEventListener("click", () => {
    if (step.beforeLeave && !step.beforeLeave()) return;
    if (last) finishSetupGuide("finished");
    else setupGuideGo(guide.index + 1);
  });
  for (const b of [skipBtn, backBtn, nextBtn]) b.setAttribute("data-no-busy", "");
  const actions = el("div", { class: "setup-guide-actions" }, [skipBtn, backBtn, nextBtn]);

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
  const found = (step.target ? step.target() : []).filter(Boolean);
  const same = found.length === guide.targets.length && found.every((t, i) => t === guide.targets[i]);
  if (same) return;
  setupGuideClearHighlight();
  for (const t of found) t.classList.add("setup-guide-target");
  guide.targets = found;
  if (found.length && !guide.scrolled) {
    guide.scrolled = true;
    found[found.length > 1 && step.preset ? 1 : 0].scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

function closeSetupGuide() {
  const guide = setupGuide;
  if (!guide) return;
  clearInterval(guide.timer);
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

/** 初期設定ガイドを終えた印を付ける（次からは自動で出さない）。 */
async function markSetupGuideDone(name, result) {
  const meta = instanceCache.find((i) => i.name === name);
  if (meta) meta.setup_guide_pending = false;
  try {
    await api(`/api/instances/${encodeURIComponent(name)}/setup-guide/done`, { method: "POST", body: { result } });
  } catch (e) {
    console.warn("setup guide:", e);
  }
}
