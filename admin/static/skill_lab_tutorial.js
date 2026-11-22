"use strict";
/* スキル研究室のチュートリアル。
 *
 * 上部の「スキル研究室」タブの「スキル研究室チュートリアル」ボタン（skill_lab.js の renderSkillLab。利用者のドラフトが
 * あるインスタンスがあるときだけ押せる）から始める。ドラフトを題材に、テーマの作り方から「テーマの設定」の各項目
 * （わかりにくいので1つずつ）・スキル調整ループ・実行の記録・レビューと昇格までを順番に案内する。
 * 案内の仕組み（説明のカード・強調）は setup_guide.js の startGuide を使う。強調する場所は skill_lab.js の data-guide の目印。
 * 終えても何も記録しない（何度でも始められる）。setup_guide.js・skill_lab.js の後に使う（skill_lab.js は画面を開くときに読む）。
 */

async function startSkillLabTutorial(name, displayName) {
  // スキル調整ワーカーが既にあるかで、ワーカーの作成の手順を出すかを決める（始めた時点の状態で決め、途中で作っても変えない）。
  try {
    instanceCache = (await api("/api/instances")).instances;
  } catch (e) {
    /* 取れなければ今の一覧のまま */
  }
  const ctx = {
    hadWorker: instanceCache.some((i) => i.kind === "skill_lab"),
    initialWorkers: new Set(instanceCache.filter((i) => i.kind === "skill_lab").map((i) => i.name)),
  };
  startGuide({
    name,
    heading: _t("スキル研究室チュートリアル"),
    label: displayName && displayName !== name ? `${displayName}（${name}）` : name,
    skipTitle: _t("チュートリアルを終えます（あとから何度でも始められます）"),
    steps: skillLabTutorialSteps(name, ctx),
    onDone: null,
  });
}

/* ---------------------------------------------------------- スキル調整ワーカーの作成 */

/** このチュートリアルで案内するワーカー（途中で作ったものがあればそれ、無ければ既存の最初のもの）。 */
function labTutorialWorker(ctx) {
  const workers = instanceCache.filter((i) => i.kind === "skill_lab");
  return workers.find((w) => !ctx.initialWorkers.has(w.name)) || workers[0] || null;
}

/** インスタンス一覧を開く（もう開いていれば作り直さない。作成のダイアログを開いたままにするため）。 */
async function labTutorialOpenInstances() {
  setupGuideSetTopTabActive("instances");
  if (!$("#instance-cards")) await navigate("instances");
}

const labCreateForm = (selector) => $(`#create-instance-form ${selector}`);
const labFieldLabel = (selector) => {
  const input = labCreateForm(selector);
  return input ? input.closest("label") : null;
};

function labWorkerCard(ctx) {
  const w = labTutorialWorker(ctx);
  return w ? $(`#lab-instance-cards .instance-card[data-instance="${CSS.escape(w.name)}"]`) : null;
}

