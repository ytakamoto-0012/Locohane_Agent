"use strict";
/* スキル研究室（admin/skill_lab/api.py）。admin.js の後に読み込み、その部品
 * （$ / $$ / api / el / dataTable / fmtTime / fileToBase64 / startMonitorPolling 等）を使う。
 *
 * 研究テーマはインスタンス（組織別・役割別）ごとに分けて扱う。トップの「スキル研究室」タブは
 * 左にインスタンスの一覧、右に選んだインスタンスのパネルを出し、パネルには
 * そのインスタンスのドラフト・テーマだけを出す（他のインスタンスのものは出さない）。
 */

const LAB_STATUS_LABELS = {
  draft: _t("準備中"),
  queued: _t("開始待ち"),
  running: _t("実行中"),
  review: _t("レビュー待ち"),
  exhausted: _t("上限到達"),
  error: _t("エラー"),
  stopped: _t("停止"),
  promoted: _t("昇格済み"),
  returned: _t("差し戻し済み"),
  cancelled: _t("取りやめ"),
};
const LAB_ACTIVE = ["queued", "running"];
const LAB_CLOSED = ["promoted", "returned", "cancelled"];
const TASK_LABELS = { trial: _t("1回試行"), draft_spec: _t("仕様の下書き"), draft_cases: _t("ケースの下書き"), draft_asset: _t("スキル・サブエージェントの下書き") };
const TASK_STATUS_LABELS = { queued: _t("待ち"), running: _t("実行中"), done: _t("完了"), error: _t("失敗") };

let labSelectedInstance = null;

function labPath(name, suffix) {
  return `/api/instances/${encodeURIComponent(name)}/skill-lab${suffix || ""}`;
}

function themePath(name, id, suffix) {
  return labPath(name, `/themes/${encodeURIComponent(id)}${suffix || ""}`);
}

function statusBadge(status) {
  return el("span", { class: `lab-status lab-status-${status}`, text: LAB_STATUS_LABELS[status] || status });
}

function labModal(title, bodyNodes, opts) {
  opts = opts || {};
  const close = el("button", { type: "button", class: "btn-cancel", text: opts.closeText || _t("閉じる") });
  const errorBox = el("div", { class: "error" });
  const actions = el("div", { class: "modal-actions" }, [close, ...(opts.actions || [])]);
  const modal = el("div", { class: "modal-backdrop" }, el("div", { class: `modal ${opts.wide ? "modal-wide" : ""}` }, [
    el("h3", { text: title }),
    ...bodyNodes,
    errorBox,
    actions,
  ]));
  close.addEventListener("click", () => modal.remove());
  document.body.appendChild(modal);
  return { modal, errorBox, close: () => modal.remove() };
}

async function labAction(fn, errorTarget) {
  try {
    return await fn();
  } catch (e) {
    if (errorTarget) errorTarget.textContent = e.message;
    else alert(e.message);
    return undefined;
  }
}

/* ------------------------------------------------------------- トップタブ */

async function renderSkillLab() {
  mainEl.innerHTML = "";
  const sidebar = el("div", { class: "lab-sidebar" });
  const panel = el("div", { class: "lab-panel" });
  mainEl.append(
    el("div", { class: "view-header" }, [el("h2", { text: _t("スキル研究室") })]),
    el("p", {
      class: "hint",
      text:
        _t("スキル・サブエージェントを研究テーマとして作り、AI に不具合を自己修正させて本番運用に耐えるものに仕上げます。") +
        _t("テーマはインスタンスごとに分かれ、昇格先もそのインスタンス専用の置き場です（他のインスタンスには入りません）。"),
    }),
    el("div", { class: "lab-layout" }, [sidebar, panel]),
  );
  const data = await api("/api/skill-lab/overview");
  const labsBox = el("div", { class: "lab-labs" }, [el("h4", { text: _t("スキル調整ワーカー（実行役）") })]);
  if (!data.labs.length) {
    labsBox.append(
      el("p", {
        class: "hint",
        text: _t("まだありません。AI による評価・修正・下書きを動かすには、インスタンス一覧の「＋ インスタンス追加」で種類「スキル調整ワーカー」を作り、[llm] main_url を設定して起動してください。"),
      }),
    );
  }
  for (const lab of data.labs) {
    labsBox.append(el("div", { class: "lab-lab-row" }, [el("span", { class: `state-dot ${lab.state}` }), _t(` {0}（{1}）`, lab.display_name, STATE_LABELS[lab.state] || lab.state)]));
  }
  const list = el("div", { class: "lab-instance-list" });
  for (const row of data.instances) {
    const c = row.counts || {};
    const item = el("button", { class: "lab-instance-item", type: "button" }, [
      el("div", { class: "lab-instance-name", text: row.display_name }),
      el("div", { class: "hint", text: row.error ? _t(`設定エラー: {0}`, row.error) : _t(`ドラフト {0} / 進行中 {1} / 実行中 {2} / レビュー待ち {3}`, c.drafts || 0, c.open || 0, c.active || 0, c.review || 0) }),
    ]);
    item.dataset.instance = row.name;
    item.addEventListener("click", () => {
      labSelectedInstance = row.name;
      $$(".lab-instance-item", list).forEach((b) => b.classList.toggle("active", b === item));
      renderSkillLabPanel(panel, row.name);
    });
    list.append(item);
  }
  sidebar.append(el("h4", { text: _t("対象インスタンス") }), list, labsBox);
  const initial = data.instances.find((r) => r.name === labSelectedInstance) || data.instances[0];
  if (initial) $(`.lab-instance-item[data-instance="${CSS.escape(initial.name)}"]`, list).click();
}

/* ---------------------------------------------------- インスタンスのパネル */

async function renderSkillLabPanel(container, name) {
  stopMonitorPolling();
  container.innerHTML = "";
  const [themesData, src] = await Promise.all([api(labPath(name, "/themes")), api(labPath(name, "/sources"))]);
  const meta = (typeof instanceCache !== "undefined" ? instanceCache : []).find((i) => i.name === name);
  container.append(labPanelHeader(name, meta ? meta.display_name : name, themesData.labs));

  const createBtn = el("button", { class: "primary", text: _t("＋ テーマを作成"), onclick: () => openCreateThemeModal(container, name, themesData.labs) });
  container.append(el("div", { class: "lab-section-head" }, [el("h3", { text: _t("研究テーマ") }), createBtn]));
  const rows = themesData.themes.map((t) => [
    linkBtn(t.title, () => renderThemeDetail(container, name, t.id)),
    statusBadge(t.status),
    _t(`スキル {0} / エージェント {1}`, t.assets.skills.length, t.assets.agents.length),
    _t(`{0} 件`, t.cases),
    t.last ? _t(`{0} {1}（反復 {2}）`, t.last.phase === "tryout" ? _t("トライアウト") : _t("評価"), t.last.verdict === "pass" ? _t("合格") : _t("不合格"), t.iterations) : "-",
    fmtTime(t.updated_at),
  ]);
  container.append(dataTable([_t("テーマ"), _t("状態"), _t("研究対象"), _t("ケース"), _t("最新の結果"), _t("更新")], rows, _t("まだテーマはありません。")));

  container.append(el("h3", { text: _t("利用者のドラフト（このインスタンス）") }));
  const draftRows = src.drafts.map((d) => [
    `${d.owner}/${d.name}`,
    d.status === "returned" ? _t(`差し戻し中（{0}）`, d.returned_reason || "") : d.status,
    fmtTime(d.updated_at),
    d.imported_theme
      ? el("span", { class: "hint", text: _t("テーマに取り込み済み") })
      : el("button", {
          text: _t("テーマを作って取り込む"),
          onclick: async () => {
            const theme = await labAction(() => api(labPath(name, "/themes"), { method: "POST", body: { title: `${d.owner}/${d.name}`, lab: defaultLab(themesData.labs) } }));
            if (!theme) return;
            const ok = await labAction(() =>
              api(themePath(name, theme.id, "/assets"), { method: "POST", body: { mode: "draft", asset_type: "skills", name: d.name, origin: d.path } }),
            );
            if (ok) renderThemeDetail(container, name, theme.id);
          },
        }),
  ]);
  container.append(dataTable([_t("ドラフト"), _t("状態"), _t("更新"), ""], draftRows, _t("このインスタンスにドラフトはありません。")));
}

function defaultLab(labs) {
  return labs.length === 1 ? labs[0].name : null;
}

function labPanelHeader(name, displayName, labs) {
  const labText = labs.length
    ? labs.map((l) => _t(`{0}（{1}）`, l.display_name, STATE_LABELS[l.state] || l.state)).join(_t("、"))
    : _t("スキル調整ワーカーがありません（AI による修正・試行は動きません）");
  return el("div", { class: "lab-panel-header" }, [
    el("div", {}, [el("span", { class: "hint", text: _t("対象インスタンス") }), el("h3", { text: _t(`{0}（{1}）`, displayName, name) })]),
    el("div", { class: "hint", text: _t(`スキル調整ワーカー: {0}`, labText) }),
  ]);
}

function labSelect(labs, current) {
  const select = el("select", { name: "lab" }, [el("option", { value: "", text: labs.length === 1 ? _t(`（自動: {0}）`, labs[0].display_name) : _t("（未選択）") })]);
  for (const l of labs) {
    const opt = el("option", { value: l.name, text: l.display_name });
    if (l.name === current) opt.selected = true;
    select.append(opt);
  }
  return select;
}