/** ワーカーの作成から起動までの手順（ワーカーが既にあれば飛ばす）。 */
function labWorkerSetupSteps(ctx) {
  const skip = () => ctx.hadWorker;
  const inForm = (title, body, target) => ({
    title,
    body,
    skip,
    enter: labTutorialOpenInstances,
    target: () => [target()],
  });
  return [
    {
      title: _t("ワーカーを作る: インスタンスの追加を開く"),
      body: _t("ここから、スキル調整ワーカーを作って動かすまでを1つずつ案内します。インスタンスの一覧に移りました。\n上の「＋ インスタンス追加」を押してください。インスタンスを追加するダイアログが開きます。開いたら「次へ」を押します。"),
      skip,
      enter: labTutorialOpenInstances,
      target: () => [$("#add-instance-btn")],
      beforeLeave: () => Boolean($("#create-instance-form")) || confirm(_t("インスタンスを追加するダイアログがまだ開いていません。開かずに、説明だけを読み進めますか？")),
    },
    inForm(
      _t("ワーカーを作る: 種類"),
      _t("「種類」で「スキル調整ワーカー（スキル研究室の実行役）」を選びます。\n選ぶと、ワーカーには要らない項目（ホスト・ポート等）が消えます。ワーカーはチャット画面を持たず、裏で評価・修正・下書きだけを行います。"),
      () => labFieldLabel('select[name="kind"]'),
    ),
    inForm(
      _t("ワーカーを作る: インスタンス名"),
      _t("ワーカーの名前を、半角英数字・_・- の6文字以内で入れます（例: lab1）。フォルダの名前に使うため、作った後は変えられません。"),
      () => labFieldLabel('input[name="name"]'),
    ),
    inForm(
      _t("ワーカーを作る: 表示名"),
      _t("一覧やテーマの画面に出る名前です（例: スキル調整ワーカー）。空欄ならインスタンス名になります。あとからカードの「編集」で変えられます。"),
      () => labFieldLabel('input[name="display_name"]'),
    ),
    inForm(
      _t("ワーカーを作る: 自動起動"),
      _t("「管理ツール起動時に自動起動する」にチェックを入れておくと、管理ツール（admin.bat）を起動するたびにワーカーも起動します。チェックしないと、使う前に毎回カードの「起動」を押す必要があります。ふだんはチェックを入れておくのがおすすめです。"),
      () => labFieldLabel('input[name="autostart"]'),
    ),
    inForm(
      _t("ワーカーを作る: 複製元"),
      _t("「複製元」で、本番で使っているインスタンス（例: default）を選ぶと、そのインスタンスの LLM の接続先（[llm] main_url 等）がワーカーに写ります。ワーカーは本番と同じモデルで試験を解く必要があるため、選んでおくと設定の手間が減ります。\nワーカーに写るのは LLM の接続先・ワーカー自身のログの設定だけで、ほかの設定は写りません。「（複製しない）」のままでも、次の手順で設定できます。"),
      () => labFieldLabel('select[name="copy_from"]'),
    ),
    {
      title: _t("ワーカーを作る: 作成"),
      body: _t("入れ終わったら「作成」を押します。エラーが出たら、ダイアログの下に理由が出ます（名前が7文字以上・すでにある名前 等）。\n作成すると、インスタンス一覧の下の「スキル調整ワーカー」の欄にカードが出ます。出たら「次へ」を押します。"),
      skip,
      enter: labTutorialOpenInstances,
      target: () => [labCreateForm('button[type="submit"]'), labWorkerCard(ctx)],
      beforeLeave: () => Boolean(labTutorialWorker(ctx)) || confirm(_t("スキル調整ワーカーがまだ作られていません。作らずに、説明だけを読み進めますか？")),
    },
    {
      title: _t("ワーカーを設定する: LLM の接続先"),
      body: _t("作ったワーカーの設定画面を開きました。ここでは、ワーカーが使う LLM の接続先 [llm] main_url だけを表示しています。\n研究するスキルを本番で動かすのと同じモデルにしてください（モデルが違うと、試験に合格しても本番で同じように動くとは限りません）。複製元を選んだなら、本番の接続先がもう入っています。\n本番の利用者と同じ推論サーバーを使うと、評価の間は本番の利用者が待たされることがあります。別の推論サーバー（同じモデル）を用意できるなら、そちらを指定すると本番に影響しません。"),
      skip,
      enter: async () => {
        const w = labTutorialWorker(ctx);
        if (w) await setupGuideShowConfig(w.name, { keys: ["llm.main_url"] });
      },
      target: () => [$('#config-keys [data-key-id="llm.main_url"]')],
    },
    {
      title: _t("ワーカーを設定する: 保存"),
      body: _t("変えたら「変更を確認」で内容を確かめ、「この内容で保存」を押します（「保存」で直接保存もできます）。何も変えていなければ、そのまま「次へ」で進めます。\nワーカーの設定には、ほかにループの進め方（[skill_lab_worker]）とワーカーのログ（[log]）がありますが、ふだんは既定のままで構いません。"),
      skip,
      enter: async () => {
        const w = labTutorialWorker(ctx);
        if (w) await setupGuideShowConfig(w.name, { keys: ["llm.main_url"] });
      },
      target: () => [$("#preview-btn"), $("#save-btn")],
      beforeLeave: () => {
        if (!$("#config-keys")) return true;
        const count = Object.keys(dirtyEdits).length + resetKeys.size;
        return count === 0 || confirm(_t("保存していない変更が {0} 件あります。保存しないまま進むと、変更は消えます。進みますか？", count));
      },
    },
    {
      title: _t("ワーカーを起動する"),
      body: _t("インスタンスの一覧に戻りました。下の「スキル調整ワーカー」の欄の、作ったワーカーのカードの「起動」を押してください（もう動いていて設定を変えたなら「再起動」）。\n状態が「稼働中」になったら「次へ」を押します。うまく起動しないときは、カードの「設定」→「ログ」タブに理由が出ます。"),
      skip,
      enter: labTutorialOpenInstances,
      target: () => {
        const card = labWorkerCard(ctx);
        if (!card) return [];
        const restart = card.querySelector(".btn-restart");
        const start = card.querySelector(".btn-start");
        return [restart && !restart.disabled ? restart : start && !start.disabled ? start : restart, card.querySelector(".state-label")];
      },
    },
  ];
}