function openCreateThemeModal(container, name, labs) {
  const title = el("input", { type: "text", placeholder: _t("例: 見積書チェック") });
  const lab = labSelect(labs, defaultLab(labs));
  const ok = el("button", { class: "primary", type: "button", text: _t("作成") });
  const m = labModal(_t("研究テーマを作成"), [el("label", {}, [_t("テーマ名"), title]), el("label", {}, [_t("担当の スキル調整ワーカー"), lab])], { actions: [ok] });
  ok.addEventListener("click", async () => {
    const theme = await labAction(() => api(labPath(name, "/themes"), { method: "POST", body: { title: title.value, lab: lab.value || null } }), m.errorBox);
    if (!theme) return;
    m.close();
    renderThemeDetail(container, name, theme.id);
  });
}

/* ------------------------------------------------------------ テーマ詳細 */

const THEME_SUBVIEWS = [
  ["overview", _t("研究内容")],
  ["runs", _t("実行の記録")],
  ["review", _t("レビュー・昇格")],
  ["history", _t("履歴")],
];

async function renderThemeDetail(container, name, id, sub) {
  stopMonitorPolling();
  container.innerHTML = "";
  const state = { name, id, sub: sub || "overview", data: null, sources: null };
  const header = el("div", { class: "theme-header" });
  const nav = el("nav", { class: "subtabs" });
  const body = el("div", { class: "theme-body" });
  const back = el("button", { class: "back-btn", text: _t("← テーマ一覧"), onclick: () => renderSkillLabPanel(container, name) });
  container.append(back, header, nav, body);
  for (const [key, label] of THEME_SUBVIEWS) {
    const b = el("button", { class: "subtab-btn", text: label });
    b.dataset.subview = key;
    b.addEventListener("click", () => {
      state.sub = key;
      $$(".subtab-btn", nav).forEach((x) => x.classList.toggle("active", x === b));
      renderThemeSub(state, body);
    });
    nav.append(b);
  }
  state.reload = async () => {
    state.data = await api(themePath(name, id));
    renderThemeHeader(state, header, container);
  };
  state.sources = await api(labPath(name, "/sources"));
  await state.reload();
  $(`.subtab-btn[data-subview="${state.sub}"]`, nav).click();
  // 実行中・タスクがある間は状態を追う（編集中の欄を消さないよう、見出しと「実行の記録」だけを更新する）。
  startMonitorPolling(body, async () => {
    const before = state.data.updated_at;
    const busy = LAB_ACTIVE.includes(state.data.status) || (state.data.tasks || []).some((t) => t.status === "queued" || t.status === "running");
    if (!busy) return;
    await state.reload();
    if (state.data.updated_at === before) return;
    if (state.sub === "runs") renderThemeSub(state, body);
    else if (state.sub === "overview") refreshThemeSections(state, true);
  });
}

function renderThemeHeader(state, header, container) {
  const d = state.data;
  const editable = !LAB_ACTIVE.includes(d.status) && !LAB_CLOSED.includes(d.status);
  const buttons = [];
  const missing = d.start_missing || [];
  const startDisabled = missing.length ? "disabled" : null;
  if (editable) {
    buttons.push(
      el("button", { class: "primary", text: _t("スキル調整ループを開始"), disabled: startDisabled, onclick: () => startTheme(state, "loop") }),
      el("button", { text: _t("トライアウトだけ実行"), disabled: startDisabled, onclick: () => startTheme(state, "tryout") }),
      el("button", { text: _t("担当・名前を変更"), onclick: () => openThemeSettingsModal(state) }),
      el("button", {
        text: _t("取りやめ"),
        onclick: async () => {
          if (!confirm(_t("このテーマを取りやめます（ファイルは残ります）。よろしいですか？"))) return;
          if (await labAction(() => api(themePath(state.name, state.id, "/cancel"), { method: "POST" }))) renderSkillLabPanel(container, state.name);
        },
      }),
    );
  }
  if (LAB_ACTIVE.includes(d.status)) {
    buttons.push(el("button", { text: _t("停止"), onclick: async () => { await labAction(() => api(themePath(state.name, state.id, "/stop"), { method: "POST" })); await state.reload(); } }));
  }
  const loop = d.loop || {};
  const labName = d.lab || (d.labs.length === 1 ? d.labs[0].name : null);
  const lab = d.labs.find((l) => l.name === labName);
  const notes = [];
  if (LAB_ACTIVE.includes(d.status) && lab && !["running", "external"].includes(lab.state)) {
    notes.push(el("p", { class: "warn-box", text: _t(`担当の スキル調整ワーカー {0} が起動していません。インスタンス一覧から起動してください。`, lab.display_name) }));
  }
  if (LAB_ACTIVE.includes(d.status) && !lab) {
    notes.push(el("p", { class: "warn-box", text: _t("担当の スキル調整ワーカーが見つからないため、このままでは始まりません。停止してから「担当・名前を変更」で選び直してください。") }));
  }
  if (editable && !lab) {
    notes.push(
      el("p", {
        class: "warn-box",
        text: d.lab
          ? _t(`担当の スキル調整ワーカー {0} が見つかりません。「担当・名前を変更」で選び直してください。`, d.lab)
          : d.labs.length
            ? _t("担当の スキル調整ワーカーが決まっていません。「担当・名前を変更」で選んでください。")
            : _t("スキル調整ワーカーがありません。インスタンス一覧で種類「スキル調整ワーカー」を作成してください。"),
      }),
    );
  }
  if (loop.message) notes.push(el("p", { class: "hint", text: loop.message }));
  if (editable && missing.length) notes.push(el("p", { class: "warn-box", text: _t(`開始に必要なもの（研究内容のページで入れてください）: {0}`, missing.join(_t("、"))) }));
  header.replaceChildren(
    el("div", { class: "theme-title-row" }, [el("h3", { text: d.title }), statusBadge(d.status)]),
    el("div", { class: "hint", text: _t(`対象インスタンス: {0} ／ 担当の スキル調整ワーカー: {1} ／ 反復 {2} 回 ／ 作成 {3} {4}`, d.instance, lab ? lab.display_name : _t("未選択"), (d.iterations || []).length, d.created_by || "", fmtTime(d.created_at)) }),
    ...notes,
    el("div", { class: "theme-actions" }, buttons),
  );
}

function openThemeSettingsModal(state) {
  const d = state.data;
  const title = el("input", { type: "text" });
  title.value = d.title;
  const lab = labSelect(d.labs, d.lab);
  const ok = el("button", { class: "primary", type: "button", text: _t("保存") });
  const m = labModal(_t("担当・名前を変更"), [el("label", {}, [_t("テーマ名"), title]), el("label", {}, [_t("担当の スキル調整ワーカー"), lab])], { actions: [ok] });
  ok.addEventListener("click", async () => {
    const res = await labAction(() => api(themePath(state.name, state.id), { method: "PUT", body: { title: title.value, lab: lab.value } }), m.errorBox);
    if (!res) return;
    m.close();
    await state.reload();
  });
}

async function startTheme(state, mode) {
  const label = mode === "loop" ? _t("スキル調整ループ（評価 → 失敗なら AI が直す → 再評価 → トライアウト）") : _t("トライアウト");
  if (!confirm(_t(`{0}を開始します。よろしいですか？`, label))) return;
  const res = await labAction(() => api(themePath(state.name, state.id, "/start"), { method: "POST", body: { mode } }));
  if (res && !res.lab_running) alert(_t("開始待ちにしました。担当の スキル調整ワーカーが起動していないため、起動すると始まります。"));
  await state.reload();
}

function renderThemeSub(state, body) {
  body.innerHTML = "";
  state.sections = null;
  const fn = { overview: renderThemeOverview, runs: renderThemeRuns, review: renderThemeReview, history: renderThemeHistory }[state.sub];
  return fn(state, body);
}

/* 「研究内容」は節ごとに描き直す（1か所を変えるたびに全体を描き直すと、編集中の仕様が消えるため）。
 * 各節の描画関数は、描き直してはいけない状態かを返す関数 dirty と、利用者の操作による描き直しでも
 * 守るか（protect）を返してよい。 */
const OVERVIEW_SECTIONS = [
  ["targets", renderThemeTargets],
  ["spec", renderThemeSpec],
  ["cases", renderThemeCases],
  ["patch", renderThemePatch],
  ["tasks", renderThemeTasks],
];

async function renderThemeOverview(state, body) {
  body.append(
    el("p", {
      class: "theme-intro",
      text:
        _t("上から順に埋めます。① で直す（作る）スキルを選び、② で採点の基準を書き、③ で試験問題を作ります。") +
        _t("「スキル調整ループを開始」を押すと、AI が ③ の試験を解き、② に照らして不合格ならスキルを直して解き直します。"),
    }),
  );
  const sections = {};
  for (const [key, fn] of OVERVIEW_SECTIONS) {
    sections[key] = { box: el("section", { class: `theme-section theme-section-${key}` }), fn, guard: null };
    body.append(sections[key].box);
  }
  state.sections = sections;
  await Promise.all(Object.values(sections).map(async (s) => (s.guard = (await s.fn(state, s.box)) || null)));
}

async function refreshThemeSections(state, fromPolling) {
  if (!state.sections) return renderThemeSub(state, $(".theme-body"));
  for (const s of Object.values(state.sections)) {
    if (s.guard && (fromPolling || s.guard.protect) && s.guard.dirty()) continue;
    s.box.innerHTML = "";
    s.guard = (await s.fn(state, s.box)) || null;
  }
}

/* 節の入力欄が描いた時点から変わったか（自動更新で入力途中の欄を消さないため）。 */
function inputsChanged(box) {
  const values = () => JSON.stringify([...box.querySelectorAll("input, textarea, select")].map((x) => (x.type === "file" ? x.files.length : x.type === "checkbox" ? x.checked : x.value)));
  const initial = values();
  return { dirty: () => values() !== initial };
}

function themeEditable(state) {
  return !LAB_ACTIVE.includes(state.data.status) && !LAB_CLOSED.includes(state.data.status);
}

/* ② は ① を、③ は ① と ② をもとに AI が書くため、もとが足りなければ先に確かめる。 */
async function draftWithCheck(state, type, params, errorBox) {
  const d = state.data;
  const hasTargets = d.summary.assets.skills.length + d.summary.assets.agents.length > 0
    || (d.tasks || []).some((t) => t.type === "draft_asset" && (t.status === "queued" || t.status === "running"));
  const specMissing = (d.start_missing || []).some((m) => m.startsWith("②"));
  const lacks = [];
  if (!hasTargets) lacks.push(_t("① 研究対象がまだありません"));
  if (type === "draft_cases" && specMissing) lacks.push(_t("② やってほしいことの「目的」が未記入です"));
  if (lacks.length) {
    const what = type === "draft_spec" ? _t("② は補足欄の内容だけから書くことになります") : _t("③ は的外れな問題になりやすくなります");
    if (!confirm(_t(`{0}。このまま頼むと、{1}。続けますか？`, lacks.join(_t("。")), what))) return undefined;
  }
  return addTask(state, type, params, errorBox);
}

async function addTask(state, type, params, errorBox) {
  const res = await labAction(() => api(themePath(state.name, state.id, "/tasks"), { method: "POST", body: { type, params } }), errorBox);
  if (res && !res.lab_running) alert(_t("依頼しました。担当の スキル調整ワーカーが起動していないため、起動すると処理されます。"));
  await state.reload();
  return res;
}

/* --- 研究内容: 仕様・設定パッチ・AI への依頼 --- */

/* 人が書いて保存するファイルの編集欄。未保存の変更がある間は描き直さない。 */
async function themeFileEditor(state, box, path, rows, extraActions) {
  const editable = themeEditable(state);
  const file = await api(themePath(state.name, state.id, `/files?path=${encodeURIComponent(path)}`));
  let saved = file.content;
  const area = el("textarea", { rows, class: "code-area" });
  area.value = saved;
  const err = el("div", { class: "error" });
  const save = el("button", {
    class: "primary",
    text: _t("保存"),
    disabled: editable ? null : "disabled",
    onclick: async () => {
      const value = area.value;
      if (await saveThemeFile(state, path, value, err)) saved = value;
    },
  });
  box.append(area, el("div", { class: "inline-form" }, [save, ...(extraActions ? extraActions(err) : [])]), err);
  return { dirty: () => area.value !== saved, protect: true };
}

/* ② やってほしいこと: spec.md を見出しごとの欄で編集する（必須は Purpose だけ）。
 * 見出しは英語圏でも打ちやすい半角の英語（admin/skill_lab/themes.py の SPEC_HEADINGS と同じ）。
 * 日本語の旧見出しで書かれた spec.md も読み、保存すると英語の見出しに揃う。
 * 決まった見出し以外を含む spec.md は欄に振り分けられないため、そのまま編集する。
 * 要素: [見出し, 旧見出し, 欄の名前, 必須, 行数, 入力例] */
const SPEC_FIELDS = [
  ["Purpose", "目的", _t("目的"), true, 3, _t("何のためのスキルか。例: 見積書の金額の計算ミスと記入漏れを、客先に出す前に見つける。")],
  ["When to use", "使う場面", _t("使う場面"), false, 2, _t("どんな業務・状況で使うか。例: 営業担当が見積書を作り終えて、提出前に確認するとき。")],
  ["Expected output", "期待する出力", _t("期待する出力"), false, 3, _t("例: 問題のある箇所を一覧にする。各行に場所・内容・正しい値。問題が無ければ「問題なし」とだけ答える。")],
  ["Must not do", "やってはいけないこと", _t("やってはいけないこと"), false, 2, _t("例: 値引きの提案など、頼まれていないことをしない。見積書のファイルを書き換えない。")],
];
// AI の下書きが資料から読み取れなかった点の印（旧形式の「要確認:」も含む）。
const SPEC_UNSURE = /^(TODO\b|要確認)/im;

function parseSpec(text) {
  const values = Object.fromEntries(SPEC_FIELDS.map(([heading]) => [heading, []]));
  let title = null;
  let cur = null;
  for (const line of text.split(/\r?\n/)) {
    const h2 = /^##\s+(.*)$/.exec(line);
    const h1 = /^#\s+(.*)$/.exec(line);
    if (h2) {
      const text = h2[1].trim();
      const field = SPEC_FIELDS.find(([heading, legacy]) => text.toLowerCase().startsWith(heading.toLowerCase()) || text.startsWith(legacy));
      if (!field) return null;
      cur = field[0];
    } else if (h1 && title === null && cur === null) {
      title = h1[1].trim();
    } else if (/^#/.test(line)) {
      return null;
    } else if (cur) {
      values[cur].push(line);
    } else if (line.trim()) {
      return null;
    }
  }
  return { title, values: Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v.join("\n").trim()])) };
}

function composeSpec(title, values) {
  return `# ${title}\n\n` + SPEC_FIELDS.map(([heading]) => `## ${heading}\n${values[heading] ? `${values[heading]}\n` : ""}`).join("\n");
}

async function renderThemeSpec(state, box) {
  const editable = themeEditable(state);
  const file = await api(themePath(state.name, state.id, `/files?path=${encodeURIComponent("spec.md")}`));
  const parsed = parseSpec(file.content);
  const err = el("div", { class: "error" });
  box.append(
    el("h4", {}, [requiredLabel(_t("② やってほしいこと（採点の基準）"), true)]),
    el("p", {
      class: "hint",
      text:
        _t("このスキルに何をしてほしいかを、ふつうの文章で書きます。AI はここを正解として、スキルを直し、試験の合否を決めます（AI がここを書き換えることはありません）。") +
        _t("③ の試験問題は例にすぎないため、ここが無いと、AI が試験にだけ通る直し方をしても見分けられません。"),
    }),
  );
  let read;
  if (parsed) {
    const areas = SPEC_FIELDS.map(([heading, , label, required, rows, placeholder]) => {
      const area = el("textarea", { rows, placeholder });
      area.value = parsed.values[heading];
      box.append(el("label", {}, [requiredLabel(label, required), area]));
      return [heading, area];
    });
    const title = parsed.title || state.data.title;
    read = () => composeSpec(title, Object.fromEntries(areas.map(([heading, area]) => [heading, area.value.trim()])));
    if (areas.some(([, area]) => SPEC_UNSURE.test(area.value))) {
      box.append(el("p", { class: "warn-box", text: _t("「TODO:」の所は、AI が資料から読み取れなかった点です。書き直してください。") }));
    }
  } else {
    const area = el("textarea", { rows: 14, class: "code-area" });
    area.value = file.content;
    box.append(el("p", { class: "hint", text: _t("決まった見出し（Purpose・When to use・Expected output・Must not do）以外を含むため、そのまま編集します。") }), area);
    read = () => area.value;
  }
  let saved = read();
  const request = el("input", { type: "text", placeholder: _t("AI への補足（任意）") });
  box.append(
    el("div", { class: "inline-form" }, [
      el("button", {
        class: "primary",
        text: _t("保存"),
        disabled: editable ? null : "disabled",
        onclick: async () => {
          const value = read();
          if (await saveThemeFile(state, "spec.md", value, err)) saved = value;
        },
      }),
      request,
      el("button", {
        text: _t("AI に下書きを頼む"),
        title: _t("① の研究対象の中身を読んで、AI が ② を書きます（人が書いた所は残し、空いている所を埋めます）"),
        disabled: editable ? null : "disabled",
        onclick: () => draftWithCheck(state, "draft_spec", { request: request.value }, err),
      }),
    ]),
    err,
  );
  return { dirty: () => read() !== saved, protect: true };
}

async function renderThemePatch(state, box) {
  box.append(
    el("details", { class: "theme-advanced" }, [
      el("summary", { text: _t("設定パッチ（config_patch.json、上級者向け）") }),
      el("p", {
        class: "hint",
        text:
          _t("評価と昇格で対象インスタンスの設定へ足す項目です（足せるのは [subagent] agent_type_run_script_allowlist・[plan] plan_approval_exempt_scripts・[main_agent_tool_guard] allow_entries）。") +
          _t("スキルのスクリプトの計画承認の免除と、サブエージェントの「フォームで編集」で選んだ run_script の許可は自動で入るため、通常は書く必要はありません。"),
      }),
    ]),
  );
  const guard = await themeFileEditor(state, box.firstChild, "config_patch.json", 8);
  const details = box.firstChild;
  if (details.querySelector("textarea").value.trim() && details.querySelector("textarea").value.trim() !== "{}") details.open = true;
  return guard;
}