/* ---------------------------------------------------------- 画面の切り替え */

/** 上部の「スキル研究室」タブで、このインスタンスを選んだ状態にする（もう開いていれば作り直さない）。 */
async function labTutorialOpenTop(name) {
  setupGuideSetTopTabActive("skill-lab");
  if (!$(".lab-instance-list")) {
    // 開き直すときに最初からこのインスタンスを選ばせる（別のインスタンスのパネルと描画が競合しないように）。
    labSelectedInstance = name;
    await navigate("skill-lab");
  }
  const item = $(`.lab-instance-item[data-instance="${CSS.escape(name)}"]`);
  if (item && !item.classList.contains("active")) item.click();
}

/** テーマの一覧（インスタンスのパネル）に戻す。テーマを開いていたら「← テーマ一覧」を押す。 */
async function labTutorialOpenPanel(name) {
  await labTutorialOpenTop(name);
  const back = $(".lab-panel > .back-btn");
  if (back) back.click();
}

/** 開いているテーマの指定のサブタブにする（テーマを開いていなければ何もしない。説明だけ読める）。 */
async function labTutorialOpenThemeTab(name, sub) {
  setupGuideSetTopTabActive("skill-lab");
  const btn = $(`.lab-panel .theme-subtabs .subtab-btn[data-subview="${sub}"]`);
  if (btn && !btn.classList.contains("active")) btn.click();
}

const labGuide = (key) => $(`.lab-panel [data-guide="${key}"]`);
const labSection = (key) => $(`.lab-panel .theme-section-${key}`);

/** 「テーマの設定」タブの手順。targets() は強調する要素（節・data-guide の目印）を返す。 */
function labThemeStep(name, title, body, targets) {
  return { title, body, enter: () => labTutorialOpenThemeTab(name, "overview"), target: () => targets().filter(Boolean) };
}

/* ---------------------------------------------------------- 手順 */

function skillLabTutorialSteps(name, ctx) {
  return [
    {
      title: _t("スキル研究室とは"),
      body: _t("利用者がチャットで作ったスキル（ドラフト）や、今あるスキル・サブエージェントを、本番で使えるように仕上げる場所です。\n「研究テーマ」を作り、① 直すスキル、② スキルにしてほしいこと（採点の基準）、③ 試験の問題（ケース）を入れて「スキル調整ループを開始」を押すと、AI が試験を解き、不合格ならスキルを直して解き直します。最後に人が確かめて「昇格」すると、そのインスタンスの正式なスキルになります。\nこのチュートリアルでは、利用者のドラフトを題材に、順番に案内します。"),
      enter: () => labTutorialOpenPanel(name),
      target: () => [$(".lab-tutorial-btn")],
    },
    {
      title: _t("スキル調整ワーカー（実行役）"),
      body: ctx.hadWorker
        ? _t("試験を解く・スキルを直す・下書きを書くといった AI の作業は、「スキル調整ワーカー」という専用のインスタンスが裏で行います。左下に一覧と、動いているか（稼働中・停止中）が出ます。\nこの環境にはワーカーがもうあるので、作り方の手順は省きます。「停止中」なら、インスタンス一覧のワーカーのカードの「起動」を押してから使ってください。")
        : _t("試験を解く・スキルを直す・下書きを書くといった AI の作業は、「スキル調整ワーカー」という専用のインスタンスが裏で行います。左下に一覧が出ます。\nこの環境にはまだワーカーがありません。ワーカーが無くてもテーマは作れますが、評価・修正・下書きは動きません。次の手順から、ワーカーを作って動かすまでを1つずつ案内します。"),
      enter: () => labTutorialOpenPanel(name),
      target: () => [$(".lab-labs")],
    },
    ...labWorkerSetupSteps(ctx),
    {
      title: _t("対象インスタンス"),
      body: _t("テーマはインスタンスごとに分かれます。左の一覧で、スキルを使うインスタンスを選びます（ドラフト・進行中・実行中・レビュー待ちの数が出ます）。\n昇格したスキルは、そのインスタンス専用の置き場に入ります。ほかのインスタンスには入りません（昇格のときに配布もできます）。"),
      enter: () => labTutorialOpenPanel(name),
      target: () => [$(`.lab-instance-item[data-instance="${CSS.escape(name)}"]`)],
    },
    {
      title: _t("利用者のドラフトからテーマを作る"),
      body: _t("「利用者のドラフト」の一覧に、利用者がチャットで作った試作のスキルが出ます。ドラフトの行の「テーマを作って取り込む」を押すと、そのドラフトを研究対象にしたテーマができ、テーマの画面が開きます。\nもう取り込み済み（「テーマに取り込み済み」）なら、上の「研究テーマ」の一覧からそのテーマを開いてください。ドラフトを使わずに一から作るときは「＋ テーマを作成」です。\nテーマの画面が開いたら「次へ」を押します。"),
      enter: () => labTutorialOpenPanel(name),
      target: () => [labGuide("drafts"), labGuide("themes"), labGuide("create-theme")],
      beforeLeave: () => Boolean($(".lab-panel .theme-header")) || confirm(_t("テーマの画面がまだ開いていません。テーマを開かずに、説明だけを読み進めますか？（テーマを開くと、説明している場所を強調して案内します）")),
    },
    {
      title: _t("テーマの見出し"),
      body: _t("上にテーマの名前と状態（準備中・実行中・レビュー待ち・昇格済み 等）、担当のスキル調整ワーカー、反復の回数が出ます。入れていないものがあると「開始に必要なもの」として黄色の枠で知らせます。\nその下のボタンで、スキル調整ループの開始・トライアウトだけの実行・取りやめ・削除をします（実行中は「停止」）。開始は、このあと説明する設定をすべて入れてから行います。"),
      enter: () => labTutorialOpenThemeTab(name, "overview"),
      target: () => [$(".lab-panel .theme-header")],
    },
    {
      title: _t("テーマの画面のタブ"),
      body: _t("「テーマの設定」で研究の中身を入れ、「実行の記録」で評価・修正の経過を見て、「レビュー・昇格」で結果を確かめて正式なスキルにします。「履歴」には、このテーマで行った操作が残ります。\nまずは「テーマの設定」を、上から順に入れていきます。各節の入力は、その節の「保存」を押すまで保存されません。保存しないままタブを切り替えると、入力は消えます。"),
      enter: () => labTutorialOpenThemeTab(name, "overview"),
      target: () => [$(".lab-panel .theme-subtabs")],
    },
    labThemeStep(
      name,
      _t("テーマの設定: テーマ名"),
      _t("テーマの一覧に出る名前です。ドラフトから作ったときは「作成者/スキル名」になっています。わかりやすい名前に変えて構いません（スキルの名前は変わりません）。"),
      () => [labGuide("theme-title")],
    ),
    labThemeStep(
      name,
      _t("テーマの設定: 担当のスキル調整ワーカー"),
      _t("このテーマの評価・修正・下書きを行うワーカーです。ワーカーが1つだけなら「（自動: …）」のままで構いません。複数あるときは、研究するスキルを本番で動かすのと同じモデルのワーカーを選びます（モデルが違うと、試験に合格しても本番で同じように動くとは限りません）。\nループの実行中・開始待ちの間は変えられません。"),
      () => [labGuide("theme-lab")],
    ),
    labThemeStep(
      name,
      _t("テーマの設定: トライアウトの回数"),
      _t("評価で全ケースに合格したあと、仕上げの試験（トライアウト）で各ケースを何回ずつ実行するかです。全回合格して初めて「レビュー待ち」になります。AI の答えは毎回少しずつ違うため、何回やっても合格するかを確かめます。\n空欄なら対象インスタンスの [skill_lab] tryout_repeats の回数です。回数を増やすほど確かになりますが、時間がかかります。"),
      () => [labGuide("theme-repeats")],
    ),
    labThemeStep(
      name,
      _t("テーマの設定: 上級者向け設定"),
      _t("このテーマだけ、ループの進め方を変えます。空欄の項目は既定の値（ループの上限は対象インスタンスの [skill_lab]、ほかは担当のワーカーの [skill_lab_worker]）を使うので、ふだんは開かなくて構いません。\nよく変えるのは、ループの最大反復回数（1反復 = 評価1回 + 失敗したときの修正1回）、反復1回の制限時間、評価で同時に流す回の数（推論サーバーの同時処理枠に合わせる）、AI 修正を Agent で行うか、です。各項目にマウスを乗せると、元の設定キーが出ます。"),
      () => [labGuide("theme-advanced")],
    ),
    labThemeStep(
      name,
      _t("テーマの設定を保存する"),
      _t("ここまでのテーマ名・担当・トライアウトの回数・上級者向け設定は、この「保存」で保存します（下の ①〜③ はそれぞれの節で保存します）。値が正しくないときは、ボタンの上に理由が出ます。"),
      () => [labGuide("theme-settings-save")],
    ),
    labThemeStep(
      name,
      _t("① 研究対象"),
      _t("AI に直させるスキル・サブエージェントです（1つ以上が必須）。ドラフトから作ったテーマでは、取り込んだドラフトがもう入っています（「利用者のドラフト … から取り込み」）。\nファイル名を押すと中身を見て編集でき、「ファイルを追加」でスクリプト等を足せます。「外す」は研究対象から外します（テーマの中の写しだけが消え、元のドラフトは残ります）。\nテーマの中で直すのは写しです。元のドラフト・正式のスキルは、昇格するまで変わりません。"),
      () => [labSection("targets")],
    ),
    labThemeStep(
      name,
      _t("① 研究対象を追加する方法"),
      _t("「研究対象を追加」を開くと、3つの方法から選べます。\nA. 利用者のドラフトを取り込む（チャットで作った試作のスキル）\nB. 今あるスキル・サブエージェントを改善する（写しを作って直し、昇格で置き換える）\nC. 新しく作る（作りたい仕事を書いて AI に作ってもらうか、白紙のひな形から自分で書く）\n1つのテーマに、スキルとサブエージェントを組み合わせて入れることもできます。"),
      () => [labGuide("asset-add")],
    ),
    labThemeStep(
      name,
      _t("② やってほしいこと（採点の基準）"),
      _t("このスキルに何をしてほしいかを、ふつうの文章で書きます。AI はここを正解として、スキルを直し、試験の合否を決めます（AI がここを書き換えることはありません）。\n必須は「目的」だけです。「使う場面」「期待する出力」「やってはいけないこと」も書くと、AI が試験にだけ通る直し方をしたときに見分けやすくなります。各欄の薄い文字が入力例です。\n「TODO:」の所は、AI の下書きが資料から読み取れなかった点です。書き直してください。"),
      () => [labSection("spec")],
    ),
    labThemeStep(
      name,
      _t("② を保存する・AI に下書きを頼む"),
      _t("書いたら「保存」を押します。自分で書くのが大変なら「AI に下書きを頼む」で、① の研究対象の中身から AI が ② を書きます（人が書いた所は残し、空いている所を埋めます）。横の欄に「金額の確認を重視して」のような補足を書けます。\nAI への依頼はワーカーが数分かけて行います。終わるまでボタンは「AI が処理中…」になり、状態はページの下の「AI への依頼」で見られます。下書きは必ず読んで、違う所を直してから保存してください。"),
      () => [labGuide("spec-actions")],
    ),
    labThemeStep(
      name,
      _t("③ ケース（試験の問題）"),
      _t("② のとおりに動くかを、実際に試すための問題です（1件以上が必須）。1問ごとに「利用者が打ちそうな指示」と「どうなっていれば合格か」を決めます。\n一覧の「編集」でフォームを開き、「YAML」でケースのファイルを直接編集できます。「1回試す」を押すと、ワーカーがそのケースを1回だけ解いて結果を見せます（スキルは直しません）。ケースを作ったら、ループを始める前に1回試して、問題そのものがおかしくないかを確かめると確実です。"),
      () => [labGuide("cases-table")],
    ),
    labThemeStep(
      name,
      _t("③ ケースを自分で作る"),
      _t("「＋ ケースを自分で作る」でフォームが開きます。必須は次の2つだけで、ほかは空欄のままで構いません。\n・利用者の指示: AI に打つ文章（例: この見積書をチェックして）。--- だけの行をはさむと、2回目以降の発言になります。\n・合格の決め方: 「回答に必ず入っているべき言葉」「入っていてはいけない言葉」「AI が判断する点」「作ったファイルを確かめるスクリプト」のどれか1つ以上。言葉では決めにくい良し悪しは「AI が判断する点」に書きます（AI の判断は、昇格する前に人が確認します）。\nファイルを渡す問題では「使うファイル」でフォルダを選びます（この節の下の「ケースで使うファイル」で先にアップロードします）。「細かい設定」では、このスキルを使ったか・使ってはいけない場面の試験・AI が質問してきたときの返事・制限時間などを決められます。"),
      () => [labGuide("case-create")],
    ),
    labThemeStep(
      name,
      _t("③ ケースを AI に作ってもらう"),
      _t("問の数を入れて「AI に作ってもらう」を押すと、① と ② をもとに AI がケースを作ります。② の「目的」が空だと的外れな問題になりやすいので、先に ② を書いてください。\nできたケースは一覧に追加されます。必ず「編集」で中身を読み、利用者が実際に打ちそうな指示か、合格の決め方が ② と合っているかを確かめてください。"),
      () => [labGuide("case-ai")],
    ),
    labThemeStep(
      name,
      _t("③ ケースで使うファイル"),
      _t("見積書など、ケースで AI に渡すファイルを置きます。フォルダ名（例: sample1）を入れてファイルを選び、「アップロード」を押すと fixtures/<フォルダ名> にまとまります。ケースの「使うファイル」でそのフォルダを選ぶと、試験のたびにその写しが作業フォルダになります（元のファイルは変わりません）。\n作ったファイルを確かめるスクリプト（.py）も、ここに一緒に置きます。"),
      () => [labGuide("fixtures")],
    ),
    labThemeStep(
      name,
      _t("設定パッチ（上級者向け）"),
      _t("評価と昇格のときに、対象インスタンスの設定（スクリプトの実行の許可・計画の承認の免除・メインエージェントに許可するツール）へ足す項目です。スキルのスクリプトの承認の免除と、サブエージェントの「フォームで編集」で選んだ実行の許可は自動で入るため、ふだんは書く必要はありません。"),
      () => [labSection("patch")],
    ),
    labThemeStep(
      name,
      _t("AI への依頼"),
      _t("「AI に下書きを頼む」「AI に作ってもらう」「1回試す」で頼んだ作業の一覧です。待ち・実行中・完了・失敗と、その結果が出ます（「1回試す」は「結果を見る」で答えと合否を見られます）。\n「待ち」のまま進まないときは、担当のスキル調整ワーカーが起動しているかを、インスタンス一覧で確かめてください。"),
      () => [labSection("tasks")],
    ),
    {
      title: _t("スキル調整ループを開始する"),
      body: _t("①〜③ を入れたら、上の「スキル調整ループを開始」を押します。AI が全ケースを解き（評価）、不合格があれば原因を調べてスキルを直し、もう一度解きます。全ケースに合格したらトライアウト（各ケースを決めた回数ずつ）を行い、全回合格で「レビュー待ち」になります。\n「トライアウトだけ実行」は、直さずに仕上げの試験だけを行います。止めたいときは「停止」です。\nスキルを直しても合格しない原因（ケース・② の書き方・環境）を AI が見つけたときは、「人の見直しが必要」で止まり、原因と直し方の案が見出しに出ます。直してから開始し直してください（指摘が誤りなら、そのまま開始し直せます）。"),
      enter: () => labTutorialOpenThemeTab(name, "overview"),
      target: () => [labGuide("theme-actions")],
    },
    {
      title: _t("実行の記録"),
      body: _t("評価・トライアウトの1回ごと（反復）に、ケースごとの合格の数と、AI が何を直したかが出ます。「詳しく」を押すと、AI の分析、各回の最終回答、実行の記録の抜粋、落ちた照合が見られます。\nAI の直し方が的外れなときは、ここで原因を見て、② やケースを直してから開始し直します。「実行の記録をクリア」は記録だけを消します（研究対象・ケースは消えません）。"),
      enter: () => labTutorialOpenThemeTab(name, "runs"),
      target: () => [$(".lab-panel .theme-subtabs .subtab-btn[data-subview=\"runs\"]"), $(".lab-panel .theme-body .data-table")],
    },
    {
      title: _t("レビュー・昇格"),
      body: _t("トライアウトの結果と、取り込んだ時点からの変更（ファイルごとの差分。スクリプトは ★ 付き）を確かめます。AI がケースを変えていたら警告が出ます。\n問題が無ければ、確認事項にチェックを入れて「承認して昇格する」を押すと、このインスタンス専用の正式なスキルになります（ほかのインスタンスへの配布も選べます）。昇格したスキルは、インスタンスを再起動すると使えます。\nドラフトから作ったテーマでは、作成者へ理由を付けて「差し戻す」か「不採用にする」こともできます。研究室での修正をドラフトへ写してから差し戻すこともできます。"),
      enter: () => labTutorialOpenThemeTab(name, "review"),
      target: () => [$(".lab-panel .theme-subtabs .subtab-btn[data-subview=\"review\"]")],
    },
    {
      title: _t("履歴"),
      body: _t("このテーマで行った操作（作成・保存・開始・昇格・差し戻し等）が、日時・実行者と一緒に残ります。\n終わったテーマ（昇格済み・差し戻し済み・取りやめ）も、見出しの「再開して編集」で準備中に戻し、直してもう一度ループを回せます。\nチュートリアルは以上です。「完了」で閉じます。上部の「スキル研究室チュートリアル」から、いつでももう一度始められます。"),
      enter: () => labTutorialOpenThemeTab(name, "history"),
      target: () => [$(".lab-panel .theme-subtabs .subtab-btn[data-subview=\"history\"]")],
    },
  ];
}