function renderThemeTasks(state, box) {
  const tasks = (state.data.tasks || []).slice().reverse();
  box.append(el("h4", { text: _t("AI への依頼") }));
  box.append(
    dataTable(
      [_t("依頼"), _t("状態"), _t("日時"), _t("結果")],
      tasks.map((t) => [
        TASK_LABELS[t.type] || t.type,
        TASK_STATUS_LABELS[t.status] || t.status,
        fmtTime(t.created_at),
        t.error
          ? el("span", { class: "error", text: t.error })
          : t.type === "trial" && t.status === "done"
            ? linkBtn(_t("結果を見る"), () => showTrialResult(t.result))
            : (t.result && t.result.message) || "",
      ]),
      _t("まだありません。"),
    ),
  );
}

async function saveThemeFile(state, path, content, errorBox) {
  errorBox.textContent = "";
  const ok = await labAction(() => api(themePath(state.name, state.id, "/files"), { method: "PUT", body: { path, content } }), errorBox);
  if (ok) {
    errorBox.textContent = "";
    await state.reload();
    flashSaved(errorBox);
  }
  return ok;
}

function flashSaved(target) {
  target.classList.add("ok-text");
  target.textContent = _t("保存しました（トライアウトの結果は無効になりました）。");
  setTimeout(() => {
    target.classList.remove("ok-text");
    if (target.textContent.startsWith(_t("保存しました"))) target.textContent = "";
  }, 4000);
}

/* --- 研究内容: 研究対象（スキル・サブエージェント） --- */

function renderThemeTargets(state, body) {
  const d = state.data;
  const editable = themeEditable(state);
  const files = d.files || [];
  body.append(
    el("h4", {}, [requiredLabel(_t("① 研究対象（スキルかサブエージェントを1つ以上）"), true)]),
    el("p", { class: "hint", text: _t("下の ② のとおりに動くよう、AI に直させるスキル・サブエージェントです。") }),
  );
  for (const [type, label] of [["skills", _t("スキル")], ["agents", _t("サブエージェント")]]) {
    body.append(el("h5", { text: label }));
    const names = d.summary.assets[type];
    if (!names.length) body.append(el("p", { class: "hint", text: _t("まだありません。") }));
    for (const n of names) {
      const src = (d.sources[type] || {})[n] || {};
      const srcText =
        src.kind === "draft" ? _t(`利用者のドラフト {0}/{1} から取り込み`, src.owner, n)
        : src.kind === "official" ? _t(`正式の{0}{1}を複製して改善`, src.origin_label === "builtin" ? _t("同梱") : src.origin_label === "instance" ? _t("このインスタンス専用") : _t("共有"), type === "skills" ? _t("スキル") : _t("サブエージェント"))
        : _t("新規");
      const prefix = type === "skills" ? `assets/skills/${n}/` : `assets/agents/${n}.md`;
      const fileLinks = files
        .filter((f) => (type === "skills" ? f.path.startsWith(prefix) : f.path === prefix))
        .map((f) => el("li", {}, [linkBtn(f.path.replace(/^assets\/(skills|agents)\//, ""), () => openFileEditor(state, f.path, f.text)), ` (${f.size} B)`]));
      const actions = [];
      if (type === "agents") actions.push(el("button", { text: _t("フォームで編集"), disabled: editable ? null : "disabled", onclick: () => openAgentEditor(state, n) }));
      if (type === "skills") actions.push(el("button", { text: _t("ファイルを追加"), disabled: editable ? null : "disabled", onclick: () => openNewFileModal(state, n) }));
      actions.push(
        el("button", {
          class: "btn-danger",
          text: _t("外す"),
          disabled: editable ? null : "disabled",
          onclick: async () => {
            if (!confirm(_t(`{0} を研究対象から外します（テーマ内のファイルは削除されます。元のドラフト・正式のものは残ります）。`, n))) return;
            if (await labAction(() => api(themePath(state.name, state.id, `/assets/${type}/${encodeURIComponent(n)}`), { method: "DELETE" }))) {
              await state.reload();
              refreshThemeSections(state);
            }
          },
        }),
      );
      body.append(el("div", { class: "asset-card" }, [el("div", { class: "asset-title" }, [el("strong", { text: n }), el("span", { class: "hint", text: ` ${srcText}` })]), el("ul", { class: "file-list" }, fileLinks), el("div", { class: "inline-form" }, actions)]));
    }
  }
  if (editable) body.append(targetAddForms(state));
  return inputsChanged(body);
}

function targetAddForms(state) {
  const src = state.sources;
  const err = el("div", { class: "error" });
  const reload = async () => {
    await state.reload();
    refreshThemeSections(state);
  };
  const post = (payload) => labAction(() => api(themePath(state.name, state.id, "/assets"), { method: "POST", body: payload }), err);

  const draftSel = el("select", {}, [el("option", { value: "", text: _t("（ドラフトを選ぶ）") }), ...src.drafts.map((d) => el("option", { value: d.path, text: `${d.owner}/${d.name}${d.imported_theme ? _t("（他のテーマに取り込み済み）") : ""}` }))]);
  const officialSel = el("select", {}, [
    el("option", { value: "", text: _t("（正式なスキル・サブエージェントを選ぶ）") }),
    ...src.skills.map((s) => el("option", { value: `skills:${s.name}`, text: _t(`スキル {0}（{1}）`, s.name, originText(s.origin)) })),
    ...src.agents.map((a) => el("option", { value: `agents:${a.name}`, text: _t(`サブエージェント {0}（{1}）`, a.name, originText(a.origin)) })),
  ]);
  const newType = el("select", {}, [
    el("option", { value: "skills", text: _t("スキル（AI に覚えさせる仕事の手順）") }),
    el("option", { value: "agents", text: _t("サブエージェント（仕事をまるごと任せる別の AI 担当者）") }),
  ]);
  const newName = el("input", { type: "text", placeholder: _t("例: estimate-check（小文字の英数字・-・_）") });
  const newRequest = el("textarea", { rows: 3, placeholder: _t("例: 見積書の PDF を読んで、金額の計算ミスと記入漏れを一覧にする") });
  const way = (hint, nodes) => el("div", { class: "add-way" }, [hint ? el("p", { class: "hint", text: hint }) : null, ...nodes]);
  const panels = {
    draft: way(src.drafts.length ? _t("利用者がチャットで作った試作のスキルを、ここで仕上げます。") : _t("このインスタンスには、利用者が作ったドラフトがありません。"), src.drafts.length ? [el("div", { class: "inline-form" }, [
      draftSel,
      el("button", {
        text: _t("ドラフトを取り込む"),
        onclick: async () => {
          const d = src.drafts.find((x) => x.path === draftSel.value);
          if (d && (await post({ mode: "draft", asset_type: "skills", name: d.name, origin: d.path }))) reload();
        },
      }),
    ])] : []),
    official: way(_t("写しを作って直します。元のものは、昇格するまで変わりません。"), [el("div", { class: "inline-form" }, [
      officialSel,
      el("button", {
        text: _t("複製して改善する"),
        onclick: async () => {
          if (!officialSel.value) return;
          const [type, n] = officialSel.value.split(":");
          if (await post({ mode: "official", asset_type: type, name: n })) reload();
        },
      }),
    ]),
    el("p", { class: "hint", text: _t("昇格しても、置き換わるのはこのインスタンスの中だけです（ほかのインスタンスは変わりません）。") })]),
    new: way(null, [
      el("label", {}, [_t("種類"), newType]),
      el("label", {}, [_t("名前"), newName]),
      el("label", {}, [_t("どんな仕事をさせたいか（AI に作ってもらうときの注文。下の ② やってほしいこと が書いてあれば、それも一緒に AI へ渡します）"), newRequest]),
      el("div", { class: "inline-form" }, [
      el("button", {
        class: "primary",
        text: _t("AI に作ってもらう"),
        onclick: async () => {
          const res = await post({ mode: "ai", asset_type: newType.value, name: newName.value.trim(), request: newRequest.value });
          if (res) {
            alert(_t("依頼しました。スキル調整ワーカーが下書きを作ると、研究対象に追加されます（このページ下の「AI への依頼」で状態を確認できます）。"));
            reload();
          }
        },
      }),
      el("button", {
        text: _t("白紙のひな形から自分で書く"),
        onclick: async () => {
          const n = newName.value.trim();
          const content = newType.value === "skills" ? skillTemplate(n, newRequest.value) : agentTemplate(n, newRequest.value);
          if (await post({ mode: "new", asset_type: newType.value, name: n, content })) reload();
        },
      }),
      ]),
      el("p", { class: "hint", text: _t("自分で書く場合、注文の文章はスキルの説明文（いつ使うかを AI が判断する手がかり）として入ります。") }),
    ]),
  };

  // 方法を先に選ばせ、選んだ方法の欄だけを出す（選択は描き直しても state.addWay に残す）。
  const ways = [
    ["draft", _t(`A. 利用者のドラフトを取り込む（{0}件）`, src.drafts.length)],
    ["official", _t("B. 今あるスキル・サブエージェントを改善する")],
    ["new", _t("C. 新しく作る")],
  ];
  const placeholder = el("p", { class: "hint", text: _t("追加の方法を上のボタンから選んでください。") });
  const buttons = ways.map(([key, label]) => el("button", { type: "button", class: "seg-btn", text: label, onclick: () => select(key) }));
  const select = (key) => {
    state.addWay = key;
    ways.forEach(([k], i) => {
      buttons[i].classList.toggle("active", k === key);
      panels[k].hidden = k !== key;
    });
    placeholder.hidden = Boolean(key);
    err.textContent = "";
  };
  select(state.addWay || null);
  return el("details", { class: "asset-add", open: state.data.summary.assets.skills.length + state.data.summary.assets.agents.length ? null : "open" }, [
    el("summary", { text: _t("研究対象を追加") }),
    el("div", { class: "seg-group" }, buttons),
    placeholder,
    ...ways.map(([k]) => panels[k]),
    err,
  ]);
}

function originText(origin) {
  return { builtin: _t("同梱"), instance: _t("このインスタンス専用"), shared: _t("共有") }[origin] || origin || "";
}

// 白紙のひな形。LLM が読むファイルのため、表示言語によらず英語で書く（同梱の skills/・agents/ と同じ）。
function skillTemplate(name, desc) {
  return `---\nname: ${name}\ndescription: ${(desc || "What it does, and for which instructions to use it.").replace(/\n/g, " ")}\n---\n\n# ${name}\n\n## Procedure\n\n1. \n`;
}

function agentTemplate(name, desc) {
  return (
    `---\nname: ${name}\ndescription: ${(desc || "What work to delegate to this sub-agent.").replace(/\n/g, " ")}\ntools: read_skill, read_skill_file, run_script, Read, Grep\n---\n\n` +
    `You are a sub-agent that has been delegated a task by the main assistant.\n\n## Procedure\n\n1. \n\n## Final answer\n\n\n---\n\n## Skills\n\n{{skills}}\n\n### Skills/scripts callable with run_script (limited to this agent type)\n\n{{run_script_allowlist}}\n`
  );
}

async function openFileEditor(state, path, isText) {
  const editable = themeEditable(state);
  if (!isText) {
    const m = labModal(path, [el("p", { class: "hint", text: _t("バイナリファイルのため、ここでは編集できません。") })], {
      actions: editable ? [el("button", { class: "btn-danger", text: _t("削除"), onclick: () => deleteThemeFile(state, path, m) })] : [],
    });
    return;
  }
  const data = await api(themePath(state.name, state.id, `/files?path=${encodeURIComponent(path)}`));
  const area = el("textarea", { rows: 26, class: "code-area" });
  area.value = data.content;
  const save = el("button", { class: "primary", text: _t("保存"), disabled: editable ? null : "disabled" });
  const del = el("button", { class: "btn-danger", text: _t("削除"), disabled: editable ? null : "disabled" });
  const m = labModal(path, [area], { wide: true, actions: [del, save] });
  save.addEventListener("click", async () => {
    if (await saveThemeFile(state, path, area.value, m.errorBox)) m.close();
  });
  del.addEventListener("click", () => deleteThemeFile(state, path, m));
}

async function deleteThemeFile(state, path, m) {
  if (!confirm(_t(`{0} を削除します。よろしいですか？`, path))) return;
  if (await labAction(() => api(themePath(state.name, state.id, `/files?path=${encodeURIComponent(path)}`), { method: "DELETE" }), m.errorBox)) {
    m.close();
    await state.reload();
    refreshThemeSections(state);
  }
}

function openNewFileModal(state, skill) {
  const rel = el("input", { type: "text", placeholder: _t("scripts/run.py や references/notes.md") });
  const area = el("textarea", { rows: 18, class: "code-area" });
  const save = el("button", { class: "primary", text: _t("作成") });
  const m = labModal(_t(`{0} にファイルを追加`, skill), [el("label", {}, [_t("スキル内のパス"), rel]), area], { wide: true, actions: [save] });
  save.addEventListener("click", async () => {
    const path = `assets/skills/${skill}/${rel.value.trim().replace(/^\/+/, "")}`;
    if (await saveThemeFile(state, path, area.value, m.errorBox)) {
      m.close();
      refreshThemeSections(state);
    }
  });
}

/* サブエージェントのフォーム編集（frontmatter の name・description・tools・model と本文、run_script の許可） */

/* frontmatter を項目ごとに分ける。値は1行の文字列・["a", "b"] のリスト・次の行からの「- 項目」の
 * リスト・| や > の複数行の文字列を読む。フォームで扱わない項目は元の行のまま残し（raw）、保存時に書き戻す。
 * 読めない形（入れ子等）があれば complex を立て、フォームではなくファイルの直接編集に回す。 */
function splitFrontmatter(text) {
  const m = /^\s*---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)([\s\S]*)$/.exec(text);
  if (!m) return { fm: {}, raw: {}, order: [], body: text, complex: false };
  const lines = m[1].split(/\r?\n/);
  const fm = {};
  const raw = {};
  const order = [];
  let complex = false;
  const unquote = (v) => {
    const t = v.trim();
    if (/^"(.*)"$/.test(t)) {
      try {
        return JSON.parse(t);
      } catch (e) {
        return t.slice(1, -1);
      }
    }
    if (/^'(.*)'$/.test(t)) return t.slice(1, -1).replace(/''/g, "'");
    return t;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim() || /^\s*#/.test(line)) continue;
    const km = /^([A-Za-z_][\w-]*)\s*:(.*)$/.exec(line);
    if (!km) {
      complex = true;
      continue;
    }
    const key = km[1];
    const rest = [];
    while (i + 1 < lines.length && (/^\s/.test(lines[i + 1]) || !lines[i + 1].trim())) rest.push(lines[++i]);
    order.push(key);
    raw[key] = [line, ...rest].join("\n");
    const value = km[2].trim();
    const items = rest.filter((l) => l.trim());
    if (/^[|>][+-]?$/.test(value)) {
      const indent = Math.min(...items.map((l) => /^\s*/.exec(l)[0].length));
      fm[key] = items.map((l) => l.slice(indent)).join(value.startsWith(">") ? " " : "\n");
    } else if (!value && items.length && items.every((l) => /^\s*-\s+/.test(l))) {
      fm[key] = items.map((l) => unquote(l.replace(/^\s*-\s+/, "")));
    } else if (/^\[.*\]$/.test(value)) {
      fm[key] = value.slice(1, -1).split(",").map(unquote).filter(Boolean);
    } else if (items.length) {
      // フォームで扱わない項目の入れ子は raw のまま書き戻せるので、扱う項目のときだけ直接編集に回す。
      if (["name", "description", "tools", "model"].includes(key)) complex = true;
    } else {
      fm[key] = unquote(value);
    }
  }
  return { fm, raw, order, body: m[2], complex };
}

/* YAML の1行の文字列として安全な形（記号を含むときは二重引用符で囲む）。 */
function yamlScalar(text) {
  const t = String(text).replace(/\r?\n/g, " ").trim();
  const plain = /^[\w　-鿿＀-￯]/.test(t) && !/[:#]\s|[:#]$|\s#|[{}[\],]/.test(t);
  const looksTyped = /^(true|false|yes|no|on|off|null|~|[-+]?[\d.][\d._]*(e[-+]?\d+)?)$/i.test(t);
  return plain && !looksTyped ? t : JSON.stringify(t);
}

async function openAgentEditor(state, agent) {
  const path = `assets/agents/${agent}.md`;
  const [file, patchFile] = await Promise.all([
    api(themePath(state.name, state.id, `/files?path=${encodeURIComponent(path)}`)),
    api(themePath(state.name, state.id, `/files?path=${encodeURIComponent("config_patch.json")}`)),
  ]);
  const { fm, raw, order, body, complex } = splitFrontmatter(file.content);
  if (complex || typeof fm.description === "object" || typeof fm.model === "object") {
    // フォームで書き戻すと項目が欠けるおそれがある形は、ファイルをそのまま編集させる。
    alert(_t("このサブエージェントの定義はフォームで扱えない書き方を含むため、ファイルを直接編集します。"));
    return openFileEditor(state, path, true);
  }
  const desc = el("textarea", { rows: 3 });
  desc.value = fm.description || "";
  const model = el("input", { type: "text", placeholder: _t("空欄なら本番と同じ（inherit）") });
  model.value = fm.model || "";
  const hadTools = "tools" in fm;
  const toolList = Array.isArray(fm.tools) ? fm.tools : String(fm.tools || "").split(",");
  const current = new Set(toolList.map((t) => String(t).trim()).filter(Boolean));
  const tools = [...new Set([...(state.sources.tools || []), ...current])];
  const toolBoxes = tools.map((t) => {
    const cb = el("input", { type: "checkbox", value: t });
    cb.checked = current.has(t);
    return el("label", { class: "checkbox tool-check" }, [cb, ` ${t}`]);
  });
  const bodyArea = el("textarea", { rows: 18, class: "code-area" });
  bodyArea.value = body;
  let patch = {};
  try {
    patch = JSON.parse(patchFile.content || "{}");
  } catch (e) {
    patch = {};
  }
  const allow = ((patch.subagent || {}).agent_type_run_script_allowlist || []).filter((e) => e[0] === agent).map((e) => (Array.isArray(e[1]) ? e[1].join("/") : e[1]));
  const allowInput = el("textarea", { rows: 3, placeholder: _t("1行に1つ。スキル名、または スキル名/スクリプト名.py") });
  allowInput.value = allow.join("\n");
  const skillNames = [...(state.sources.skills || []).map((s) => s.name), ...state.data.summary.assets.skills];
  const save = el("button", { class: "primary", text: _t("保存") });
  const m = labModal(
    _t(`サブエージェント {0}`, agent),
    [
      el("label", {}, [_t("説明（description。メインのアシスタントが委譲先を選ぶ手がかり）"), desc]),
      el("div", {}, [
        el("div", { class: "hint", text: _t("使えるツール（tools）。1つも選ばないと、既定のツール一式をすべて使えるサブエージェントになります。") }),
        el("div", { class: "tool-grid" }, toolBoxes.length ? toolBoxes : [el("span", { class: "hint", text: state.sources.tools_error || _t("ツール一覧を取得できません") })]),
      ]),
      el("label", {}, [_t("モデル（model、任意）"), model]),
      el("label", {}, [_t("run_script で呼んでよいスキル（空欄なら制限なし）"), allowInput]),
      el("p", { class: "hint", text: _t(`スキル名の候補: {0}`, [...new Set(skillNames)].join(", ")) }),
      el("label", {}, [_t("本文（システムプロンプト。{{skills}}・{{run_script_allowlist}} はスキル一覧・許可一覧に置き換わります）"), bodyArea]),
    ],
    { wide: true, actions: [save] },
  );
  save.addEventListener("click", async () => {
    const selected = toolBoxes.map((l) => l.querySelector("input")).filter((cb) => cb.checked).map((cb) => cb.value);
    if (!selected.length && hadTools && current.size && !confirm(_t("ツールを1つも選んでいません。既定のツール一式をすべて使えるようになります。よろしいですか？"))) return;
    const lines = ["---", `name: ${agent}`, `description: ${yamlScalar(desc.value)}`];
    if (selected.length) lines.push(`tools: ${selected.join(", ")}`);
    if (model.value.trim()) lines.push(`model: ${yamlScalar(model.value)}`);
    // フォームで扱わない項目は元の書き方のまま残す。
    for (const key of order) if (!["name", "description", "tools", "model"].includes(key)) lines.push(raw[key]);
    lines.push("---", "");
    const content = lines.join("\n") + bodyArea.value.replace(/^\r?\n/, "");
    if (!(await saveThemeFile(state, path, content, m.errorBox))) return;
    const entries = allowInput.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean).map((s) => (s.includes("/") ? [agent, [s.slice(0, s.indexOf("/")).trim(), s.slice(s.indexOf("/") + 1).trim()]] : [agent, s]));
    patch.subagent = patch.subagent || {};
    const others = (patch.subagent.agent_type_run_script_allowlist || []).filter((e) => e[0] !== agent);
    patch.subagent.agent_type_run_script_allowlist = [...others, ...entries];
    if (!patch.subagent.agent_type_run_script_allowlist.length) delete patch.subagent.agent_type_run_script_allowlist;
    if (!Object.keys(patch.subagent).length) delete patch.subagent;
    if (await saveThemeFile(state, "config_patch.json", JSON.stringify(patch, null, 2) + "\n", m.errorBox)) m.close();
  });
}

/* --- ケース --- */

function renderThemeCases(state, body) {
  const d = state.data;
  const editable = themeEditable(state);
  const err = el("div", { class: "error" });
  const rows = d.cases_list.map((c) => [
    c.id,
    c.error ? el("span", { class: "error", text: c.error }) : (c.turns || []).join(" → "),
    c.work_dir || "-",
    c.judge ? _t("あり") : "-",
    el("span", {}, [
      linkBtn(_t("編集"), () => openCaseEditor(state, c.id)),
      " ",
      linkBtn("YAML", () => openFileEditor(state, c.path, true)),
      " ",
      linkBtn(_t("1回試す"), () => addTask(state, "trial", { case_id: c.id }, err)),
    ]),
  ]);
  body.append(
    el("h4", {}, [requiredLabel(_t("③ 試験問題（1問以上）"), true)]),
    el("p", { class: "hint", text: _t("② のとおりに動くかを、実際に試すための問題です。1問ごとに「利用者が打ちそうな指示」と「どうなっていれば合格か」を決めます。") }),
    el("p", { class: "hint", text: _t("例: 指示「この見積書をチェックして」（見積書のファイルを添えて）→ 合格の条件「回答に計算ミスの箇所が書かれている」") }),
    el("p", {
      class: "hint",
      text: _t("「スキル調整ループを開始」を押すと、AI がこの試験を何度も解き、不合格ならスキルを直して解き直します。問題そのものが②と食い違っているときだけ、AI が問題の方を直すことがあります（直した内容は昇格前に確認できます）。"),
    }),
    dataTable(["ID", _t("利用者の指示"), _t("使うファイル"), _t("AI が判断する点"), ""], rows, _t("まだ試験問題はありません。")),
    err,
  );
  if (editable) {
    const count = el("input", { type: "number", min: 1, max: 10, value: 3, class: "narrow" });
    const request = el("input", { type: "text", placeholder: _t("AI への補足（任意）") });
    body.append(
      el("div", { class: "inline-form" }, [
        el("button", { class: "primary", text: _t("＋ 試験問題を自分で作る"), onclick: () => openCaseEditor(state, null) }),
      ]),
      el("div", { class: "inline-form" }, [
        el("span", { class: "hint", text: _t("または ① と ② をもとに AI に") }),
        count,
        el("span", { class: "hint", text: _t("問 作ってもらう") }),
        request,
        el("button", { text: _t("AI に作ってもらう"), onclick: () => draftWithCheck(state, "draft_cases", { count: Number(count.value) || 3, request: request.value }, err) }),
      ]),
    );
  }

  body.append(
    el("h5", {}, [requiredLabel(_t("試験問題で使うファイル"), false)]),
    el("p", { class: "hint", text: _t("見積書など、試験問題で AI に渡すファイルをここに置きます。試験のたびに写しを使うので、元のファイルは変わりません。") }),
  );
  const fixtureFiles = d.files.filter((f) => f.path.startsWith("cases/fixtures/"));
  body.append(
    dataTable(
      [_t("ファイル"), _t("サイズ"), ""],
      fixtureFiles.map((f) => [f.path.replace("cases/", ""), `${f.size} B`, editable ? linkBtn(_t("削除"), () => deleteThemeFile(state, f.path, { errorBox: err, close: () => {} })) : ""]),
      _t("まだありません。"),
    ),
  );
  if (editable) {
    const folder = el("input", { type: "text", placeholder: _t("フォルダ名（例: sample1）") });
    const fileInput = el("input", { type: "file", multiple: "multiple" });
    const fileBox = el("span", {}, [fileInput]);
    localizeFileInput(fileInput);
    body.append(
      el("div", { class: "inline-form" }, [
        folder,
        fileBox,
        el("button", {
          text: _t("アップロード"),
          onclick: async () => {
            if (!folder.value.trim() || !fileInput.files.length) {
              err.textContent = _t("フォルダ名とファイルを指定してください。");
              return;
            }
            for (const file of fileInput.files) {
              const content = await fileToBase64(file);
              const ok = await labAction(
                () => api(themePath(state.name, state.id, "/fixtures"), { method: "POST", body: { folder: folder.value.trim(), filename: file.name, content_base64: content } }),
                err,
              );
              if (!ok) return;
            }
            await state.reload();
            refreshThemeSections(state);
          },
        }),
      ]),
      el("p", { class: "hint", text: _t("フォルダ名を付けてアップロードし、試験問題の「使うファイル」でそのフォルダ（fixtures/<フォルダ名>）を選びます。") }),
    );
  }
  return inputsChanged(body);
}

function linesOf(text) {
  return text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
}

function requiredLabel(text, required) {
  return el("span", {}, [text, el("span", { class: required ? "req-mark" : "hint", text: required ? _t("【必須】") : _t("（任意）") })]);
}

/* 名前を空欄で保存したときの問題の名前（case_001 から空いている番号）。 */
function nextCaseId(state) {
  const used = new Set(state.data.cases_list.map((c) => c.id));
  let n = 1;
  while (used.has(`case_${String(n).padStart(3, "0")}`)) n++;
  return `case_${String(n).padStart(3, "0")}`;
}

async function openCaseEditor(state, caseId) {
  const editable = themeEditable(state);
  const data = caseId ? (await api(themePath(state.name, state.id, `/cases/${encodeURIComponent(caseId)}`))).data : { turns: [""], auto_approve: true };
  const expect = data.expect || {};
  const id = el("input", { type: "text", placeholder: _t(`空欄なら {0}（英数字・-・_）`, nextCaseId(state)) });
  id.value = caseId || "";
  if (caseId) id.disabled = true;
  const turns = el("textarea", { rows: 4, placeholder: _t("例: この見積書をチェックして") });
  turns.value = (data.turns || []).join("\n---\n");
  const assets = state.data.summary.assets;
  const readArgs = ((expect.tool_call_args_contains || {}).read_skill || {}).skill_name;
  const dispatchArgs = ((expect.tool_call_args_contains || {}).dispatch_agent || {}).agent_type;
  const skillSel = el("select", {}, [el("option", { value: "", text: _t("（確かめない）") }), ...[...assets.skills, ...state.sources.skills.map((s) => s.name)].filter((v, i, a) => a.indexOf(v) === i).map((n) => el("option", { value: n, text: n }))]);
  skillSel.value = readArgs || "";
  const agentSel = el("select", {}, [el("option", { value: "", text: _t("（確かめない）") }), ...[...assets.agents, ...state.sources.agents.map((a) => a.name)].filter((v, i, a) => a.indexOf(v) === i).map((n) => el("option", { value: n, text: n }))]);
  agentSel.value = dispatchArgs || "";
  const called = el("input", { type: "text", placeholder: _t("カンマ区切り（いずれかが呼ばれれば合格）") });
  called.value = (expect.tool_called_any || []).join(", ");
  const notCalled = el("input", { type: "text", placeholder: _t("カンマ区切り（どれも呼ばれなければ合格）") });
  notCalled.value = (expect.tool_not_called || []).join(", ");
  const contains = el("textarea", { rows: 2, placeholder: _t("1行に1つ（最終回答にすべて含まれれば合格）") });
  contains.value = (expect.response_contains || []).join("\n");
  const notContains = el("textarea", { rows: 2, placeholder: _t("1行に1つ（最終回答にどれも含まれなければ合格）") });
  notContains.value = (expect.response_not_contains || []).join("\n");
  const judgeArea = el("textarea", { rows: 3, placeholder: _t("AI の判断は、昇格する前に人が確認します") });
  judgeArea.value = data.judge || "";
  // 今の値が一覧に無い（フォルダを消した・fixtures/ 以外の書き方）ときも選択肢に残し、保存で黙って消さない。
  const workDirChoices = [...state.data.fixtures];
  if (data.work_dir && !workDirChoices.includes(data.work_dir)) workDirChoices.push(data.work_dir);
  const workDir = el("select", {}, [
    el("option", { value: "", text: _t("（使わない）") }),
    ...workDirChoices.map((f) => el("option", { value: f, text: state.data.fixtures.includes(f) ? f : _t(`{0}（見つかりません）`, f) })),
  ]);
  workDir.value = data.work_dir || "";
  const answers = el("textarea", { rows: 2, placeholder: _t("AI がユーザーに質問したときに返す答え（1行に1つ、順に使う）") });
  answers.value = (data.scripted_text_answers || []).join("\n");
  const autoApprove = el("input", { type: "checkbox" });
  autoApprove.checked = data.auto_approve !== false;
  const timeout = el("input", { type: "number", min: 60, placeholder: _t("既定900"), class: "narrow" });
  timeout.value = data.timeout_seconds || "";
  const notes = el("input", { type: "text", placeholder: _t("このケースで確かめること") });
  notes.value = data.notes || "";
  const save = el("button", { class: "primary", text: _t("保存"), disabled: editable ? null : "disabled" });
  const m = labModal(
    caseId ? _t(`試験問題 {0}`, caseId) : _t("新しい試験問題"),
    [
      el("p", { class: "hint", text: _t("必須なのは「利用者の指示」と、合格の決め方（言葉の条件か AI が判断する点の、どちらか1つ）だけです。ほかは空欄のままで構いません。") }),
      el("label", {}, [requiredLabel(_t("利用者の指示（AI に打つ文章）"), true), turns]),
      el("fieldset", { class: "case-expect" }, [
        el("legend", { text: _t("合格の決め方【必須】下のどれか1つ以上を入れる") }),
        el("label", {}, [_t("回答に必ず入っているべき言葉（1行に1つ）"), contains]),
        el("label", {}, [_t("回答に入っていてはいけない言葉（1行に1つ）"), notContains]),
        el("label", {}, [_t("AI が判断する点（言葉では決めにくい良し悪し。例: 指摘が具体的で、根拠の箇所が示されている）"), judgeArea]),
      ]),
      el("label", {}, [requiredLabel(_t("使うファイル（「試験問題で使うファイル」でアップロードしたフォルダ）"), false), workDir]),
      el("details", { class: "theme-advanced" }, [
        el("summary", { text: _t("細かい設定（任意。通常はそのままで構いません）") }),
        el("label", {}, [_t("問題の名前（英数字・-・_）"), id]),
        el("label", {}, [_t("このスキルを使ったか"), skillSel]),
        el("label", {}, [_t("このサブエージェントに任せたか"), agentSel]),
        el("label", {}, [_t("使うべき機能（ツール名）"), called]),
        el("label", {}, [_t("使ってはいけない機能（ツール名）"), notCalled]),
        el("p", { class: "hint", text: _t("やり取りが続く場合: 利用者の指示に --- だけの行をはさむと、そこから先が2回目以降の発言になります。") }),
        el("label", {}, [_t("AI が利用者に質問してきたときの返事（1行に1つ、順に使う）"), answers]),
        el("label", { class: "checkbox" }, [autoApprove, _t(" 「実行してよいか」の確認には自動で「はい」と答える")]),
        el("label", {}, [_t("制限時間（秒）"), timeout]),
        el("label", {}, [_t("メモ"), notes]),
      ]),
    ],
    { wide: true, actions: [save] },
  );
  save.addEventListener("click", async () => {
    const cid = id.value.trim() || nextCaseId(state);
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(cid)) {
      m.errorBox.textContent = _t("問題の名前は、英数字で始まる英数字・-・_ にしてください（空欄なら自動で付けます）。");
      return;
    }
    if (!turns.value.trim()) {
      m.errorBox.textContent = _t("「利用者の指示」を入力してください。");
      return;
    }
    const exp = {};
    const args = {};
    if (skillSel.value) args.read_skill = { skill_name: skillSel.value };
    if (agentSel.value) args.dispatch_agent = { agent_type: agentSel.value };
    if (Object.keys(args).length) exp.tool_call_args_contains = { ...(expect.tool_call_args_contains || {}), ...args };
    else if (expect.tool_call_args_contains) {
      const rest = { ...expect.tool_call_args_contains };
      delete rest.read_skill;
      delete rest.dispatch_agent;
      if (Object.keys(rest).length) exp.tool_call_args_contains = rest;
    }
    const csv = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
    if (csv(called.value).length) exp.tool_called_any = csv(called.value);
    if (csv(notCalled.value).length) exp.tool_not_called = csv(notCalled.value);
    if (linesOf(contains.value).length) exp.response_contains = linesOf(contains.value);
    if (linesOf(notContains.value).length) exp.response_not_contains = linesOf(notContains.value);
    if (!Object.keys(exp).length && !judgeArea.value.trim()) {
      m.errorBox.textContent = _t("合格の決め方を1つ以上入れてください（回答に入っているべき言葉、または AI が判断する点など）。");
      return;
    }
    // フォームに無い項目（env 等）は、読み込んだケースの値のまま残す。
    const payload = {
      ...data,
      turns: turns.value.split(/\r?\n---\r?\n/).map((s) => s.trim()).filter(Boolean),
      expect: Object.keys(exp).length ? exp : null,
      judge: judgeArea.value.trim() || null,
      work_dir: workDir.value || null,
      scripted_text_answers: linesOf(answers.value),
      auto_approve: autoApprove.checked,
      timeout_seconds: timeout.value ? Number(timeout.value) : null,
      notes: notes.value.trim() || null,
    };
    const url = themePath(state.name, state.id, `/cases/${encodeURIComponent(cid)}${caseId ? "" : "?create=true"}`);
    const ok = await labAction(() => api(url, { method: "PUT", body: { data: payload } }), m.errorBox);
    if (ok) {
      m.close();
      await state.reload();
      refreshThemeSections(state);
    }
  });
}

function showTrialResult(r) {
  if (!r) return;
  const outcome = { pass: _t("合格"), fail: _t("不合格"), error: _t("エラー") }[r.outcome] || r.outcome;
  labModal(
    _t(`1回試行: {0}（{1}）`, r.case_id, outcome),
    [
      r.ai_judge ? el("p", {}, [el("strong", { text: `AI judge: ${r.ai_judge.pass ? _t("合格") : _t("不合格")} ` }), r.ai_judge.reason]) : null,
      r.error ? el("p", { class: "error", text: `${r.error}: ${r.detail || ""}` }) : null,
      el("h4", { text: _t("満たさなかった判定") }),
      el("pre", { class: "lab-pre", text: JSON.stringify(Object.fromEntries(Object.entries(r.rule_results || {}).filter(([, v]) => !v.pass)), null, 2) }),
      el("h4", { text: _t("最終回答") }),
      el("pre", { class: "lab-pre", text: r.final_answer || "" }),
      el("h4", { text: _t("実行の記録（抜粋）") }),
      el("pre", { class: "lab-pre", text: r.excerpt || "" }),
    ].filter(Boolean),
    { wide: true },
  );
}

/* --- 実行の記録 --- */

function renderThemeRuns(state, body) {
  const its = (state.data.iterations || []).slice().reverse();
  const rows = its.map((it) => [
    String(it.n),
    it.phase === "tryout" ? _t(`トライアウト（各{0}回）`, it.repeat) : _t("評価（1回）"),
    el("span", { class: it.verdict === "pass" ? "ok-text" : "error", text: it.verdict === "pass" ? _t("全回合格") : _t("不合格あり") }),
    Object.entries(it.cases || {}).map(([cid, c]) => `${cid}: ${c.pass}/${c.pass + c.fail + c.error}`).join(_t("、")),
    it.fix ? (it.fix.applied && it.fix.applied.length ? _t(`AI が直した: {0}`, it.fix.applied.join(", ")) : _t(`直せず: {0}`, (it.fix.errors || []).join(" / "))) : "-",
    linkBtn(_t("詳しく"), () => openIterationDetail(state, it.n)),
  ]);
  body.append(
    el("p", { class: "hint", text: _t("評価で失敗があると AI が原因を分析して直し、もう一度評価します。全ケースに合格したらトライアウト（対象インスタンスの tryout_repeats 回ずつ）を行い、全回合格でレビュー待ちになります。") }),
    dataTable([_t("反復"), _t("種類"), _t("判定"), _t("ケースごとの合格"), _t("AI 修正"), ""], rows, _t("まだ実行していません。")),
  );
}

async function openIterationDetail(state, n) {
  const it = await api(themePath(state.name, state.id, `/iterations/${n}`));
  const nodes = [];
  if (it.fix) {
    nodes.push(el("h4", { text: _t("AI の分析と修正") }), el("p", { text: it.fix.analysis || "" }));
    if (it.fix.applied && it.fix.applied.length) nodes.push(el("p", { text: _t(`直したファイル: {0}`, it.fix.applied.join(", ")) }));
    if (it.fix.errors && it.fix.errors.length) nodes.push(el("p", { class: "error", text: _t(`適用できなかった理由: {0}`, it.fix.errors.join(" / ")) }));
  }
  nodes.push(el("h4", { text: _t("各回の結果") }));
  for (const r of it.runs) {
    const outcome = { pass: _t("合格"), fail: _t("不合格"), error: _t("エラー") }[r.outcome] || r.outcome;
    nodes.push(
      el("details", { class: `run-detail run-${r.outcome}` }, [
        el("summary", { text: _t(`{0}（{1}回目）: {2}{3}`, r.case_id, r.repeat_index, outcome, r.ai_judge ? ` ／ AI judge: ${r.ai_judge.reason}` : "") }),
        Object.keys(r.failed_rules || {}).length ? el("pre", { class: "lab-pre", text: JSON.stringify(r.failed_rules, null, 2) }) : null,
        r.error ? el("p", { class: "error", text: `${r.error}: ${r.detail || ""}` }) : null,
        el("div", { class: "hint", text: _t("最終回答") }),
        el("pre", { class: "lab-pre", text: r.final_answer || "" }),
        el("div", { class: "hint", text: _t("実行の記録（抜粋）") }),
        el("pre", { class: "lab-pre", text: r.excerpt || "" }),
      ]),
    );
  }
  labModal(_t(`反復 {0}（{1}）`, n, it.phase === "tryout" ? _t("トライアウト") : _t("評価")), nodes, { wide: true });
}

/* --- レビュー・昇格 --- */

async function renderThemeReview(state, body) {
  const d = state.data;
  const t = d.tryout;
  if (t) {
    body.append(
      el("h4", { text: _t("トライアウト") }),
      el("p", {}, [
        _t(`各ケース {0} 回、全回合格（{1}、反復 {2}）。`, t.repeat, fmtTime(t.at), t.iteration),
        t.ai_judged ? el("strong", { text: _t(" judge 付きのケースは AI が判定しています。昇格の承認は、その判定を人が確認したことを意味します。") }) : "",
      ]),
    );
    if (d.tryout_problems && d.tryout_problems.length) body.append(el("p", { class: "error", text: d.tryout_problems.join(" / ") }));
  } else {
    body.append(el("p", { class: "hint", text: _t("有効なトライアウトの結果がありません（研究対象・ケース・設定パッチを変えると0回からやり直しになります）。") }));
  }

  const diff = await api(themePath(state.name, state.id, "/diff"));
  body.append(el("h4", { text: _t("取り込み・複製した時点（新規は空）からの変更") }));
  if (diff.ai_changed_cases) body.append(el("p", { class: "warn-box", text: _t("AI がケースを変更しています。ケースが仕様どおりの確かめ方になっているか、「履歴」と「ケース」で確認してください。") }));
  if (!diff.files.length) body.append(el("p", { class: "hint", text: _t("変更はありません。") }));
  for (const f of diff.files) {
    body.append(
      el("details", { class: `diff-file ${f.important ? "diff-important" : ""}` }, [
        el("summary", { text: _t(`{0}（{1}）{2}`, f.path, { added: _t("追加"), deleted: _t("削除"), modified: _t("変更") }[f.status], f.important ? _t(" ★スクリプト") : "") }),
        el("pre", { class: "lab-pre diff-pre", text: f.diff }),
      ]),
    );
  }

  if (!themeEditable(state)) {
    if (d.promotion) body.append(el("h4", { text: _t("昇格の記録") }), el("pre", { class: "lab-pre", text: JSON.stringify(d.promotion, null, 2) }));
    if (d.returned) body.append(el("h4", { text: _t("差し戻しの記録") }), el("pre", { class: "lab-pre", text: JSON.stringify(d.returned, null, 2) }));
    return;
  }

  body.append(el("h4", { text: _t("昇格") }));
  const promoBox = el("div", { class: "promote-box" });
  body.append(promoBox);
  await renderPromoteBox(state, promoBox, []);

  // 差し戻し先は取り込み元のドラフトなので、ドラフトから始めたテーマにだけ出す（それ以外は「取りやめ」で終える）。
  if (!Object.values(d.sources.skills || {}).some((s) => s.kind === "draft")) return;
  body.append(el("h4", { text: _t("差し戻し・不採用") }));
  const reason = el("textarea", { rows: 3, placeholder: _t("作成者が直せるよう、具体的に（作成者の skill-creator に表示されます）") });
  const applyFixes = el("input", { type: "checkbox" });
  const err = el("div", { class: "error" });
  const doReturn = async (reject) => {
    if (!confirm(reject ? _t("不採用にします。よろしいですか？") : _t("取り込み元のドラフトへ差し戻します。よろしいですか？"))) return;
    const res = await labAction(
      () => api(themePath(state.name, state.id, "/return"), { method: "POST", body: { reason: reason.value, apply_fixes: applyFixes.checked, reject } }),
      err,
    );
    if (res) {
      await state.reload();
      renderThemeSub(state, body);
    }
  };
  body.append(
    reason,
    el("label", { class: "checkbox" }, [applyFixes, _t(" 研究室での修正をドラフトへ写してから差し戻す")]),
    el("div", { class: "inline-form" }, [el("button", { text: _t("差し戻す"), onclick: () => doReturn(false) }), el("button", { class: "btn-danger", text: _t("不採用にする"), onclick: () => doReturn(true) })]),
    err,
  );
}

async function renderPromoteBox(state, box, distributeTo) {
  box.innerHTML = "";
  const qs = distributeTo.map((n) => `distribute_to=${encodeURIComponent(n)}`).join("&");
  const check = await api(themePath(state.name, state.id, `/promote-check${qs ? `?${qs}` : ""}`));
  for (const p of check.problems) box.append(el("p", { class: "error", text: `× ${p}` }));
  for (const n of check.notices) box.append(el("p", { class: "hint", text: _t(`・{0}`, n) }));
  for (const [inst, pl] of Object.entries(check.placements)) {
    box.append(
      el("div", { class: "placement" }, [
        el("strong", { text: _t(`{0} の専用の置き場へ: `, inst) }),
        el("code", { text: pl.dir }),
        el("ul", {}, [
          ...[...pl.skills, ...pl.agents].map((p) => el("li", { text: p })),
          ...(pl.replaces || []).map((r) => el("li", { class: "hint", text: _t(`置き換え（元は evals/history/promote/ へ退避）: {0}`, r) })),
          ...Object.entries(pl.register || {}).map(([k, v]) => el("li", { class: "hint", text: _t(`設定に追加: [{0} {1}`, k.replace(".", "]."), JSON.stringify(v)) })),
        ]),
      ]),
    );
  }
  const distBoxes = check.distributable.map((n) => {
    const cb = el("input", { type: "checkbox", value: n });
    cb.checked = distributeTo.includes(n);
    cb.addEventListener("change", () => renderPromoteBox(state, box, distBoxes.map((l) => l.querySelector("input")).filter((c) => c.checked).map((c) => c.value)));
    return el("label", { class: "checkbox" }, [cb, ` ${n}`]);
  });
  if (distBoxes.length) box.append(el("div", { class: "hint", text: _t("他のインスタンスにも配布する（任意。それぞれの専用の置き場へ複製し、設定も登録します）") }), el("div", { class: "inline-form" }, distBoxes));
  const confirmBoxes = check.confirmations.map((c) => el("label", { class: "checkbox warn-box" }, [el("input", { type: "checkbox", class: "confirm-cb" }), ` ${c}`]));
  box.append(...confirmBoxes);
  const note = el("input", { type: "text", placeholder: _t("判定メモ（任意。evals/promotion_log.md に残ります）") });
  const err = el("div", { class: "error" });
  const btn = el("button", { class: "primary", text: _t("承認して昇格する"), disabled: check.ok ? null : "disabled" });
  btn.addEventListener("click", async () => {
    if (confirmBoxes.some((l) => !l.querySelector("input").checked)) {
      err.textContent = _t("確認事項にチェックを入れてください。");
      return;
    }
    if (!confirm(_t("昇格します。配置先のインスタンスは再起動後に使えるようになります。よろしいですか？"))) return;
    const res = await labAction(
      () => api(themePath(state.name, state.id, "/promote"), { method: "POST", body: { distribute_to: distributeTo, discard_source_changes: confirmBoxes.length > 0, note: note.value } }),
      err,
    );
    if (!res) return;
    alert(
      _t(`昇格しました。{0}`, res.archived_drafts.length ? _t(`ドラフト {0} 件をアーカイブしました。`, res.archived_drafts.length) : "") +
        (res.restart_needed.length ? _t(`\n稼働中のインスタンス（{0}）を再起動すると使えるようになります。`, res.restart_needed.join(", ")) : ""),
    );
    await state.reload();
    renderThemeSub(state, $(".theme-body"));
  });
  box.append(note, btn, err);
}

/* --- 履歴 --- */

function renderThemeHistory(state, body) {
  const rows = (state.data.history || []).slice().reverse().map((h) => [fmtTime(h.at), h.actor, h.action, h.detail || ""]);
  body.append(dataTable([_t("日時"), _t("実行者"), _t("操作"), _t("内容")], rows, _t("まだありません。")));
}
