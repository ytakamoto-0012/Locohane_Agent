"use strict";
/* 設定ダッシュボード（管理ツール）フロントエンド。ビルド不要の素のJS。
 * バックエンドAPIは admin/server.py 参照。Cookieセッション + CSRFヘッダ
 * （X-Locohane-Admin: 1、更新系リクエストのみ）で認証する。
 */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function clone(tplId) {
  const tpl = document.getElementById(tplId);
  const fragment = tpl.content.cloneNode(true);
  applyI18n(fragment);
  return fragment;
}

async function api(path, opts) {
  opts = opts || {};
  const method = opts.method || "GET";
  // X-Locohane-Lang: サーバーのエラーメッセージ・管理アシスタントの回答の言語（admin/i18n.py）。
  const headers = { "Content-Type": "application/json", "X-Locohane-Lang": I18N_LANG };
  if (method !== "GET") headers["X-Locohane-Admin"] = "1";
  const doFetch = () =>
    fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  let res = await doFetch();
  // ダッシュボード表示中にセッションが切れた（放置によるタイムアウト・管理ツールの
  // 再起動等）場合は、画面を残したまま再ログインさせ、元のリクエストを再送する。
  // リロードさせると編集中の入力内容が失われるため。
  if (res.status === 401 && mainEl && path !== "/api/login" && path !== "/api/logout") {
    await promptRelogin();
    res = await doFetch();
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      if (data && data.detail) detail = data.detail;
    } catch (e) {
      /* ignore */
    }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

const app = $("#app");

/* ---------------------------------------------------------------- 起動 */

async function boot() {
  try {
    const me = await api("/api/me");
    showShell(me.username, me.version);
  } catch (e) {
    showLogin();
  }
}

function showLogin(message) {
  mainEl = null;
  app.innerHTML = "";
  app.appendChild(clone("tpl-login"));
  $("#login-lang").appendChild(createLanguageSelect());
  if (message) $("#login-error").textContent = message;
  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = new FormData(ev.target);
    try {
      const result = await api("/api/login", {
        method: "POST",
        body: { username: form.get("username"), password: form.get("password") },
      });
      showShell(result.username, result.version);
    } catch (e) {
      $("#login-error").textContent = e.message || _t("ログインに失敗しました。");
    }
  });
}

let mainEl = null;
// 再ログイン待ちの Promise。同時に複数のリクエストが 401 になっても、
// オーバーレイは1つだけ出し、全員がそのログイン完了を待つ。
let reloginPromise = null;

function promptRelogin() {
  if (reloginPromise) return reloginPromise;
  reloginPromise = new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "relogin-overlay";
    overlay.appendChild(clone("tpl-login"));
    document.body.appendChild(overlay);
    $(".hint", overlay).textContent = _t("セッションの有効期限が切れました。再度ログインしてください（入力中の内容は保持されます）。");
    $("input[name=username]", overlay).value = $("#me-username") ? $("#me-username").textContent : "";
    $("input[name=password]", overlay).focus();
    $("form", overlay).addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const form = new FormData(ev.target);
      try {
        const result = await api("/api/login", {
          method: "POST",
          body: { username: form.get("username"), password: form.get("password") },
        });
        if ($("#me-username")) $("#me-username").textContent = result.username;
        overlay.remove();
        reloginPromise = null;
        resolve();
      } catch (e) {
        $(".error", overlay).textContent = e.message || _t("ログインに失敗しました。");
      }
    });
  });
  return reloginPromise;
}

function showShell(username, version) {
  app.innerHTML = "";
  app.appendChild(clone("tpl-shell"));
  $("#me-username").textContent = username;
  $("#lang-select-slot").appendChild(createLanguageSelect());
  if (version) $("#app-version").textContent = `v${version}`;
  mainEl = $("#main");
  $("#logout-btn").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    showLogin();
  });
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      navigate(btn.dataset.view);
    });
  });
  navigate("instances");
  // 右上の「AIに相談」（assistant.js）。無効なら何もしない。
  if (typeof initAssistant === "function") initAssistant(username);
}

function navigate(view) {
  stopMonitorPolling();
  if (view === "instances") return renderInstances();
  if (view === "settings") return renderSettings();
  if (view === "audit") return renderAudit();
  if (view === "data-backup") return renderDataBackup();
  if (view === "admin-settings") return renderAdminSettings();
  if (view === "skill-lab") return renderSkillLab();
  if (view === "api-docs") return renderApiDocs();
}

/* ---------------------------------------------------- インスタンス一覧 */

const STATE_LABELS = {
  running: _t("稼働中"),
  stopped: _t("停止中"),
  external: _t("外部で起動中"),
  crashed: _t("異常終了"),
  error: _t("設定エラー"),
};

const PROCESSING_LABELS = {
  start: _t("起動中…"),
  stop: _t("停止中…"),
  restart: _t("再起動中…"),
};

let instanceCache = [];

async function renderInstances() {
  mainEl.innerHTML = "";
  mainEl.appendChild(clone("tpl-instances"));
  $("#add-instance-btn").addEventListener("click", openCreateInstanceModal);
  await refreshInstanceCards();
  // カードの稼働状況だけを定期更新する（カード全体を作り直すと操作中のボタンが消えるため）。
  // 一覧の取得中に別の画面へ移っていたら始めない。
  const cards = $("#instance-cards");
  if (cards) startMonitorPolling(cards, refreshCardActivity);
}

async function refreshInstanceCards() {
  const data = await api("/api/instances");
  instanceCache = data.instances;
  const container = $("#instance-cards");
  if (!container) return;
  // 通常のインスタンスは上段、スキル調整ワーカーは下段に分けて並べる。
  const labContainer = $("#lab-instance-cards");
  container.innerHTML = "";
  labContainer.innerHTML = "";
  $("#lab-instance-section").classList.toggle("hidden", !instanceCache.some((i) => i.kind === "skill_lab"));
  for (const inst of instanceCache) {
    const card = clone("tpl-instance-card").firstElementChild;
    card.querySelector(".state-dot").classList.add(inst.state);
    card.querySelector(".display-name").textContent = inst.display_name;
    card.querySelector(".badge-default").classList.toggle("hidden", !inst.is_default);
    const isLab = inst.kind === "skill_lab";
    card.classList.toggle("lab-card", isLab);
    card.querySelector(".badge-lab").classList.toggle("hidden", !isLab);
    card.querySelector(".addr").textContent = isLab ? _t("スキル研究室の実行役（チャット画面なし）") : `${inst.app_host}:${inst.app_port}`;
    card.querySelector(".state-label").textContent = STATE_LABELS[inst.state] || inst.state;
    const link = card.querySelector(".open-link");
    link.href = inst.url;
    link.classList.toggle("hidden", isLab);
    card.querySelector(".btn-monitor").classList.toggle("hidden", isLab);

    const btnStart = card.querySelector(".btn-start");
    const btnStop = card.querySelector(".btn-stop");
    const btnRestart = card.querySelector(".btn-restart");
    const btnEdit = card.querySelector(".btn-edit");
    const btnDelete = card.querySelector(".btn-delete");

    btnStart.disabled = inst.state === "running" || inst.state === "external";
    btnStop.disabled = inst.state !== "running";
    btnRestart.disabled = inst.state !== "running";
    btnDelete.disabled = inst.state === "running" || inst.state === "external" || inst.is_default;

    btnStart.addEventListener("click", () => runInstanceAction(inst.name, "start", card));
    btnStop.addEventListener("click", () => {
      if (confirm(_t(`{0} を停止します。生成中の処理は中断されます。よろしいですか？`, inst.display_name))) {
        runInstanceAction(inst.name, "stop", card);
      }
    });
    btnRestart.addEventListener("click", () => {
      if (confirm(_t(`{0} を再起動します。生成中の処理は中断されます。よろしいですか？`, inst.display_name))) {
        runInstanceAction(inst.name, "restart", card);
      }
    });
    btnDelete.addEventListener("click", () => openDeleteInstanceModal(inst));
    btnEdit.addEventListener("click", () => openEditInstanceModal(inst));
    card.querySelector(".btn-config").addEventListener("click", () => renderInstanceDetail(inst.name));
    const btnLab = card.querySelector(".btn-lab");
    btnLab.classList.toggle("hidden", isLab);
    btnLab.addEventListener("click", () => renderInstanceDetail(inst.name, "skill-lab"));
    const btnKnowledge = card.querySelector(".btn-knowledge");
    btnKnowledge.classList.toggle("hidden", isLab);
    btnKnowledge.addEventListener("click", () => renderInstanceDetail(inst.name, "knowledge"));
    card.querySelector(".btn-monitor").addEventListener("click", () => renderInstanceDetail(inst.name, "monitor"));
    card.dataset.instance = inst.name;

    (isLab ? labContainer : container).appendChild(card);
  }
  refreshCardActivity();
}

// カードに「接続中ユーザー・生成中スレッド」を表示する（取得失敗時は何も出さない）。
async function refreshCardActivity() {
  let data;
  try {
    data = await api("/api/monitor/overview");
  } catch (e) {
    return;
  }
  for (const item of data.instances) {
    const card = $(`.instance-card[data-instance="${CSS.escape(item.name)}"]`);
    // スキル調整ワーカーにはチャットの統計が無いため、ワーカーの処理状況を別に出す（refreshWorkerCards）。
    if (!card || card.classList.contains("lab-card")) continue;
    const box = card.querySelector(".card-activity");
    const lc = item.log_counts_24h || {};
    const live = (v) => (item.available ? String(v) : "-");
    const tiles = [
      cardStat(_t("接続中ユーザー"), live(item.users.length), item.available && item.users.length ? item.users.join(", ") : null),
      cardStat(_t("接続セッション"), live(item.sessions)),
      cardStat(_t("生成中スレッド"), live(item.generating), item.available && item.waiting ? _t(`うち応答待ち {0}`, item.waiting) : null),
      cardStat(_t("24時間の警告"), fmtNum(lc.WARNING || 0), `ERROR ${fmtNum(lc.ERROR || 0)} / CRITICAL ${fmtNum(lc.CRITICAL || 0)}`),
    ];
    box.replaceChildren(...tiles);
    renderCardContextChart(card.querySelector(".card-context"), item);
    renderCardIngest(card.querySelector(".card-ingest"), item);
  }
  refreshWorkerCards();
}

// カードに「ナレッジで取り込み中のコレクション」と進捗を出す（無ければ何も出さない）。クリックでナレッジ画面を開く。
function renderCardIngest(box, item) {
  const jobs = item.ingesting || [];
  if (!jobs.length) {
    box.replaceChildren();
    return;
  }
  const rows = jobs.map((j) => {
    // 削除・順番待ち（admin/knowledge.py の IngestJob.kind / state）は進捗が無いため、状態だけを出す。
    if (j.kind === "delete" || j.state === "queued") {
      const labels = j.kind === "delete" ? KNOWLEDGE_DELETE_LABELS : j.kind === "copy" ? KNOWLEDGE_COPY_LABELS : KNOWLEDGE_JOB_LABELS;
      const state = labels[j.state] || j.state;
      return el("div", { class: "card-ingest-job", title: `${j.collection}\n${state}` }, [
        el("div", { class: "card-ingest-head" }, [
          el("span", { class: "card-ingest-name", text: j.collection }),
          el("span", { class: "card-ingest-count", text: state }),
        ]),
      ]);
    }
    const state = j.stop_requested ? _t("停止中") : j.trigger === "schedule" ? _t("取り込み中（自動）") : _t("取り込み中");
    const count = j.total ? _t(`{0} / {1} ファイル`, fmtNum(j.done), fmtNum(j.total)) : _t("対象を確認中");
    // 取り込み中のファイルはワーカーのアイコンにカーソルを合わせると出るため、文字では出さない（コピーは枠を使わないため出す）。
    const current = j.current && j.kind === "copy" ? j.current.split(/[\\/]/).pop() : "";
    return el("div", { class: "card-ingest-job", title: [j.collection, state, count, j.current].filter(Boolean).join("\n") }, [
      el("div", { class: "card-ingest-head" }, [
        el("span", { class: "card-ingest-name", text: j.collection }),
        el("span", { class: "card-ingest-count", text: `${state} ${count}` }),
      ]),
      knowledgeProgressBar(j.done, j.total),
      // 使っている取り込みワーカー（全インスタンス共通の並列数の枠）のアイコン。
      knowledgeJobWorkerIcons(j.workers),
      current ? el("div", { class: "card-ingest-file", text: current }) : null,
    ]);
  });
  const title = el("div", { class: "card-ingest-title", text: _t("ナレッジの取り込み") });
  box.replaceChildren(title, ...rows);
  box.onclick = () => renderInstanceDetail(item.name, "knowledge");
}

// スキル調整ワーカー（スキル研究室の実行役）のカードに、処理中のテーマ・待ち件数・LLM 接続先を出す。
async function refreshWorkerCards() {
  if (!$(".instance-card.lab-card")) return;
  let data;
  try {
    data = await api("/api/skill-lab/overview");
  } catch (e) {
    return;
  }
  for (const lab of data.labs) {
    const card = $(`.instance-card[data-instance="${CSS.escape(lab.name)}"]`);
    if (!card) continue;
    const running = lab.running || [];
    const tiles = [
      cardStat(_t("処理中のテーマ"), String(running.length), running.map((r) => `${r.instance}: ${r.title}`).join(", ") || null),
      cardStat(_t("開始待ちのテーマ"), String(lab.queued || 0)),
      cardStat(_t("待ちの依頼"), String(lab.tasks || 0), _t("1回試行・AI による下書き")),
      cardStat(_t("LLM 接続先"), lab.endpoint ? lab.endpoint.replace(/^https?:\/\//, "") : "-", lab.endpoint_error || lab.endpoint || null),
    ];
    card.querySelector(".card-activity").replaceChildren(...tiles);
    card.querySelector(".card-context").replaceChildren();
  }
}

/* --- カードの「生成中スレッドの入力トークン」グラフ --- */

// 濃色パネル上で検証済みのカテゴリ色（dataviz の既定パレット・濃色版）。先頭3色は SERIES_* と同じ。
const THREAD_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
// "<インスタンス名>/<thread_id>" -> 色番号。生成中の間は同じ色を使い続ける
// （他のスレッドの生成が終わっても色を詰め直さない）。
const threadColorSlots = new Map();

function assignThreadColors(instance, threadIds) {
  const prefix = `${instance}/`;
  const keep = new Set(threadIds.map((t) => prefix + t));
  for (const key of [...threadColorSlots.keys()]) {
    if (key.startsWith(prefix) && !keep.has(key)) threadColorSlots.delete(key);
  }
  const used = new Set([...threadColorSlots].filter(([k]) => k.startsWith(prefix)).map(([, v]) => v));
  for (const t of threadIds) {
    const key = prefix + t;
    if (threadColorSlots.has(key)) continue;
    const free = THREAD_COLORS.findIndex((_, i) => !used.has(i));
    if (free < 0) continue; // 色が足りない分は描かない（色を使い回すと区別できないため）
    threadColorSlots.set(key, free);
    used.add(free);
  }
}

function renderCardContextChart(box, item) {
  const series = item.available ? item.context_series || [] : [];
  const sig = JSON.stringify(series);
  if (box.dataset.sig === sig) return; // 変化がなければ描き直さない（ホバー中のツールチップを消さないため）
  box.dataset.sig = sig;
  if (!series.length) {
    box.replaceChildren();
    return;
  }
  assignThreadColors(item.name, series.map((s) => s.thread_id));
  const drawn = [];
  let hidden = 0;
  for (const s of series) {
    const slot = threadColorSlots.get(`${item.name}/${s.thread_id}`);
    if (slot === undefined) hidden++;
    else drawn.push({ ...s, color: THREAD_COLORS[slot], label: s.thread_name || s.thread_id, owner: s.owner || _t("（不明）") });
  }
  drawn.sort((a, b) => THREAD_COLORS.indexOf(a.color) - THREAD_COLORS.indexOf(b.color));
  // 生成中は数秒ごとに点が増えて描き直すため、ホバー位置を box に覚えておき、描き直し後も同じ位置で出し直す。
  box._hover = box._hover || { clientX: null };
  const chart = contextMiniChart(drawn, box._hover);
  box.replaceChildren(
    ...[
      el("div", { class: "card-context-title", text: _t("LLMリクエストごとの入力トークン") }),
      chart,
      hidden ? el("div", { class: "hint", text: _t(`ほか {0} スレッドは色の上限のため非表示`, hidden) }) : null,
    ].filter(Boolean) // replaceChildren は null を "null" という文字列として挿入するため除く
  );
  // 要素を差し替えた瞬間はポインタが外れても pointerleave が届かないことがあるため、:hover でも確かめる。
  if (box._hover.clientX !== null && box.matches(":hover")) chart._showAtClientX(box._hover.clientX);
  else box._hover.clientX = null;
}

// ツールチップに出すスレッド名（＝多くは最初の指示文）とユーザー名の最大文字数。カードの幅からはみ出さないように。
const CONTEXT_TOOLTIP_LABEL_MAX = 20;
const CONTEXT_TOOLTIP_OWNER_MAX = 16;

function truncateText(text, max) {
  const chars = [...(text || "")];
  return chars.length > max ? `${chars.slice(0, max).join("")}…` : chars.join("");
}

// x = 時刻（スレッドごとに呼び出し時刻が異なるため）、y = 入力トークン。
// 時刻はどちらもサーバーのローカル時刻（タイムゾーンなし）で届くため、ブラウザの
// タイムゾーンによらずサーバー側の時刻のまま表示される（アプリログの時刻と一致する）。
function contextMiniChart(series, hover) {
  const W = 300, H = 130, M = { top: 8, right: 10, bottom: 20, left: 38 };
  const iw = W - M.left - M.right, ih = H - M.top - M.bottom;
  const toMs = (ts) => new Date(ts).getTime();
  for (const s of series) s.pts = s.points.map((p) => ({ ...p, t: toMs(p.ts) })).filter((p) => Number.isFinite(p.t));
  const all = series.flatMap((s) => s.pts);
  // 点が多くても Math.min(...配列) の引数上限に当たらないよう reduce で求める。
  const times = [...series.map((s) => toMs(s.started_at)), ...all.map((p) => p.t)].filter(Number.isFinite);
  const tMin = times.length ? times.reduce((a, b) => Math.min(a, b)) : Date.now();
  const tMax = Math.max(tMin + 1000, times.reduce((a, b) => Math.max(a, b), tMin));
  const ticks = niceTicks(all.reduce((m, p) => Math.max(m, p.value), 1), 3);
  const top = ticks[ticks.length - 1];
  const x = (t) => M.left + ((t - tMin) / (tMax - tMin)) * iw;
  const y = (v) => M.top + ih - (v / top) * ih;
  // 短い時間幅では分までだと両端が同じ表示になるため秒まで出す。
  const timeOpts = tMax - tMin < 10 * 60 * 1000 ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" };
  const axisTime = (t) => new Date(t).toLocaleTimeString(I18N_LOCALE, timeOpts);

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", "aria-label": _t("生成中スレッドのLLMリクエストごとの入力トークン") });
  for (const t of ticks) {
    svg.append(svgEl("line", { x1: M.left, x2: W - M.right, y1: y(t), y2: y(t), class: t === 0 ? "chart-baseline" : "chart-grid" }));
    const label = svgEl("text", { x: M.left - 6, y: y(t) + 4, "text-anchor": "end", class: "chart-axis-text card-chart-text" });
    label.textContent = fmtCompact(t);
    svg.append(label);
  }
  for (const [t, anchor] of [[tMin, "start"], [tMax, "end"]]) {
    const label = svgEl("text", { x: x(t), y: H - 5, "text-anchor": anchor, class: "chart-axis-text card-chart-text" });
    label.textContent = axisTime(t);
    svg.append(label);
  }
  for (const s of series) {
    if (s.pts.length > 1) {
      // 階段状に描く（次のリクエストまでコンテキスト長は変わらない。ツールチップの値とも一致させる）。
      const d = s.pts
        .map((p, k) => `${k ? `H${x(p.t).toFixed(1)}V` : `M${x(p.t).toFixed(1)},`}${y(p.value).toFixed(1)}`)
        .join("");
      svg.append(svgEl("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    // 現在値（最後の点）だけ強調する。
    const last = s.pts[s.pts.length - 1];
    if (last) svg.append(svgEl("circle", { cx: x(last.t), cy: y(last.value), r: 4, fill: s.color, stroke: "var(--panel)", "stroke-width": 2 }));
  }
  const cross = svgEl("line", { y1: M.top, y2: M.top + ih, class: "chart-crosshair", visibility: "hidden" });
  svg.append(cross);
  const hit = svgEl("rect", { x: M.left, y: M.top, width: iw, height: ih, fill: "transparent" });
  svg.append(hit);

  const tooltip = el("div", { class: "chart-tooltip hidden" });
  const box = el("div", { class: "chart-box" }, [svg, tooltip]);
  const showAtClientX = (clientX) => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const sx = ((clientX - rect.left) / rect.width) * W;
    const t = tMin + (Math.max(0, Math.min(iw, sx - M.left)) / iw) * (tMax - tMin);
    cross.setAttribute("x1", x(t));
    cross.setAttribute("x2", x(t));
    cross.setAttribute("visibility", "visible");
    const rows = [el("div", { class: "hint", text: new Date(t).toLocaleTimeString(I18N_LOCALE) })];
    for (const s of series) {
      // その時刻までの最後のリクエストの値（まだ呼び出しが無ければ出さない）。
      const p = s.pts.filter((q) => q.t <= t).pop();
      if (!p) continue;
      rows.push(
        el("div", { class: "tt-row" }, [
          el("span", { class: "tt-key", style: `background:${s.color}` }),
          el("strong", { text: fmtNum(p.value) }),
          el("span", { text: truncateText(s.owner, CONTEXT_TOOLTIP_OWNER_MAX) }),
          el("span", { class: "hint", text: truncateText(s.label, CONTEXT_TOOLTIP_LABEL_MAX) }),
        ])
      );
    }
    tooltip.replaceChildren(...rows);
    tooltip.classList.remove("hidden");
    const px = (x(t) / W) * rect.width;
    tooltip.style.left = `${px + 12 + tooltip.offsetWidth > rect.width ? Math.max(0, px - 12 - tooltip.offsetWidth) : px + 12}px`;
    tooltip.style.top = "4px";
  };
  hit.addEventListener("pointermove", (ev) => {
    hover.clientX = ev.clientX;
    showAtClientX(ev.clientX);
  });
  hit.addEventListener("pointerleave", () => {
    hover.clientX = null;
    cross.setAttribute("visibility", "hidden");
    tooltip.classList.add("hidden");
  });

  const legend = el(
    "div",
    { class: "chart-legend card-context-legend" },
    series.map((s) =>
      el("span", { class: "legend-item", title: _t(`{0}: {1}\n現在 {2} トークン`, s.owner, s.label, fmtNum(s.pts.length ? s.pts[s.pts.length - 1].value : null)) }, [
        el("span", { class: "legend-line", style: `background:${s.color}` }),
        el("span", { class: "legend-text", text: s.label }),
      ])
    )
  );
  const empty = all.length
    ? null
    : el("div", { class: "hint", text: _t("LLMリクエストの記録がまだありません（[log] level が INFO より上、または [llm] track_token_usage=false の場合は記録されません）") });
  const wrap = el("div", {}, [legend, box, empty]);
  wrap._showAtClientX = showAtClientX;
  return wrap;
}

function cardStat(label, value, sub) {
  return el("div", { class: "card-stat", title: sub || "" }, [
    el("div", { class: "card-stat-value", text: value }),
    el("div", { class: "card-stat-label", text: label }),
    sub ? el("div", { class: "card-stat-sub", text: sub }) : null,
  ]);
}

async function runInstanceAction(name, action, card) {
  // 一覧の再取得に失敗した場合に処理中表示のまま固まらないよう、元の表示を控えておく。
  let restoreCard = () => {};
  if (card) {
    const dot = card.querySelector(".state-dot");
    const label = card.querySelector(".state-label");
    const buttons = $$("button", card);
    const prevLabel = label.textContent;
    const prevDisabled = buttons.map((btn) => btn.disabled);
    restoreCard = () => {
      dot.classList.remove("processing");
      label.textContent = prevLabel;
      buttons.forEach((btn, i) => (btn.disabled = prevDisabled[i]));
    };
    dot.classList.add("processing");
    label.textContent = PROCESSING_LABELS[action] || label.textContent;
    for (const btn of buttons) btn.disabled = true;
  }
  try {
    await api(`/api/instances/${encodeURIComponent(name)}/${action}`, { method: "POST" });
  } catch (e) {
    alert(_t(`操作に失敗しました: {0}`, e.message));
  }
  try {
    await refreshInstanceCards();
  } catch (e) {
    restoreCard();
    alert(_t(`インスタンス一覧の更新に失敗しました: {0}`, e.message));
  }
}

function openCreateInstanceModal() {
  const modal = clone("tpl-create-instance").firstElementChild;
  document.body.appendChild(modal);
  const select = modal.querySelector("select[name=copy_from]");
  for (const inst of instanceCache) {
    const opt = document.createElement("option");
    opt.value = inst.name;
    opt.textContent = inst.display_name;
    select.appendChild(opt);
  }
  // スキル調整ワーカーはチャット画面・ポートを持たないため、本体用の項目を隠す。
  const kindSelect = modal.querySelector("select[name=kind]");
  const syncKind = () => {
    const isLab = kindSelect.value === "skill_lab";
    $$(".app-only", modal).forEach((node) => node.classList.toggle("hidden", isLab));
    $$(".lab-only", modal).forEach((node) => node.classList.toggle("hidden", !isLab));
  };
  kindSelect.addEventListener("change", syncKind);
  syncKind();
  modal.querySelector(".btn-cancel").addEventListener("click", () => modal.remove());
  modal.querySelector("form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = new FormData(ev.target);
    const body = {
      name: form.get("name"),
      display_name: form.get("display_name") || null,
      app_host: form.get("app_host") || "127.0.0.1",
      app_port: form.get("app_port") ? Number(form.get("app_port")) : null,
      autostart: form.get("autostart") === "on",
      headless: form.get("headless") === "on",
      watch: form.get("watch") === "on",
      copy_from: form.get("copy_from") || null,
      kind: form.get("kind") || "app",
      copy_extensions: form.get("copy_extensions") === "on",
    };
    try {
      await api("/api/instances", { method: "POST", body });
      modal.remove();
      await refreshInstanceCards();
    } catch (e) {
      modal.querySelector("#create-instance-error").textContent = e.message;
    }
  });
}

function openEditInstanceModal(inst) {
  const modal = clone("tpl-edit-instance").firstElementChild;
  document.body.appendChild(modal);
  modal.querySelector(".edit-instance-name").textContent = inst.name;
  const form = modal.querySelector("form");
  form.elements.display_name.value = inst.display_name;
  form.elements.app_host.value = inst.app_host;
  form.elements.app_port.value = inst.app_port;
  form.elements.autostart.checked = inst.autostart;
  form.elements.headless.checked = inst.headless;
  form.elements.watch.checked = inst.watch;
  // スキル調整ワーカーはチャット画面・ポートを持たないため、本体用の項目を隠して送らない（API は null なら現状維持）。
  const isLab = inst.kind === "skill_lab";
  $$(".app-only", modal).forEach((node) => node.classList.toggle("hidden", isLab));
  form.elements.app_port.required = !isLab;

  modal.querySelector(".btn-cancel").addEventListener("click", () => modal.remove());
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const data = new FormData(ev.target);
    const body = {
      display_name: data.get("display_name") || null,
      app_host: isLab ? null : data.get("app_host") || null,
      app_port: !isLab && data.get("app_port") ? Number(data.get("app_port")) : null,
      autostart: data.get("autostart") === "on",
      headless: isLab ? null : data.get("headless") === "on",
      watch: isLab ? null : data.get("watch") === "on",
    };
    try {
      const portOrHostChanged = !isLab && body.app_host !== inst.app_host || body.app_port !== inst.app_port;
      await api(`/api/instances/${encodeURIComponent(inst.name)}`, { method: "PUT", body });
      modal.remove();
      await refreshInstanceCards();
      if (portOrHostChanged && (inst.state === "running")) {
        alert(_t("ホスト・ポートを変更しました。反映するにはインスタンスを再起動してください。"));
      }
    } catch (e) {
      modal.querySelector("#edit-instance-error").textContent = e.message;
    }
  });
}

async function openDeleteInstanceModal(inst) {
  let entries, extensions;
  try {
    const data = await api(`/api/instances/${encodeURIComponent(inst.name)}/data-paths`);
    entries = data.entries;
    extensions = data.extensions || {};
  } catch (e) {
    alert(_t("データの保存先を取得できませんでした: ") + e.message);
    return;
  }
  const modal = clone("tpl-delete-instance").firstElementChild;
  document.body.appendChild(modal);
  modal.querySelector(".delete-instance-name").textContent = inst.name;
  // インスタンス専用のスキル・サブエージェント（スキル研究室で昇格したもの）が一緒に消える場合は警告する。
  const extNames = [...(extensions.skills || []).map((n) => _t(`スキル {0}`, n)), ...(extensions.agents || []).map((n) => _t(`サブエージェント {0}`, n))];
  if (extensions.deleted_with_instance && extNames.length) {
    modal.querySelector(".extensions-warning").replaceChildren(
      el("strong", { text: _t(`このインスタンス専用のスキル・サブエージェント {0} 件も一緒に削除されます: `, extNames.length) }),
      extNames.join(_t("、")),
      el("br"),
      el("code", { text: extensions.dir }),
    );
    modal.querySelector(".extensions-warning").classList.remove("hidden");
  }
  const list = modal.querySelector(".data-path-list");
  for (const entry of entries) {
    const row = document.createElement("label");
    row.className = "checkbox data-path-row" + (entry.deletable ? "" : " disabled");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.name = "delete_data";
    cb.value = entry.key;
    cb.disabled = !entry.deletable;
    cb.checked = entry.deletable && entry.default_selected;
    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = entry.label;
    const path = document.createElement("code");
    path.textContent = entry.path;
    text.append(title, document.createElement("br"), path);
    if (entry.reason) {
      const reason = document.createElement("span");
      reason.className = "hint";
      reason.textContent = _t(`（{0}）`, entry.reason);
      text.append(" ", reason);
    }
    row.append(cb, text);
    list.appendChild(row);
  }

  // common_data_dir を選ぶと配下の項目もまとめて消えるため、配下の項目を連動させる。
  const common = entries.find((e) => e.key === "common_data_dir");
  const commonCb = list.querySelector('input[value="common_data_dir"]');
  const isUnderCommon = (entry) => {
    if (!common || entry.key === "common_data_dir") return false;
    const base = common.path.replace(/[\\/]+$/, "").toLowerCase();
    const p = entry.path.toLowerCase();
    return p.startsWith(base + "\\") || p.startsWith(base + "/");
  };
  const syncCommon = () => {
    for (const entry of entries) {
      if (!isUnderCommon(entry)) continue;
      const cb = list.querySelector(`input[value="${entry.key}"]`);
      if (commonCb.checked) {
        cb.checked = entry.exists;
        cb.disabled = true;
      } else {
        cb.checked = entry.deletable && entry.default_selected;
        cb.disabled = !entry.deletable;
      }
    }
  };
  if (commonCb && !commonCb.disabled) {
    commonCb.addEventListener("change", syncCommon);
    syncCommon();
  }

  modal.querySelector(".btn-cancel").addEventListener("click", () => modal.remove());
  modal.querySelector("form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const errEl = modal.querySelector("#delete-instance-error");
    if (ev.target.elements.confirm_name.value !== inst.name) {
      errEl.textContent = _t("インスタンス名が一致しません。");
      return;
    }
    // disabled のチェックボックス（common_data_dir 配下の連動分）は送らない。
    const deleteData = $$('input[name="delete_data"]', list)
      .filter((cb) => cb.checked && !cb.disabled)
      .map((cb) => cb.value);
    try {
      await api(`/api/instances/${encodeURIComponent(inst.name)}`, {
        method: "DELETE",
        body: { delete_data: deleteData },
      });
      modal.remove();
      await refreshInstanceCards();
    } catch (e) {
      errEl.textContent = _t("削除に失敗しました: ") + e.message;
    }
  });
}

/* ------------------------------------------------------- インスタンス詳細 */

let currentInstanceName = null;

async function renderInstanceDetail(name, initialView) {
  stopMonitorPolling();
  currentInstanceName = name;
  mainEl.innerHTML = "";
  mainEl.appendChild(clone("tpl-instance-detail"));
  $(".detail-name").textContent = name;
  $(".back-btn").addEventListener("click", () => {
    stopMonitorPolling();
    renderInstances();
  });
  $$(".subtab-btn").forEach((btn) => {
    btn.addEventListener("click", () => selectInstanceSubview(btn.dataset.subview));
  });
  // スキル調整ワーカーはチャットを持たないため、会話・ユーザー・表示設定・研究テーマの画面を出さない。
  const meta = instanceCache.find((i) => i.name === name);
  const isLab = meta && meta.kind === "skill_lab";
  $$(".subtab-btn").forEach((btn) => {
    const hide = isLab ? ["monitor", "threads", "users", "display", "skill-lab", "knowledge"].includes(btn.dataset.subview) : false;
    btn.classList.toggle("hidden", hide);
  });
  await selectInstanceSubview(initialView || "config");
}

function selectInstanceSubview(view, opts) {
  $$(".subtab-btn").forEach((b) => b.classList.toggle("active", b.dataset.subview === view));
  return renderInstanceSubview(view, opts);
}

function renderInstanceSubview(view, opts) {
  stopMonitorPolling();
  const container = $("#instance-subview");
  container.innerHTML = "";
  if (view === "monitor") return renderMonitor(container);
  if (view === "threads") return renderThreads(container, opts || {});
  if (view === "logs") return renderLogs(container, opts || {});
  if (view === "config") return renderConfigEditor(container);
  if (view === "users") return renderUsers(container);
  if (view === "env") return renderEnvVars(container);
  if (view === "display") return renderSettingsPanel(container, currentInstanceName);
  if (view === "backups") return renderBackups(container);
  if (view === "skill-lab") return renderSkillLabPanel(container, currentInstanceName);
  if (view === "knowledge") return renderKnowledgePanel(container, currentInstanceName);
}

/* --- config.ini 編集 --- */

let configKeys = [];
let configMtime = null;
let dirtyEdits = {}; // "section.key" -> 新しい値の文字列
let resetKeys = new Set(); // "section.key" 既定値へ戻すよう指定

async function renderConfigEditor(container) {
  container.appendChild(clone("tpl-config-editor"));
  dirtyEdits = {};
  resetKeys = new Set();
  await Promise.all([loadConfigKeys(), loadConfigCandidates()]);
  buildPresetList();
  buildSectionList();
  renderKeyList();
  updateFooter();

  $("#config-search").addEventListener("input", renderKeyList);
  $("#config-changed-only").addEventListener("change", renderKeyList);
  $("#preview-btn").addEventListener("click", openPreviewModal);
  $("#save-btn").addEventListener("click", () => doSave(null));
}

async function loadConfigKeys() {
  const data = await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/config`);
  configKeys = data.keys;
  configMtime = data.mtime;
  const note = $("#config-scope-note");
  if (note) {
    note.textContent = data.scope_note || "";
    note.classList.toggle("hidden", !data.scope_note);
  }
}

// スキル名・スクリプト名・agent_type を選ぶ欄の候補（admin/ini_catalog.py の "source"）。
// 保存済みの設定でこのインスタンスが認識しているもの（[paths] project_locohane_dir 等を走査）。
let configCandidates = null;

async function loadConfigCandidates() {
  try {
    const data = await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/config/candidates`);
    configCandidates = data.error ? null : data;
  } catch (e) {
    configCandidates = null;
  }
}

let activeSection = null;

// よく変更するキーをまとめて表示するプリセット（表示順もこの並び）
const CONFIG_PRESETS = [
  {
    name: _t("並列数"),
    keys: ["llm.max_concurrent_requests", "graph.max_parallel", "subagent.max_parallel"],
  },
  {
    name: _t("トークン制御"),
    tokenRatio: true,
    keys: [
      "graph.token_guard_soft_threshold",
      "subagent.token_guard_soft_threshold",
      "subagent.token_guard_hard_threshold",
      "context_trim.enabled",
      "context_trim.trigger_total_tokens",
      "context_compaction.enabled",
      "context_compaction.token_threshold",
      "context_compaction.single_request_token_threshold",
      "context_compaction.pre_note_threshold",
      "token_ratio.enabled",
      "token_ratio.subagent.token_guard_soft_threshold",
      "token_ratio.subagent.token_guard_hard_threshold",
      "token_ratio.context_trim.trigger_total_tokens",
      "token_ratio.context_compaction.single_request_token_threshold",
      "token_ratio.context_compaction.pre_note_threshold",
    ],
  },
  {
    name: _t("スキル関係"),
    keys: [
      "paths.project_locohane_dir",
      "subagent.agent_type_run_script_allowlist",
      "main_agent_tool_guard.allow_entries",
      "plan.plan_approval_exempt_scripts",
      "skill_creator.draft_dir",
      "skill_creator.tryout_repeats",
      "skill_creator.other_users_drafts",
      "skill_creator.archive_dir",
      "skill_lab.instance_locohane_dir",
    ],
  },
  {
    name: _t("ベクターDB"),
    keys: [
      "vector_db.enabled",
      "vector_db.backend",
      "vector_db.qdrant_url",
      "vector_db.qdrant_preset",
      "vector_db.distance",
      "vector_db.embedding_url",
      "vector_db.embedding_routing_strategy",
      "vector_db.embedding_max_retries",
      "vector_db.search_embedding_max_retries",
      "vector_db.embedding_retry_interval_seconds",
      "vector_db.embedding_encoding_format",
      "vector_db.query_prefix",
      "vector_db.document_prefix",
      "vector_db.image_input",
      "vector_db.image_input_extensions",
      "vector_db.embedded_images",
      "vector_db.embedded_image_min_px",
      "vector_db.image_dpi",
      "vector_db.office_timeout_seconds",
      "vector_db.ingest_temp_dir",
      "vector_db.ingest_workers",
      "vector_db.ingest_processes",
      "vector_db.ingest_embed_requests",
      "vector_db.scan_workers",
      "vector_db.max_files_per_collection",
      "vector_db.chunk_size",
      "vector_db.chunk_overlap",
      "vector_db.max_file_mb",
      "vector_db.top_k",
      "vector_db.max_result_chars",
      "vector_db.min_score",
      "vector_db.image_min_score",
      "vector_db.path_min_score",
      "vector_db.rrf_weight_text",
      "vector_db.rrf_weight_keyword",
      "vector_db.rrf_weight_image",
      "vector_db.rrf_weight_path_keyword",
    ],
  },
];
let activePreset = null;

function buildPresetList() {
  const box = $("#preset-list");
  box.innerHTML = "";
  activePreset = null;
  // その種類のインスタンスに出していないキーだけのプリセット（スキル調整ワーカーでの「並列数」等）は出さない。
  // ワーカーは出すキーが少ないため、プリセットを使わずセクション一覧から選ぶ。
  const available = new Set(configKeys.map(keyId));
  for (const preset of CONFIG_PRESETS) {
    if (!preset.keys.some((id) => available.has(id))) continue;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = preset.name;
    btn.addEventListener("click", () => {
      activePreset = activePreset === preset ? null : preset;
      $$("button", box).forEach((b) => b.classList.toggle("active", b === btn && activePreset !== null));
      if (activePreset) selectSection("");
      renderKeyList();
    });
    box.appendChild(btn);
  }
}

function clearPreset() {
  activePreset = null;
  $$("#preset-list button").forEach((b) => b.classList.remove("active"));
}

function selectSection(sec) {
  activeSection = sec;
  $$("#section-list li").forEach((li) => li.classList.toggle("active", li.dataset.section === sec));
}

function buildSectionList() {
  const sections = [];
  const seen = new Set();
  for (const k of configKeys) {
    if (!seen.has(k.section)) {
      seen.add(k.section);
      sections.push(k.section);
    }
  }
  const list = $("#section-list");
  list.innerHTML = "";
  const allLi = document.createElement("li");
  allLi.textContent = _t("すべて");
  allLi.dataset.section = "";
  allLi.classList.add("active");
  list.appendChild(allLi);
  for (const sec of sections) {
    const li = document.createElement("li");
    const count = configKeys.filter((k) => k.section === sec).length;
    li.innerHTML = `[${sec}] <span class="count">${count}</span>`;
    li.dataset.section = sec;
    list.appendChild(li);
  }
  activeSection = "";
  $$("li", list).forEach((li) => {
    li.addEventListener("click", () => {
      clearPreset();
      selectSection(li.dataset.section);
      renderKeyList();
    });
  });
}

function keyId(k) {
  return `${k.section}.${k.key}`;
}

function effectiveValue(k) {
  const id = keyId(k);
  if (id in dirtyEdits) return dirtyEdits[id];
  if (resetKeys.has(id)) return k.default;
  return k.effective;
}

function isDirty(k) {
  const id = keyId(k);
  return id in dirtyEdits || resetKeys.has(id);
}

function renderKeyList() {
  const query = $("#config-search").value.trim().toLowerCase();
  const changedOnly = $("#config-changed-only").checked;
  const listEl = $("#config-keys");
  listEl.innerHTML = "";
  let shown = 0;
  if (activePreset && activePreset.tokenRatio && tokenRatioPairs().length > 0) {
    listEl.appendChild(buildTokenRatioBanner());
  }
  const keys = activePreset
    ? activePreset.keys.map((id) => configKeys.find((k) => keyId(k) === id)).filter(Boolean)
    : configKeys;
  for (const k of keys) {
    if (activeSection && k.section !== activeSection) continue;
    if (query && !(`${k.section}.${k.key}`.toLowerCase().includes(query) || k.description.toLowerCase().includes(query))) continue;
    if (changedOnly && !isDirty(k) && k.override === null) continue;
    listEl.appendChild(buildKeyRow(k));
    shown++;
  }
  if (shown === 0) {
    listEl.innerHTML = _t('<p class="hint">該当するキーがありません。</p>');
  }
}

/* --- トークン制御の相対比連動（config.ini の [token_ratio] 参照） --- */

const TOKEN_RATIO_SECTION = "token_ratio";
const TOKEN_RATIO_BASE_ID = "graph.token_guard_soft_threshold";

function findConfigKey(id) {
  return configKeys.find((k) => keyId(k) === id);
}

function positiveNumber(text) {
  const s = (text || "").trim();
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) && n > 0 ? n : null;
}

// [token_ratio] の enabled 以外のキー（キー名が自動入力先の "section.key"）と、その自動入力先の組
function tokenRatioPairs() {
  return configKeys
    .filter((k) => k.section === TOKEN_RATIO_SECTION && k.key !== "enabled")
    .map((ratioKey) => ({ ratioKey, target: findConfigKey(ratioKey.key) }))
    .filter((p) => p.target);
}

function tokenRatioEnabled() {
  const k = findConfigKey(`${TOKEN_RATIO_SECTION}.enabled`);
  return !!k && effectiveValue(k).trim().toLowerCase() === "true";
}

// 基準値×比率（整数に四捨五入）を自動入力先へ入れる。値が変わったキーの id を返す。
// 基準値・比率が空欄や正の数でない組は飛ばす。
function applyTokenRatios(pairs) {
  const baseKey = findConfigKey(TOKEN_RATIO_BASE_ID);
  const base = baseKey ? positiveNumber(effectiveValue(baseKey)) : null;
  if (base === null) return [];
  const changed = [];
  for (const { ratioKey, target } of pairs) {
    const ratio = positiveNumber(effectiveValue(ratioKey));
    if (ratio === null) continue;
    const value = String(Math.round(base * ratio));
    if (effectiveValue(target).trim() === value) continue;
    storeEdit(target, value);
    changed.push(keyId(target));
  }
  return changed;
}

// k の変更に連動して自動入力し、値が変わったキーの id を返す（enabled=false なら何もしない）
function applyLinkedEdits(k) {
  if (!tokenRatioEnabled()) return [];
  const id = keyId(k);
  if (id === TOKEN_RATIO_BASE_ID || id === `${TOKEN_RATIO_SECTION}.enabled`) return applyTokenRatios(tokenRatioPairs());
  if (k.section === TOKEN_RATIO_SECTION) return applyTokenRatios(tokenRatioPairs().filter((p) => p.ratioKey === k));
  return [];
}

// 入力中の欄（フォーカス）を壊さないよう、一覧全体ではなく自動入力した行だけを作り直す
function refreshKeyRows(ids) {
  for (const id of ids) {
    const old = $(`#config-keys [data-key-id="${CSS.escape(id)}"]`);
    if (old) old.replaceWith(buildKeyRow(findConfigKey(id)));
  }
}

function buildTokenRatioBanner() {
  const box = document.createElement("div");
  box.className = "token-ratio-banner";
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = tokenRatioEnabled()
    ? _t("[graph] token_guard_soft_threshold（基準値）を変更すると、[token_ratio] の比率×基準値が各閾値へ自動入力されます。")
    : _t("[token_ratio] enabled が false のため、基準値を変更しても各閾値は連動しません。");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = _t("比率で再計算");
  btn.addEventListener("click", () => {
    const baseKey = findConfigKey(TOKEN_RATIO_BASE_ID);
    if (!baseKey || positiveNumber(effectiveValue(baseKey)) === null) {
      alert(_t("[graph] token_guard_soft_threshold に正の数を入力してください。"));
      return;
    }
    applyTokenRatios(tokenRatioPairs());
    renderKeyList();
    updateFooter();
  });
  box.append(note, btn);
  return box;
}

function buildKeyRow(k) {
  const row = clone("tpl-key-row").firstElementChild;
  const id = keyId(k);
  row.dataset.keyId = id;
  row.querySelector(".key-label").textContent = `[${k.section}] ${k.key}`;
  row.querySelector(".key-desc").textContent = k.description;
  row.querySelector(".env-badge").classList.toggle("hidden", !k.env_override_active);
  if (k.env_override_active) {
    row.querySelector(".env-badge").textContent = _t(`環境変数 {0} で上書き中`, k.env_name);
  }
  const changedBadge = row.querySelector(".changed-badge");
  const resetBtn = row.querySelector(".reset-btn");
  const inputWrap = row.querySelector(".key-input-wrap");

  const currentlyOverridden = k.override !== null || id in dirtyEdits;
  changedBadge.classList.toggle("hidden", !(isDirty(k) || (k.override !== null && !resetKeys.has(id))));
  resetBtn.classList.toggle("hidden", !currentlyOverridden || resetKeys.has(id));

  const value = effectiveValue(k);

  if (k.ignored) {
    // 全インスタンス共通の設定になったキーに残っている、インスタンス別の古い上書き（admin/server.py の _config_key_view）。
    // 効かない値のため編集はさせず、確認と「既定値に戻す」（上書きから消す）だけをできるようにする。
    row.querySelector(".key-desc").textContent =
      _t("このキーは全インスタンス共通の設定になったため、ここに残っている値は効きません。「管理者設定」画面で設定し、この値は「既定値に戻す」で消してください。") +
      "\n" + k.description;
    inputWrap.appendChild(el("code", { text: resetKeys.has(id) ? _t("（保存すると消します）") : k.override }));
  } else if (k.ui_kind === "bool") {
    const wrap = document.createElement("label");
    wrap.className = "checkbox";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = value.trim().toLowerCase() === "true";
    input.addEventListener("change", () => setEdit(k, input.checked ? "true" : "false"));
    wrap.appendChild(input);
    wrap.appendChild(document.createTextNode(_t(" 有効にする")));
    inputWrap.appendChild(wrap);
  } else if (k.ui_kind === "choice") {
    const select = buildChoiceSelect(k.choices, value.trim(), (v) => setEdit(k, v));
    select.classList.add("choice-select");
    inputWrap.appendChild(select);
  } else if (k.ui_kind === "list") {
    buildListInput(k, value, inputWrap);
  } else if (k.ui_kind === "multiline") {
    inputWrap.appendChild(buildRawTextarea(k, value));
  } else {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value;
    input.addEventListener("input", () => setEdit(k, input.value));
    inputWrap.appendChild(input);
  }

  resetBtn.addEventListener("click", () => {
    delete dirtyEdits[id];
    resetKeys.add(id);
    applyLinkedEdits(k);
    renderKeyList();
    updateFooter();
  });

  return row;
}

function buildRawTextarea(k, value) {
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.rows = Math.min(16, Math.max(3, value.split("\n").length));
  textarea.addEventListener("input", () => setEdit(k, textarea.value));
  return textarea;
}

// 固有キーワードの選択肢から選ぶ select。現在値が選択肢に無い場合（手動編集等）は
// その値も選択肢に残し、意図せず別の値へ書き換わらないようにする。
function buildChoiceSelect(choices, current, onChange) {
  const select = document.createElement("select");
  const matched = choices.find((c) => c === current) ?? choices.find((c) => c.toLowerCase() === current.toLowerCase());
  const options = matched === undefined ? [current, ...choices] : choices;
  for (const c of options) {
    const opt = document.createElement("option");
    opt.value = c;
    if (c === "") opt.textContent = _t("（未指定）");
    else if (matched === undefined && c === current) opt.textContent = _t(`{0}（選択肢外の現在値）`, c);
    else opt.textContent = c;
    select.appendChild(opt);
  }
  select.value = matched === undefined ? current : matched;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

/* --- リスト値（[...]）の項目ごと編集 ---
 * config.ini のリスト値は src/config.py で ast.literal_eval される Python リテラル
 * なので、ここでもその部分集合（文字列・数値・True/False/None・リスト/タプル・
 * 辞書・# コメント・末尾カンマ）をパース/シリアライズする。パースできない値は
 * 従来どおりテキストで編集する。 */

function parsePyLiteral(text) {
  const s = text;
  let i = 0;
  const fail = (msg) => {
    throw new Error(_t(`{0}（{1}文字目付近）`, msg, i + 1));
  };
  const skip = () => {
    for (;;) {
      while (i < s.length && /\s/.test(s[i])) i++;
      if (s[i] !== "#") return;
      while (i < s.length && s[i] !== "\n") i++;
    }
  };
  const ESC = { n: "\n", t: "\t", r: "\r", "\\": "\\", "'": "'", '"': '"', 0: "\0", a: "\x07", b: "\b", f: "\f", v: "\v" };
  const parseStr = () => {
    const q = s[i++];
    let out = "";
    while (i < s.length && s[i] !== q) {
      let c = s[i++];
      if (c === "\n") fail(_t("文字列の途中で改行されています"));
      if (c !== "\\") {
        out += c;
        continue;
      }
      c = s[i++];
      if (c === "\n") continue;
      if (c === "x" || c === "u" || c === "U") {
        const len = c === "x" ? 2 : c === "u" ? 4 : 8;
        const hex = s.slice(i, i + len);
        if (hex.length !== len || !/^[0-9a-fA-F]+$/.test(hex)) fail(_t("不正なエスケープです"));
        out += String.fromCodePoint(parseInt(hex, 16));
        i += len;
      } else if (c in ESC) {
        out += ESC[c];
      } else {
        out += "\\" + c;
      }
    }
    if (s[i] !== q) fail(_t("文字列が閉じていません"));
    i++;
    return out;
  };
  const parseValue = () => {
    skip();
    const c = s[i];
    if (c === "[" || c === "(") {
      const close = c === "[" ? "]" : ")";
      i++;
      const arr = [];
      for (;;) {
        skip();
        if (s[i] === close) break;
        arr.push(parseValue());
        skip();
        if (s[i] === ",") i++;
        else if (s[i] !== close) fail(_t(`"," か "{0}" が必要です`, close));
      }
      i++;
      return arr;
    }
    if (c === "{") {
      i++;
      const obj = {};
      for (;;) {
        skip();
        if (s[i] === "}") break;
        const key = parseValue();
        if (typeof key !== "string") fail(_t("辞書のキーは文字列にしてください"));
        skip();
        if (s[i] !== ":") fail(_t('":" が必要です'));
        i++;
        obj[key] = parseValue();
        skip();
        if (s[i] === ",") i++;
        else if (s[i] !== "}") fail(_t('"," か "}" が必要です'));
      }
      i++;
      return obj;
    }
    if (c === '"' || c === "'") {
      let out = parseStr();
      // Python の暗黙の文字列連結（"a" "b"）
      for (;;) {
        const save = i;
        skip();
        if (s[i] === '"' || s[i] === "'") {
          out += parseStr();
        } else {
          i = save;
          return out;
        }
      }
    }
    const m = /^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?/.exec(s.slice(i));
    if (m) {
      i += m[0].length;
      return Number(m[0]);
    }
    for (const [word, val] of [["True", true], ["False", false], ["None", null]]) {
      if (s.startsWith(word, i) && !/\w/.test(s[i + word.length] || "")) {
        i += word.length;
        return val;
      }
    }
    fail(_t("解釈できない値です"));
  };
  const result = parseValue();
  skip();
  if (i < s.length) fail(_t("余分な文字があります"));
  return result;
}

function toPyLiteral(v) {
  if (v === null) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (typeof v === "number") return String(v);
  if (typeof v === "string") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(toPyLiteral).join(", ") + "]";
  return "{" + Object.entries(v).map(([key, x]) => `${JSON.stringify(key)}: ${toPyLiteral(x)}`).join(", ") + "}";
}

// config.ini と同じ「1要素1行・末尾カンマ・閉じ括弧はインデント付き」の形で書き出す。
// 空リストは空文字にする。[llm].sub_url のように「値が空なら未指定扱い」のキーは
// "[]" だと空リストの検証エラーになり保存できないため（空文字はどのリスト値でも
// 空リストと同じに解釈される）。
function serializePyList(arr) {
  if (arr.length === 0) return "";
  return "[\n" + arr.map((x) => `    ${toPyLiteral(x)},\n`).join("") + "    ]";
}

function parsePyList(text) {
  const data = parsePyLiteral(text.trim() || "[]");
  if (!Array.isArray(data)) throw new Error(_t("リスト（[...]）形式ではありません"));
  return data;
}

const rawListIds = new Set();

function rerenderKeyRow(k, inputWrap) {
  inputWrap.closest(".key-row").replaceWith(buildKeyRow(k));
}

function buildListInput(k, value, inputWrap) {
  const id = keyId(k);
  const toolbar = document.createElement("div");
  toolbar.className = "le-toolbar";
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "le-btn";
  const note = document.createElement("span");
  note.className = "hint";
  toolbar.append(toggle, note);
  inputWrap.appendChild(toolbar);

  let data = null;
  let parseError = null;
  if (!rawListIds.has(id)) {
    try {
      data = parsePyList(value);
    } catch (e) {
      parseError = e.message;
    }
  }

  if (data === null) {
    toggle.textContent = _t("項目ごとに編集");
    if (parseError) {
      note.classList.replace("hint", "error");
      note.textContent = _t(`項目ごとの編集に切り替えられません: {0}`, parseError);
    }
    toggle.addEventListener("click", () => {
      try {
        parsePyList(effectiveValue(k));
      } catch (e) {
        note.classList.replace("hint", "error");
        note.textContent = _t(`項目ごとの編集に切り替えられません: {0}`, e.message);
        return;
      }
      rawListIds.delete(id);
      rerenderKeyRow(k, inputWrap);
    });
    inputWrap.appendChild(buildRawTextarea(k, value));
    return;
  }

  toggle.textContent = _t("テキストで編集");
  toggle.addEventListener("click", () => {
    rawListIds.add(id);
    rerenderKeyRow(k, inputWrap);
  });
  if (/^\s*#/m.test(value)) {
    note.textContent = _t("※ここで編集して保存すると、リスト内のコメント行は保存値に含まれません（config.ini 自体は変わりません）。");
  }
  let defaultCanon = null;
  try {
    defaultCanon = serializePyList(parsePyList(k.default));
  } catch (e) {
    // 既定値がパースできない場合は、既定値との一致判定をしない
  }
  const emit = () => {
    const text = serializePyList(data);
    // 書式だけが違う（中身は既定値と同じ）場合は「変更なし」として扱う
    setEdit(k, text === defaultCanon ? k.default : text);
  };
  if (k.schema && schemaMatches(k.schema, data)) {
    inputWrap.appendChild(
      buildSchemaEditor(data, k.schema, (v) => {
        data = v;
        emit();
      })
    );
    return;
  }
  if (k.schema) {
    note.textContent = _t("※値が想定の形式と異なるため、要素の種類を選んで編集する形式で表示しています。");
  }
  inputWrap.appendChild(buildArrayEditor(data, emit, { depth: 0 }));
}

/* --- スキーマ付きリストの編集 ---
 * admin/ini_catalog.py の _KEY_SCHEMAS で構造が決まっているキーは、型を選ばせず
 * 「スキル名」「スクリプトファイル名」のような名前付きの入力欄で編集させる。 */

function schemaDefault(schema) {
  switch (schema.type) {
    case "number":
      return schema.default ?? 0;
    case "choice":
      return schema.choices[0];
    case "list":
    case "grouped":
      return [];
    case "tuple":
      return schema.items.map((it) => schemaDefault(it.schema));
    case "dict": {
      const obj = {};
      for (const f of schema.fields) if (!f.optional) obj[f.name] = schemaDefault(f.schema);
      return obj;
    }
    case "oneof":
      return schemaDefault(schema.variants[0].schema);
    default:
      return schema.default ?? "";
  }
}

function schemaMatches(schema, v) {
  switch (schema.type) {
    case "number":
      return typeof v === "number";
    case "list":
      return Array.isArray(v) && v.every((x) => schemaMatches(schema.item, x));
    case "grouped":
      return Array.isArray(v) && v.every((x) => Array.isArray(x) && x.length === 2 && typeof x[0] === "string" && schemaMatches(schema.item, x[1]));
    case "tuple":
      return Array.isArray(v) && v.length === schema.items.length && schema.items.every((it, i) => schemaMatches(it.schema, v[i]));
    case "dict": {
      if (v === null || typeof v !== "object" || Array.isArray(v)) return false;
      const names = new Set(schema.fields.map((f) => f.name));
      // 未知のキーがあると画面に出せず保存時に消えてしまうため、汎用エディタに任せる
      if (Object.keys(v).some((key) => !names.has(key))) return false;
      return schema.fields.every((f) => (f.name in v ? schemaMatches(f.schema, v[f.name]) : f.optional));
    }
    case "oneof":
      return schema.variants.some((variant) => schemaMatches(variant.schema, v));
    default:
      // string / choice（選択肢外の値は選択肢に残して表示する）
      return typeof v === "string";
  }
}

/* --- 候補から選ぶ欄（スキーマの "source"） ---
 * スキル名・スクリプトファイル名・agent_type・ツール名を、このインスタンスが認識している
 * もの（configCandidates）から選ばせる。一覧に無い名前も「手入力」で入れられる。 */

const SOURCE_MANUAL = "\u0000manual";
const SOURCE_ORIGIN_LABELS = {
  instance: () => _t("インスタンス専用"),
  shared: () => "project_locohane_dir",
};

// source の候補 [{value, title, origin}]。候補が取れていなければ null（手入力の欄にする）。
// ctx.skill() は同じ tuple 内で選ばれているスキル名（"script" の候補の絞り込みに使う）。
function sourceOptions(source, ctx = {}) {
  if (!configCandidates) return null;
  if (source === "skill") {
    return configCandidates.skills.map((s) => ({ value: s.name, title: s.description, origin: s.origin }));
  }
  if (source === "agent_type") {
    return configCandidates.agent_types.map((a) => ({ value: a.name, title: a.description, origin: a.origin }));
  }
  if (source === "script") {
    const skill = ctx.skill ? configCandidates.skills.find((s) => s.name === ctx.skill()) : null;
    return (skill ? skill.scripts : []).map((name) => ({ value: name, title: "", origin: null }));
  }
  if (source === "tool") {
    // ツール名が取れなかったときは手入力の欄にする（MCP の動的ツールは一覧に無いので「手入力」で入れる）
    const tools = configCandidates.tools || [];
    return tools.length ? tools.map((name) => ({ value: name, title: "", origin: null })) : null;
  }
  return null;
}

// 手入力の欄は、1文字ごとに set(v, { typing: true })、入力を確定したとき（フォーカスが外れた・Enter）に set(v) を呼ぶ
// （入力途中の値で、ほかの欄を作り直さないため。buildSchemaTuple 参照）。box.isManual() は手入力の欄になっているか。
function buildSourceField(current, options, set, placeholder) {
  const box = document.createElement("span");
  box.className = "le-source";
  let value = current ?? "";
  let manual = false;
  box.isManual = () => manual;
  const render = () => {
    box.innerHTML = "";
    if (manual) {
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = placeholder || "";
      input.value = value;
      input.addEventListener("input", () => {
        value = input.value;
        set(value, { typing: true });
      });
      input.addEventListener("change", () => {
        value = input.value;
        set(value);
      });
      box.append(
        input,
        smallBtn(_t("一覧から選ぶ"), _t("一覧から選ぶ"), false, () => {
          manual = false;
          render();
        })
      );
      return input;
    }
    const select = document.createElement("select");
    const addOption = (v, text, title = "") => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = text;
      if (title) opt.title = title;
      select.appendChild(opt);
    };
    if (!options.some((o) => o.value === value)) {
      addOption(value, value === "" ? _t("（未指定）") : _t(`{0}（一覧にない現在値）`, value));
    }
    for (const o of options) {
      const originLabel = SOURCE_ORIGIN_LABELS[o.origin];
      addOption(o.value, originLabel ? `${o.value}（${originLabel()}）` : o.value, o.title);
    }
    addOption(SOURCE_MANUAL, _t("（手入力…）"));
    select.value = value;
    select.addEventListener("change", () => {
      if (select.value === SOURCE_MANUAL) {
        manual = true;
        render().focus();
        return;
      }
      value = select.value;
      set(value);
    });
    box.append(select);
    return select;
  };
  render();
  return box;
}

// set(v) は親に「この値が v に変わった」ことを伝える。リスト・タプル・辞書は
// 自分自身の中身を書き換えてから同じ参照で set する。
// ctx は "source" 付きの欄へ渡す文脈（buildSchemaTuple の ctx.skill 参照）。
function buildSchemaEditor(value, schema, set, optional = false, ctx = {}) {
  switch (schema.type) {
    case "list":
      return buildSchemaList(value, schema, set);
    case "grouped":
      return buildSchemaGrouped(value, schema, set);
    case "tuple":
      return buildSchemaTuple(value, schema, set);
    case "dict":
      return buildSchemaDict(value, schema, set);
    case "oneof":
      return buildSchemaOneOf(value, schema, set);
    case "choice":
      return buildChoiceSelect(optional ? ["", ...schema.choices] : schema.choices, value ?? "", set);
    case "number": {
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      input.placeholder = schema.placeholder || "";
      input.value = typeof value === "number" ? String(value) : "";
      input.addEventListener("input", () => {
        const text = input.value.trim();
        if (text === "" && optional) {
          input.classList.remove("invalid");
          set(undefined);
          return;
        }
        const n = Number(text);
        const ok = text !== "" && Number.isFinite(n);
        input.classList.toggle("invalid", !ok);
        if (ok) set(n);
      });
      return input;
    }
    default: {
      const options = schema.source ? sourceOptions(schema.source, ctx) : null;
      if (options) return buildSourceField(value, options, set, schema.placeholder);
      const text = value ?? "";
      const long = text.length > 60 || text.includes("\n");
      const input = document.createElement(long ? "textarea" : "input");
      if (long) input.rows = Math.min(8, Math.max(2, Math.ceil(text.length / 80)));
      else input.type = "text";
      input.placeholder = schema.placeholder || "";
      input.value = text;
      input.addEventListener("input", () => set(input.value));
      return input;
    }
  }
}

function buildSchemaList(arr, schema, set) {
  const box = document.createElement("div");
  box.className = "le-list";
  const render = () => {
    box.innerHTML = "";
    arr.forEach((item, idx) => {
      const itemEl = document.createElement("div");
      itemEl.className = "le-item";
      const body = document.createElement("div");
      body.className = "le-item-body";
      body.appendChild(
        buildSchemaEditor(item, schema.item, (v) => {
          arr[idx] = v;
          set(arr);
        })
      );
      const ctrl = document.createElement("div");
      ctrl.className = "le-item-ctrl";
      const swap = (a, b) => {
        [arr[a], arr[b]] = [arr[b], arr[a]];
        render();
        set(arr);
      };
      ctrl.append(
        smallBtn("↑", _t("上へ移動"), idx === 0, () => swap(idx - 1, idx)),
        smallBtn("↓", _t("下へ移動"), idx === arr.length - 1, () => swap(idx, idx + 1)),
        smallBtn("✕", _t("削除"), false, () => {
          arr.splice(idx, 1);
          render();
          set(arr);
        })
      );
      itemEl.append(body, ctrl);
      box.appendChild(itemEl);
    });
    const add = smallBtn(_t("＋ 項目を追加"), _t("項目を追加"), false, () => {
      arr.push(schemaDefault(schema.item));
      render();
      set(arr);
    });
    add.classList.add("le-add");
    box.appendChild(add);
  };
  render();
  return box;
}

// [[グループ名, item], ...] をグループ名ごとのパネルで編集する。画面上は
// グループ単位の状態を持ち、変更のたびに元の形のリスト（arr）へ書き戻す。
function buildSchemaGrouped(arr, schema, set) {
  const groups = [];
  for (const [name, item] of arr) {
    let g = groups.find((x) => x.name === name);
    if (!g) groups.push((g = { name, items: [] }));
    g.items.push(item);
  }
  const flush = () => {
    arr.length = 0;
    for (const g of groups) for (const item of g.items) arr.push([g.name, item]);
    set(arr);
  };

  const box = document.createElement("div");
  box.className = "le-list";
  const render = () => {
    box.innerHTML = "";
    groups.forEach((g, gi) => {
      const panel = document.createElement("div");
      panel.className = "le-group";
      const head = document.createElement("div");
      head.className = "le-group-head";
      const label = document.createElement("span");
      label.className = "le-sfield-label";
      label.textContent = schema.group_label;
      const setName = (v) => {
        g.name = v;
        flush();
      };
      const nameOptions = schema.group_source ? sourceOptions(schema.group_source) : null;
      let nameInput;
      if (nameOptions) {
        nameInput = buildSourceField(g.name, nameOptions, setName, schema.group_placeholder);
      } else {
        nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.placeholder = schema.group_placeholder || "";
        nameInput.value = g.name;
        nameInput.addEventListener("input", () => setName(nameInput.value));
      }
      const count = document.createElement("span");
      count.className = "hint";
      count.textContent = _t(`{0} 件`, g.items.length);
      head.append(
        label,
        nameInput,
        count,
        smallBtn("✕", _t(`この{0}をまとめて削除`, schema.group_label), false, () => {
          if (g.items.length > 0 && !confirm(_t(`{0}「{1}」の {2} 件をまとめて削除しますか？`, schema.group_label, g.name, g.items.length))) return;
          groups.splice(gi, 1);
          render();
          flush();
        })
      );
      panel.appendChild(head);
      // グループ内の項目は通常のリストと同じ編集UI（並べ替え・削除・追加）
      panel.appendChild(buildSchemaList(g.items, { type: "list", item: schema.item }, () => {
        count.textContent = _t(`{0} 件`, g.items.length);
        flush();
      }));
      box.appendChild(panel);
    });
    const add = smallBtn(_t(`＋ {0}を追加`, schema.group_label), _t(`{0}を追加`, schema.group_label), false, () => {
      // 項目が0件のグループはリストに書き出せないため、空の項目を1件持たせる
      groups.push({ name: "", items: [schemaDefault(schema.item)] });
      render();
      flush();
    });
    add.classList.add("le-add");
    box.appendChild(add);
  };
  render();
  return box;
}

function buildSchemaTuple(arr, schema, set) {
  const box = document.createElement("div");
  box.className = "le-tuple";
  // [スキル名, スクリプトファイル名] は、選んだスキルの scripts/*.py をスクリプト欄の候補にする
  const skillIdx = schema.items.findIndex((it) => it.schema.source === "skill");
  const ctx = skillIdx >= 0 ? { skill: () => arr[skillIdx] } : {};
  const scriptFields = []; // [idx, 作り直す関数, 今の欄を返す関数]
  // スキル名を確定したとき（一覧から選んだ・手入力を確定した）に呼ぶ。手入力の途中では呼ばない（入力途中の名前が
  // 既知のスキルと一致した時点で、スクリプト名を消して欄を作り直さないため）。
  const refreshScripts = () => {
    const skill = configCandidates && configCandidates.skills.find((s) => s.name === arr[skillIdx]);
    for (const [idx, rerender, current] of scriptFields) {
      // 手入力したスクリプト名（"_" で始まる等、一覧に出ないもの）は、そのまま残す
      const field = current();
      if (field.isManual && field.isManual()) continue;
      // 認識しているスキルに無いスクリプト名は消す（1つしか無ければそれを選ぶ）。未知のスキルでは触らない
      if (skill && !skill.scripts.includes(arr[idx])) arr[idx] = skill.scripts.length === 1 ? skill.scripts[0] : "";
      rerender();
    }
  };
  schema.items.forEach((it, idx) => {
    const fieldEl = document.createElement("div");
    fieldEl.className = it.schema.type === "number" ? "le-sfield le-sfield-narrow" : "le-sfield";
    const label = document.createElement("span");
    label.className = "le-sfield-label";
    label.textContent = it.label;
    const build = () =>
      buildSchemaEditor(
        arr[idx],
        it.schema,
        (v, opts) => {
          arr[idx] = v;
          if (idx === skillIdx && !(opts && opts.typing)) refreshScripts();
          set(arr);
        },
        false,
        ctx
      );
    let editor = build();
    if (it.schema.source === "script") {
      scriptFields.push([
        idx,
        () => {
          const next = build();
          editor.replaceWith(next);
          editor = next;
        },
        () => editor,
      ]);
    }
    fieldEl.append(label, editor);
    box.appendChild(fieldEl);
  });
  return box;
}

function buildSchemaDict(obj, schema, set) {
  const box = document.createElement("div");
  box.className = "le-dict";
  for (const f of schema.fields) {
    const row = document.createElement("div");
    row.className = "le-field";
    const label = document.createElement("span");
    label.className = "le-field-name";
    label.textContent = f.optional ? _t(`{0}（任意）`, f.name) : f.name;
    const body = document.createElement("div");
    body.className = "le-item-body";
    body.appendChild(
      buildSchemaEditor(
        obj[f.name],
        f.schema,
        (v) => {
          // 任意項目は空欄なら辞書から取り除く（src/config.py 側で「未指定」扱い）
          if (v === undefined || (f.optional && v === "")) delete obj[f.name];
          else obj[f.name] = v;
          set(obj);
        },
        !!f.optional
      )
    );
    row.append(label, body);
    box.appendChild(row);
  }
  return box;
}

function buildSchemaOneOf(value, schema, set) {
  const box = document.createElement("div");
  box.className = "le-oneof";
  let current = value;
  const setCurrent = (v) => {
    current = v;
    set(v);
  };
  const select = document.createElement("select");
  select.className = "le-type";
  schema.variants.forEach((variant, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = variant.label;
    select.appendChild(opt);
  });
  const body = document.createElement("div");
  body.className = "le-oneof-body";
  const renderBody = (i) => {
    body.innerHTML = "";
    body.appendChild(buildSchemaEditor(current, schema.variants[i].schema, setCurrent));
  };
  const initial = Math.max(0, schema.variants.findIndex((variant) => schemaMatches(variant.schema, value)));
  select.value = String(initial);
  // 先頭の欄の候補の種類（"source"）。ツール名 ↔ スキル名のように種類が違えば値を引き継がない
  const headSource = (s) => (s.type === "tuple" ? s.items[0].schema.source : s.type === "string" ? s.source : undefined);
  let shown = initial;
  select.addEventListener("change", () => {
    // 形を切り替えても、入力済みの先頭の文字列（スキル名・ディレクトリ等）は引き継ぐ
    const firstOf = (v) => (Array.isArray(v) ? v[0] : v && typeof v === "object" ? Object.values(v)[0] : v);
    const nextSchema = schema.variants[Number(select.value)].schema;
    const sameKind = headSource(schema.variants[shown].schema) === headSource(nextSchema);
    shown = Number(select.value);
    const head = sameKind && typeof firstOf(current) === "string" ? firstOf(current) : "";
    const next = schemaDefault(nextSchema);
    if (typeof next === "string") setCurrent(head);
    else {
      if (Array.isArray(next) && typeof next[0] === "string") next[0] = head;
      else if (nextSchema.type === "dict" && typeof next[nextSchema.fields[0].name] === "string") next[nextSchema.fields[0].name] = head;
      setCurrent(next);
    }
    renderBody(Number(select.value));
  });
  renderBody(initial);
  box.append(select, body);
  return box;
}

const VALUE_TYPES = [
  ["string", _t("文字")],
  ["number", _t("数値")],
  ["bool", _t("真偽")],
  ["list", _t("リスト")],
];

function valueType(v) {
  if (Array.isArray(v)) return "list";
  if (v !== null && typeof v === "object") return "dict";
  if (typeof v === "number") return "number";
  if (typeof v === "boolean") return "bool";
  return "string";
}

function convertValue(v, type) {
  if (type === "string") return typeof v === "string" ? v : v === null || typeof v === "object" ? "" : String(v);
  if (type === "number") return Number.isFinite(Number(v)) && v !== "" && typeof v !== "object" ? Number(v) : 0;
  if (type === "bool") return v === true;
  if (type === "dict") return {};
  if (Array.isArray(v)) return v;
  return v === "" || v === null ? [] : [v];
}

function buildTypeSelect(current, onChange, withDict = false) {
  const select = document.createElement("select");
  select.className = "le-type";
  select.title = _t("値の種類");
  const types = withDict || current === "dict" ? [...VALUE_TYPES, ["dict", _t("辞書")]] : VALUE_TYPES;
  for (const [value, label] of types) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    select.appendChild(opt);
  }
  select.value = current;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function smallBtn(text, title, disabled, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "le-btn";
  btn.textContent = text;
  btn.title = title;
  btn.disabled = disabled;
  btn.addEventListener("click", onClick);
  return btn;
}

// 追加する要素の雛形。直前の要素と同じ形で、文字列だけ空にする
// （選択肢付きフィールドは有効な値のまま残す）。
function blankLike(v) {
  if (typeof v === "string") return "";
  if (Array.isArray(v)) return v.map(blankLike);
  if (v !== null && typeof v === "object") {
    const obj = {};
    for (const [key, x] of Object.entries(v)) obj[key] = blankLike(x);
    return obj;
  }
  return v;
}

function buildValueEditor(value, set, notify, ctx) {
  if (Array.isArray(value)) return buildArrayEditor(value, notify, ctx);
  if (value !== null && typeof value === "object") return buildDictEditor(value, notify, ctx);
  return buildScalarEditor(value, set);
}

// depth=0（キー直下のリスト）は1要素1行の縦並び、それより深いリスト
// （["スキル名","スクリプト名"] 等の組）は横並びで表示する。
function buildArrayEditor(arr, notify, ctx) {
  const inline = ctx.depth > 0;
  const box = document.createElement("div");
  box.className = inline ? "le-list le-inline" : "le-list";
  const childCtx = { depth: ctx.depth + 1 };
  const render = () => {
    box.innerHTML = "";
    arr.forEach((item, idx) => {
      const itemEl = document.createElement("div");
      itemEl.className = "le-item";
      if (inline) {
        itemEl.appendChild(
          buildTypeSelect(valueType(item), (type) => {
            arr[idx] = convertValue(arr[idx], type);
            render();
            notify();
          })
        );
      }
      const body = document.createElement("div");
      body.className = "le-item-body";
      body.appendChild(
        buildValueEditor(
          item,
          (v) => {
            arr[idx] = v;
            notify();
          },
          notify,
          childCtx
        )
      );
      itemEl.appendChild(body);
      const ctrl = document.createElement("div");
      ctrl.className = "le-item-ctrl";
      if (!inline) {
        const swap = (a, b) => {
          [arr[a], arr[b]] = [arr[b], arr[a]];
          render();
          notify();
        };
        ctrl.appendChild(smallBtn("↑", _t("上へ移動"), idx === 0, () => swap(idx - 1, idx)));
        ctrl.appendChild(smallBtn("↓", _t("下へ移動"), idx === arr.length - 1, () => swap(idx, idx + 1)));
      }
      ctrl.appendChild(
        smallBtn("✕", _t("削除"), false, () => {
          arr.splice(idx, 1);
          render();
          notify();
        })
      );
      itemEl.appendChild(ctrl);
      box.appendChild(itemEl);
    });
    // 空リストには手本にする要素が無いため、追加する値の種類を選ばせる
    const newTypeSelect = arr.length === 0 ? buildTypeSelect("string", () => {}, !inline) : null;
    const add = smallBtn(inline ? _t("＋") : _t("＋ 項目を追加"), _t("項目を追加"), false, () => {
      arr.push(newTypeSelect ? convertValue("", newTypeSelect.value) : blankLike(arr[arr.length - 1]));
      render();
      notify();
    });
    const addRow = document.createElement("div");
    addRow.className = "le-add";
    if (newTypeSelect) addRow.appendChild(newTypeSelect);
    addRow.appendChild(add);
    box.appendChild(addRow);
  };
  render();
  return box;
}

function buildDictEditor(obj, notify, ctx) {
  const box = document.createElement("div");
  box.className = "le-dict";
  const childCtx = { depth: ctx.depth + 1 };
  const render = () => {
    box.innerHTML = "";
    for (const name of Object.keys(obj)) {
      const row = document.createElement("div");
      row.className = "le-field";
      const label = document.createElement("span");
      label.className = "le-field-name";
      label.textContent = name;
      const body = document.createElement("div");
      body.className = "le-item-body";
      body.appendChild(
        buildValueEditor(
          obj[name],
          (v) => {
            obj[name] = v;
            notify();
          },
          notify,
          childCtx
        )
      );
      row.append(
        label,
        body,
        smallBtn("✕", _t("この項目を削除"), false, () => {
          delete obj[name];
          render();
          notify();
        })
      );
      box.appendChild(row);
    }
    const addRow = document.createElement("div");
    addRow.className = "le-field le-field-add";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = _t("項目名");
    const typeSelect = buildTypeSelect("string", () => {});
    const addBtn = smallBtn(_t("＋ 項目を追加"), _t("項目を追加"), false, () => {
      const name = nameInput.value.trim();
      if (!name || name in obj) {
        nameInput.classList.add("invalid");
        return;
      }
      obj[name] = convertValue("", typeSelect.value);
      render();
      notify();
    });
    addRow.append(nameInput, typeSelect, addBtn);
    box.appendChild(addRow);
  };
  render();
  return box;
}

function buildScalarEditor(value, set) {
  if (typeof value === "boolean") {
    const wrap = document.createElement("label");
    wrap.className = "checkbox";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = value;
    input.addEventListener("change", () => set(input.checked));
    wrap.append(input, document.createTextNode(" True"));
    return wrap;
  }
  if (typeof value === "number") {
    const input = document.createElement("input");
    input.type = "number";
    input.step = "any";
    input.value = String(value);
    input.addEventListener("input", () => {
      const n = Number(input.value);
      const ok = input.value.trim() !== "" && Number.isFinite(n);
      input.classList.toggle("invalid", !ok);
      if (ok) set(n);
    });
    return input;
  }
  if (value === null) {
    const span = document.createElement("span");
    span.className = "hint";
    span.textContent = "None";
    return span;
  }
  if (value.length > 60 || value.includes("\n")) {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.rows = Math.min(8, Math.max(2, Math.ceil(value.length / 80) + value.split("\n").length - 1));
    textarea.addEventListener("input", () => set(textarea.value));
    return textarea;
  }
  const input = document.createElement("input");
  input.type = "text";
  input.value = value;
  input.addEventListener("input", () => set(input.value));
  return input;
}

function setEdit(k, newValue) {
  storeEdit(k, newValue);
  refreshKeyRows(applyLinkedEdits(k));
  if (keyId(k) === `${TOKEN_RATIO_SECTION}.enabled`) {
    const banner = $("#config-keys .token-ratio-banner");
    if (banner) banner.replaceWith(buildTokenRatioBanner());
  }
  updateFooter();
}

function storeEdit(k, newValue) {
  const id = keyId(k);
  resetKeys.delete(id);
  delete dirtyEdits[id];
  if (newValue === k.default) {
    // 上書き値が保存済みなら、既定値の手入力は「既定値へ戻す」と同じ変更として扱う
    if (k.override !== null) resetKeys.add(id);
  } else if (k.override === null || newValue !== k.effective) {
    // 上書きがあるときの effective は保存済みの値（既定値に足すだけのリストは、既定値と合成した値。admin/server.py）。
    dirtyEdits[id] = newValue;
  }
}

function updateFooter() {
  const count = Object.keys(dirtyEdits).length + resetKeys.size;
  $("#dirty-count").textContent = count > 0 ? _t(`{0} 件変更`, count) : "";
  $("#save-btn").disabled = count === 0;
}

function buildUpdatePayload() {
  const updates = {};
  for (const id of Object.keys(dirtyEdits)) {
    const [section, key] = splitKeyId(id, configKeys);
    updates[section] = updates[section] || {};
    updates[section][key] = dirtyEdits[id];
  }
  const resets = Array.from(resetKeys).map((id) => splitKeyId(id, configKeys));
  return { updates, resets, base_mtime: configMtime };
}

function splitKeyId(id, keys) {
  // セクション名自体にドットを含む場合(例: context_trim.subagent)があるため、
  // 先頭一致でキー一覧と突き合わせて正しく分割する。
  for (const k of keys) {
    if (keyId(k) === id) return [k.section, k.key];
  }
  const idx = id.lastIndexOf(".");
  return [id.slice(0, idx), id.slice(idx + 1)];
}

async function openPreviewModal() {
  const modal = clone("tpl-preview-modal").firstElementChild;
  document.body.appendChild(modal);
  modal.querySelector(".btn-cancel").addEventListener("click", () => modal.remove());
  const diffEl = modal.querySelector("#preview-diff");
  diffEl.textContent = _t("確認中...");
  try {
    const payload = buildUpdatePayload();
    const result = await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/config/preview`, {
      method: "POST",
      body: payload,
    });
    diffEl.innerHTML = "";
    if (result.changes.length === 0) {
      diffEl.innerHTML = _t('<p class="hint">変更はありません。</p>');
    }
    for (const c of result.changes) {
      const div = document.createElement("div");
      div.className = "diff-item";
      div.innerHTML = `<span class="diff-key">[${c.section}] ${c.key}</span><br/>
        <span class="diff-old">${c.old === null ? _t("(既定値)") : escapeHtml(c.old)}</span> →
        <span class="diff-new">${c.new === null ? _t("(既定値)") : escapeHtml(c.new)}</span>`;
      diffEl.appendChild(div);
    }
    modal.querySelector("#confirm-save-btn").addEventListener("click", () => {
      modal.remove();
      doSave(null);
    });
  } catch (e) {
    modal.querySelector("#preview-error").textContent = e.message;
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

async function doSave() {
  const payload = buildUpdatePayload();
  try {
    const result = await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/config`, {
      method: "PUT",
      body: payload,
    });
    dirtyEdits = {};
    resetKeys = new Set();
    // [paths] project_locohane_dir 等の変更で認識するスキルが変わるため、候補も取り直す
    await Promise.all([loadConfigKeys(), loadConfigCandidates()]);
    renderKeyList();
    updateFooter();
    const banner = $("#restart-banner");
    if (result.needs_restart) {
      banner.classList.remove("hidden");
      const btn = $("#restart-now-btn");
      btn.onclick = async () => {
        await runInstanceAction(currentInstanceName, "restart");
        banner.classList.add("hidden");
      };
    } else {
      banner.classList.add("hidden");
    }
  } catch (e) {
    if (e.status === 409) {
      alert(_t("他のセッションで既に保存されています。画面を再読み込みします。"));
      await renderInstanceDetail(currentInstanceName);
    } else {
      alert(_t("保存に失敗しました: ") + e.message);
    }
  }
}

/* --- ユーザー管理 --- */

async function renderUsers(container) {
  container.appendChild(clone("tpl-users"));
  const data = await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/users`);
  $("#users-inherited-note").textContent = data.inherited_from_project_env
    ? _t("プロジェクト直下 .env の AUTH_USERS（管理者設定）を継承しています。この画面からは変更・削除できません（このインスタンス専用のユーザーを追加すると、以後は専用のものだけが使われます）。")
    : _t("このインスタンス専用のユーザー設定です。");
  const tbody = $("#users-tbody");
  tbody.innerHTML = "";
  for (const name of data.usernames) {
    const inherited = data.inherited_from_project_env;
    const tr = document.createElement("tr");
    if (inherited) tr.classList.add("row-inherited");
    const pwInput = document.createElement("input");
    pwInput.type = "password";
    pwInput.placeholder = _t("新しいパスワード");
    pwInput.disabled = inherited;
    const pwBtn = document.createElement("button");
    pwBtn.textContent = _t("変更");
    pwBtn.disabled = inherited;
    pwBtn.addEventListener("click", async () => {
      if (!pwInput.value) return;
      try {
        await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/users/${encodeURIComponent(name)}`, {
          method: "PUT",
          body: { password: pwInput.value },
        });
      } catch (e) {
        $("#users-error").textContent = e.message;
        return;
      }
      pwInput.value = "";
      alert(_t("パスワードを変更しました。"));
    });
    const delBtn = document.createElement("button");
    delBtn.textContent = _t("削除");
    delBtn.disabled = inherited;
    delBtn.addEventListener("click", async () => {
      if (!confirm(_t(`ユーザー "{0}" を削除しますか？`, name))) return;
      try {
        await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/users/${encodeURIComponent(name)}`, {
          method: "DELETE",
        });
      } catch (e) {
        $("#users-error").textContent = e.message;
        return;
      }
      const fresh = $("#instance-subview");
      fresh.innerHTML = "";
      renderUsers(fresh);
    });
    const tdName = document.createElement("td");
    tdName.textContent = name;
    if (inherited) {
      const badge = document.createElement("span");
      badge.className = "badge-default";
      badge.textContent = _t("プロジェクト直下 .env（管理者設定）");
      badge.title = _t("プロジェクト直下の .env で設定されたユーザーです。変更は管理者が .env を直接編集してください。");
      tdName.appendChild(document.createTextNode(" "));
      tdName.appendChild(badge);
    }
    const tdPw = document.createElement("td");
    tdPw.appendChild(pwInput);
    tdPw.appendChild(pwBtn);
    const tdActions = document.createElement("td");
    tdActions.appendChild(delBtn);
    tr.appendChild(tdName);
    tr.appendChild(tdPw);
    tr.appendChild(tdActions);
    tbody.appendChild(tr);
  }

  $("#add-user-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = new FormData(ev.target);
    try {
      await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/users`, {
        method: "POST",
        body: { username: form.get("username"), password: form.get("password") },
      });
      const fresh = $("#instance-subview");
      fresh.innerHTML = "";
      renderUsers(fresh);
    } catch (e) {
      $("#users-error").textContent = e.message;
    }
  });

  $("#secret-status").textContent = data.auth_secret_set ? _t("設定済み") : _t("未設定");
  $("#gen-secret-btn").addEventListener("click", async () => {
    if (!confirm(_t("JWT署名鍵を再生成すると、既存のログインセッションは全て無効になります。よろしいですか？"))) return;
    await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/auth-secret`, { method: "POST" });
    $("#secret-status").textContent = _t("設定済み（再生成しました）");
  });
}

/* --- 環境変数 --- */

async function renderEnvVars(container) {
  container.appendChild(clone("tpl-env"));
  const data = await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/env`);
  const tbody = $("#env-tbody");
  tbody.innerHTML = "";
  for (const [key, value] of Object.entries(data.vars)) {
    const tr = document.createElement("tr");
    const delBtn = document.createElement("button");
    delBtn.textContent = _t("削除");
    delBtn.addEventListener("click", async () => {
      await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/env/${encodeURIComponent(key)}`, {
        method: "DELETE",
      });
      const fresh = $("#instance-subview");
      fresh.innerHTML = "";
      renderEnvVars(fresh);
    });
    const tdKey = document.createElement("td");
    tdKey.textContent = key;
    const tdVal = document.createElement("td");
    tdVal.textContent = value;
    const tdActions = document.createElement("td");
    tdActions.appendChild(delBtn);
    tr.appendChild(tdKey);
    tr.appendChild(tdVal);
    tr.appendChild(tdActions);
    tbody.appendChild(tr);
  }
  $("#add-env-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = new FormData(ev.target);
    try {
      await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/env`, {
        method: "PUT",
        body: { key: form.get("key"), value: form.get("value") || "" },
      });
      const fresh = $("#instance-subview");
      fresh.innerHTML = "";
      renderEnvVars(fresh);
    } catch (e) {
      $("#env-error").textContent = e.message;
    }
  });
}

/* --- バックアップ --- */

async function renderBackups(container) {
  container.appendChild(clone("tpl-backups"));
  const data = await api(`/api/instances/${encodeURIComponent(currentInstanceName)}/backups`);
  const tbody = $("#backups-tbody");
  tbody.innerHTML = "";
  if (data.backups.length === 0) {
    tbody.innerHTML = _t('<tr><td colspan="3" class="hint">まだバックアップはありません。</td></tr>');
    return;
  }
  for (const b of data.backups) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    tdName.textContent = b.name;
    const tdTime = document.createElement("td");
    tdTime.textContent = new Date(b.mtime * 1000).toLocaleString(I18N_LOCALE);
    const restoreBtn = document.createElement("button");
    restoreBtn.textContent = _t("復元");
    restoreBtn.addEventListener("click", async () => {
      if (!confirm(_t(`{0} の内容で復元します。現在の設定はさらにバックアップされます。よろしいですか？`, b.name))) return;
      try {
        const result = await api(
          `/api/instances/${encodeURIComponent(currentInstanceName)}/backups/${encodeURIComponent(b.name)}/restore`,
          { method: "POST" }
        );
        alert(result.needs_restart ? _t("復元しました。反映にはインスタンスの再起動が必要です。") : _t("復元しました。"));
      } catch (e) {
        alert(_t("復元に失敗しました: ") + e.message);
      }
    });
    const tdActions = document.createElement("td");
    tdActions.appendChild(restoreBtn);
    tr.appendChild(tdName);
    tr.appendChild(tdTime);
    tr.appendChild(tdActions);
    tbody.appendChild(tr);
  }
}

/* ---------------------------------------------------------- 表示設定 */

let settingsTarget = "";

function settingsQuery() {
  return settingsTarget ? `?instance=${encodeURIComponent(settingsTarget)}` : "";
}

function renderSettings() {
  mainEl.innerHTML = "";
  return renderSettingsPanel(mainEl, "");
}

// target: "" なら共通設定（public/settings/）、インスタンス名ならそのインスタンス専用設定。
async function renderSettingsPanel(container, target) {
  settingsTarget = target;
  container.appendChild(clone("tpl-settings"));
  // インスタンス詳細ではサブタブ名が見出しを兼ねるため、タイトルは出さない。
  $("#settings-title").classList.toggle("hidden", Boolean(target));

  $$(".settings-section[data-text]").forEach((section) => {
    const file = section.dataset.text;
    section.querySelector(".save-setting-btn").addEventListener("click", () =>
      settingsAction(() =>
        api(`/api/settings/${file}${settingsQuery()}`, {
          method: "PUT",
          body: { content: section.querySelector("textarea").value },
        }),
        _t("保存しました（ブラウザの再読み込みで反映されます）。")
      )
    );
    section.querySelector(".reset-setting-btn").addEventListener("click", () => resetSetting(file));
  });
  $$(".settings-section[data-image]").forEach((section) => {
    const kind = section.dataset.image;
    section.querySelector(".upload-image-btn").addEventListener("click", () => uploadImage(section, kind));
    section.querySelector(".reset-setting-btn").addEventListener("click", () => resetSetting(`images/${kind}`));
  });
  await loadSettings();
}

async function loadSettings() {
  $("#settings-error").textContent = "";
  $("#settings-target-hint").textContent = settingsTarget
    ? _t("このインスタンスだけに適用されます。専用の設定が無い項目は、上部の「表示設定」タブの共通設定が使われます。")
    : _t("各インスタンスの既定値です。インスタンスごとに変えたい場合は、インスタンスの「設定」→「表示設定」タブで設定してください。");
  const data = await api(`/api/settings${settingsQuery()}`);
  const sourceLabel = (overridden) => (!settingsTarget ? "" : overridden ? _t("［このインスタンス専用］") : _t("［共通設定を使用中］"));
  $$(".settings-section[data-text]").forEach((section) => {
    const info = data.texts[section.dataset.text];
    section.querySelector("textarea").value = info.content;
    section.querySelector(".setting-source").textContent = sourceLabel(info.overridden);
    section.querySelector(".reset-setting-btn").classList.toggle("hidden", !info.overridden);
  });
  $$(".settings-section[data-image]").forEach((section) => {
    const info = data.images[section.dataset.image];
    section.querySelector(".image-current").textContent = info.filename || _t("（なし）");
    section.querySelector(".setting-source").textContent = sourceLabel(info.overridden);
    section.querySelector(".reset-setting-btn").classList.toggle("hidden", !info.overridden);
  });
}

async function settingsAction(fn, message) {
  try {
    await fn();
    await loadSettings();
    alert(message);
  } catch (e) {
    $("#settings-error").textContent = e.message;
  }
}

function resetSetting(target) {
  if (!confirm(_t("このインスタンス専用の設定を削除し、共通設定に戻します。よろしいですか？"))) return;
  settingsAction(
    () => api(`/api/settings/${target}${settingsQuery()}`, { method: "DELETE" }),
    _t("共通設定に戻しました（ブラウザの再読み込みで反映されます）。")
  );
}

async function uploadImage(section, kind) {
  const file = section.querySelector("input[type=file]").files[0];
  if (!file) {
    $("#settings-error").textContent = _t("ファイルを選択してください。");
    return;
  }
  const base64 = await fileToBase64(file);
  settingsAction(
    () =>
      api(`/api/settings/images/${kind}${settingsQuery()}`, {
        method: "PUT",
        body: { filename: file.name, content_base64: base64 },
      }),
    _t("アップロードしました（ブラウザの再読み込みで反映されます）。")
  );
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      resolve(result.substring(result.indexOf(",") + 1));
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

/* ---------------------------------------------------------- 変更履歴 */

async function renderAudit() {
  mainEl.innerHTML = "";
  mainEl.appendChild(clone("tpl-audit"));
  const data = await api("/api/audit?limit=200");
  const tbody = $("#audit-tbody");
  tbody.innerHTML = "";
  for (const entry of data.entries) {
    const tr = document.createElement("tr");
    const cells = [
      entry.ts || "",
      entry.instance || "-",
      entry.action || "",
      entry.actor || "",
      summarizeAuditEntry(entry),
    ];
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  if (data.entries.length === 0) {
    tbody.innerHTML = _t('<tr><td colspan="5" class="hint">まだ履歴はありません。</td></tr>');
  }
}

function summarizeAuditEntry(entry) {
  if (entry.changes && entry.changes.length) {
    return entry.changes.map((c) => `[${c.section}].${c.key}`).join(", ");
  }
  if (entry.action === "conversation_view") return _t(`会話閲覧: {0} / {1}`, entry.thread_owner || "", entry.thread_id || "");
  if (entry.target_user) return _t(`対象: {0}`, entry.target_user);
  if (entry.file) return _t(`ファイル: {0}`, entry.file);
  if (entry.key) return _t(`キー: {0}`, entry.key);
  return "";
}

/* ------------------------------------------------------------ モニター */
/* 稼働状況・会話閲覧・トークン推移・ログ・LLM接続先（読み取り専用。admin/monitor.py 参照） */

const MONITOR_POLL_MS = 5000;
let monitorTimer = null;

function stopMonitorPolling() {
  if (monitorTimer) clearInterval(monitorTimer);
  monitorTimer = null;
}

// container を表示している間だけ fn を定期実行する。前回の fn が終わるまで次を出さない
// （取得が間隔より遅いと要求が積み上がるため）。container が画面から外れたら自分の
// タイマーだけを止める（stopMonitorPolling() だと、その間に別の画面が始めた
// ポーリングまで止めてしまう）。
function startMonitorPolling(container, fn) {
  stopMonitorPolling();
  let busy = false;
  const timer = setInterval(async () => {
    if (!document.body.contains(container)) {
      clearInterval(timer);
      if (monitorTimer === timer) monitorTimer = null;
      return;
    }
    if (busy) return;
    busy = true;
    try {
      await fn();
    } finally {
      busy = false;
    }
  }, MONITOR_POLL_MS);
  monitorTimer = timer;
}

// 連続して投げた要求のうち、最後のもの以外の応答を捨てるための連番
// （検索語の入力中などに、古い条件の応答が後から届いて表示を上書きしないように）。
function nextRequestSeq(target) {
  target._requestSeq = (target._requestSeq || 0) + 1;
  return target._requestSeq;
}

function isLatestRequest(target, seq) {
  return target._requestSeq === seq;
}

// textContent だけで要素を組み立てる（ユーザー名・会話内容等は信頼できないデータのため innerHTML を使わない）。
function el(tag, props, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children || [])) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : String(c));
  }
  return node;
}

function monitorPath(suffix) {
  return `/api/instances/${encodeURIComponent(currentInstanceName)}/monitor${suffix}`;
}

function fmtNum(n) {
  return n === null || n === undefined ? "-" : Number(n).toLocaleString("ja-JP");
}

function fmtCompact(n) {
  if (n >= 1e6) return `${+(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${+(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k`;
  return String(n);
}

// ISO 8601（UTC の "+00:00"/"Z" 付き、またはローカル時刻）をローカル表示へ。
function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(I18N_LOCALE, { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtElapsed(iso) {
  if (!iso) return "-";
  const sec = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (sec < 60) return _t(`{0}秒`, sec);
  if (sec < 3600) return _t(`{0}分{1}秒`, Math.floor(sec / 60), sec % 60);
  return _t(`{0}時間{1}分`, Math.floor(sec / 3600), Math.floor((sec % 3600) / 60));
}

// sort（任意）: { sortable: 列ごとの真偽, index: 並べ替え中の列（無ければ null）, dir: "asc"|"desc", onSort: (列) => void }。
// 見出しを押すと onSort を呼ぶだけで、行の並べ替えは呼び出し側で行う。
function dataTable(headers, rows, emptyText, sort) {
  const tbody = el("tbody");
  for (const cells of rows) tbody.append(el("tr", {}, cells.map((c) => el("td", {}, c))));
  if (!rows.length) tbody.append(el("tr", {}, el("td", { colspan: headers.length, class: "hint", text: emptyText || _t("なし") })));
  const th = (h, i) => {
    if (!sort || !sort.sortable[i]) return el("th", { text: h });
    const active = sort.index === i;
    return el("th", {
      class: "sortable" + (active ? " sorted" : ""),
      title: _t("クリックで並べ替え"),
      "aria-sort": active ? (sort.dir === "asc" ? "ascending" : "descending") : "none",
      onclick: () => sort.onSort(i),
    }, [h, el("span", { class: "sort-indicator", text: active ? (sort.dir === "asc" ? " ▲" : " ▼") : " ↕" })]);
  };
  return el("table", { class: "data-table" }, [el("thead", {}, el("tr", {}, headers.map(th))), tbody]);
}

function linkBtn(text, onClick) {
  return el("button", { class: "link-btn inline-link", text, onclick: onClick });
}

function statTile(label, value, sub) {
  return el("div", { class: "stat-tile" }, [
    el("div", { class: "stat-value", text: value }),
    el("div", { class: "stat-label", text: label }),
    sub ? el("div", { class: "stat-sub", text: sub }) : null,
  ]);
}

/* --- 稼働状況 --- */

async function renderMonitor(container) {
  const tiles = el("div", { class: "stat-row" });
  const body = el("div");
  const updated = el("span", { class: "hint" });
  const endpointsBox = el("div");
  container.append(
    el("div", { class: "monitor-toolbar" }, [updated, el("span", { class: "hint", text: _t(`（{0}秒ごとに自動更新）`, MONITOR_POLL_MS / 1000) })]),
    tiles,
    body,
    el("section", { class: "monitor-section" }, [
      el("div", { class: "section-head" }, [
        el("h3", { text: _t("LLM接続先") }),
        el("button", { text: _t("状態を確認"), onclick: () => loadEndpoints(endpointsBox) }),
      ]),
      el("p", { class: "hint", text: _t("[llm] main_url / sub_url の各接続先へ問い合わせます（llama_cpp は GET /slots でスロット使用状況も表示）。") }),
      endpointsBox,
    ])
  );

  const refresh = async () => {
    let data;
    try {
      data = await api(monitorPath("/runtime"));
    } catch (e) {
      body.replaceChildren(el("div", { class: "error", text: e.message }));
      return;
    }
    if (!document.body.contains(container)) return;
    updated.textContent = _t(`状態: {0}`, STATE_LABELS[data.state] || data.state) + (data.updated_at ? _t(` ／ 本体の最終更新 {0}`, fmtTime(data.updated_at)) : "");
    const lc = data.log_counts_24h || {};
    tiles.replaceChildren(
      statTile(_t("接続中ユーザー"), data.available ? String(data.users.filter((u) => u.sessions > 0).length) : "-"),
      statTile(_t("接続セッション"), data.available ? String(data.sessions.length) : "-"),
      statTile(_t("生成中スレッド"), data.available ? String(data.generating.length) : "-", data.available ? _t(`うち応答待ち {0}`, data.generating.filter((g) => g.waiting_for_user).length) : null),
      statTile(_t("直近24時間の警告"), `${fmtNum(lc.WARNING || 0)}`, `ERROR ${fmtNum(lc.ERROR || 0)} / CRITICAL ${fmtNum(lc.CRITICAL || 0)}`)
    );
    if (!data.available) {
      body.replaceChildren(
        el("p", {
          class: "hint",
          text:
            data.state === "running" || data.state === "external"
              ? _t("実行状態を取得できません（本体が更新前のバージョン、または起動直後です）。")
              : _t("インスタンスが停止しているため、接続中のユーザーはいません。"),
        })
      );
      return;
    }
    const openThread = (id) => () => selectInstanceSubview("threads", { openThreadId: id });
    const threadCell = (id, name) => (id && name ? linkBtn(name, openThread(id)) : el("span", { class: "hint", text: id ? _t("（未送信の新規チャット）") : "-" }));
    body.replaceChildren(
      el("section", { class: "monitor-section" }, [
        el("h3", { text: _t("実行中のユーザー") }),
        dataTable(
          [_t("ユーザー"), _t("接続セッション"), _t("生成中スレッド"), ""],
          data.users.map((u) => [u.user, String(u.sessions), String(u.generating), linkBtn(_t("会話一覧"), () => selectInstanceSubview("threads", { owner: u.user }))]),
          _t("接続中のユーザーはいません。")
        ),
      ]),
      el("section", { class: "monitor-section" }, [
        el("h3", { text: _t("生成中のスレッド") }),
        dataTable(
          [_t("ユーザー"), _t("スレッド"), _t("経過時間"), _t("状態")],
          data.generating.map((g) => [
            g.owner || "-",
            threadCell(g.thread_id, g.thread_name || g.thread_id),
            fmtElapsed(g.started_at),
            g.waiting_for_user ? el("span", { class: "badge warn", text: _t("ユーザーの応答待ち") }) : el("span", { class: "badge ok", text: _t("生成中") }),
          ]),
          _t("生成中のスレッドはありません。")
        ),
      ]),
      el("section", { class: "monitor-section" }, [
        el("h3", { text: _t("接続中のセッション（ブラウザタブ）") }),
        dataTable(
          [_t("ユーザー"), _t("開いているスレッド")],
          data.sessions.map((s) => [s.user, threadCell(s.thread_id, s.has_first_interaction ? s.thread_name || s.thread_id : null)]),
          _t("接続中のセッションはありません。")
        ),
      ])
    );
  };
  await refresh();
  // 初回の取得中に別のタブへ移っていたら始めない（移動先の画面のポーリングを止めてしまうため）。
  if (document.body.contains(container)) startMonitorPolling(container, refresh);
}

async function loadEndpoints(box) {
  const seq = nextRequestSeq(box);
  box.replaceChildren(el("p", { class: "hint", text: _t("確認中...") }));
  try {
    const data = await api(monitorPath("/endpoints"));
    if (!isLatestRequest(box, seq)) return;
    box.replaceChildren(
      dataTable(
        [_t("用途"), _t("接続先"), _t("モデル"), "provider", _t("状態"), _t("スロット使用中"), _t("応答時間")],
        data.endpoints.map((e) => [
          e.role,
          e.base_url,
          e.model,
          e.provider,
          e.reachable ? el("span", { class: "badge ok", text: _t("✓ 応答あり"), title: e.error || "" }) : el("span", { class: "badge critical", text: _t("✕ 応答なし"), title: e.error || "" }),
          e.slots_total === null ? el("span", { class: "hint", text: "-", title: e.reachable ? e.error || "" : "" }) : `${e.slots_busy} / ${e.slots_total}`,
          `${fmtNum(e.latency_ms)} ms`,
        ])
      )
    );
  } catch (e) {
    if (!isLatestRequest(box, seq)) return;
    box.replaceChildren(el("div", { class: "error", text: e.message }));
  }
}

/* --- 会話（スレッド一覧・閲覧） --- */

const THREADS_PAGE_SIZE = 50;

async function renderThreads(container, opts) {
  const state = { owner: opts.owner || "", q: "", offset: 0 };
  const usersBox = el("div");
  const ownerSelect = el("select", { class: "inline-select" }, el("option", { value: "", text: _t("（全ユーザー）") }));
  const search = el("input", { type: "search", placeholder: _t("スレッド名・IDで検索...") });
  const listBox = el("div");
  const pager = el("div", { class: "pager" });
  const viewer = el("div", { class: "thread-viewer" });

  container.append(
    el("section", { class: "monitor-section" }, [el("h3", { text: _t("ユーザー別の利用状況") }), usersBox]),
    el("section", { class: "monitor-section" }, [el("h3", { text: _t("スレッド一覧") }), el("div", { class: "filter-row" }, [ownerSelect, search]), listBox, pager]),
    viewer
  );

  try {
    const users = (await api(monitorPath("/users"))).users;
    for (const u of users) ownerSelect.append(el("option", { value: u.user, text: u.user }));
    usersBox.replaceChildren(
      dataTable(
        [_t("ユーザー"), _t("スレッド数"), _t("トークン累計"), _t("最終利用")],
        users.map((u) => [
          linkBtn(u.user, () => {
            ownerSelect.value = u.user;
            state.owner = u.user;
            state.offset = 0;
            loadList();
          }),
          fmtNum(u.threads),
          fmtNum(u.tokens_total),
          fmtTime(u.last_active),
        ])
      )
    );
  } catch (e) {
    usersBox.replaceChildren(el("div", { class: "error", text: e.message }));
  }
  ownerSelect.value = state.owner;

  async function loadList() {
    const params = new URLSearchParams({ limit: THREADS_PAGE_SIZE, offset: state.offset });
    if (state.owner) params.set("owner", state.owner);
    if (state.q) params.set("q", state.q);
    const seq = nextRequestSeq(listBox);
    let data;
    try {
      data = await api(monitorPath(`/threads?${params}`));
    } catch (e) {
      if (isLatestRequest(listBox, seq)) listBox.replaceChildren(el("div", { class: "error", text: e.message }));
      return;
    }
    if (!isLatestRequest(listBox, seq)) return;
    listBox.replaceChildren(
      dataTable(
        [_t("最終更新"), _t("ユーザー"), _t("スレッド"), _t("発言数"), _t("トークン累計")],
        data.threads.map((t) => [fmtTime(t.updated_at), t.owner, linkBtn(t.name || t.id, () => openThreadViewer(viewer, t.id)), fmtNum(t.user_messages), fmtNum(t.tokens_total)]),
        _t("該当するスレッドはありません。")
      )
    );
    const from = data.total ? state.offset + 1 : 0;
    const to = Math.min(state.offset + THREADS_PAGE_SIZE, data.total);
    pager.replaceChildren(
      el("button", { text: _t("← 前へ"), disabled: state.offset === 0 ? "" : null, onclick: () => { state.offset -= THREADS_PAGE_SIZE; loadList(); } }),
      el("span", { class: "hint", text: _t(`{0}–{1} / {2}件`, from, to, data.total) }),
      el("button", { text: _t("次へ →"), disabled: to >= data.total ? "" : null, onclick: () => { state.offset += THREADS_PAGE_SIZE; loadList(); } })
    );
  }

  ownerSelect.addEventListener("change", () => {
    state.owner = ownerSelect.value;
    state.offset = 0;
    loadList();
  });
  let searchTimer = null;
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = search.value.trim();
      state.offset = 0;
      loadList();
    }, 300);
  });
  await loadList();
  if (opts.openThreadId) openThreadViewer(viewer, opts.openThreadId);
}

const STEP_LABELS = { user_message: _t("ユーザー"), assistant_message: "AI", system_message: _t("システム"), tool: _t("ツール"), llm: _t("思考") };

async function openThreadViewer(viewer, threadId, internal) {
  const seq = nextRequestSeq(viewer);
  viewer.replaceChildren(el("p", { class: "hint", text: _t("読み込み中...") }));
  let detail, tokens;
  try {
    [detail, tokens] = await Promise.all([
      api(monitorPath(`/threads/${encodeURIComponent(threadId)}?internal=${internal ? "true" : "false"}`)),
      api(monitorPath(`/threads/${encodeURIComponent(threadId)}/tokens`)),
    ]);
  } catch (e) {
    if (isLatestRequest(viewer, seq)) viewer.replaceChildren(el("div", { class: "error", text: e.message }));
    return;
  }
  // 別のスレッドを開き直した（または表示切替を重ねた）後に届いた古い応答は捨てる。
  if (!isLatestRequest(viewer, seq)) return;
  // ログが残っていればログの合計を使う。スレッドに保存された累計（token_usage_cumulative）は、
  // 2026-10-08 の修正（LLM呼び出しごとの即時保存・圧縮処理分の加算）より前の会話では、
  // 生成中の切断→再開で 0 からやり直しになったり圧縮処理分が抜けたりして少なく出るため。
  const pts = tokens.points;
  const fromLog = pts.length > 0;
  const cum = fromLog
    ? {
        total: pts[pts.length - 1].cumulative_total,
        input: pts.reduce((s, p) => s + p.call_in, 0),
        output: pts.reduce((s, p) => s + p.call_out, 0),
      }
    : detail.token_usage_cumulative || {};
  const cumSource = fromLog ? _t("アプリログの合計") : _t("スレッドに保存された値");
  const internalCb = el("input", { type: "checkbox" });
  internalCb.checked = !!internal;
  internalCb.addEventListener("change", () => openThreadViewer(viewer, threadId, internalCb.checked));

  const messages = el("div", { class: "conversation" });
  for (const s of detail.steps) {
    const role = s.type === "user_message" ? "user" : s.type === "assistant_message" || s.type === "system_message" ? "ai" : "internal";
    const head = el("div", { class: "msg-head" }, [
      el("strong", { text: s.type === "tool" ? _t(`ツール: {0}`, s.name || "") : STEP_LABELS[s.type] || s.type }),
      el("span", { class: "hint", text: fmtTime(s.created_at) }),
      s.truncated ? el("span", { class: "badge warn", text: _t("長いため一部省略") }) : null,
    ]);
    let bodyNode;
    if (role === "internal" || s.is_control) {
      bodyNode = el("details", {}, [
        el("summary", { text: (s.output || s.input || "").split("\n")[0].slice(0, 120) || _t("（内容なし）") }),
        s.input ? el("pre", { class: "msg-pre" }, [el("span", { class: "hint", text: _t("入力:\n") }), s.input]) : null,
        el("pre", { class: "msg-pre", text: s.output }),
      ]);
    } else {
      bodyNode = el("pre", { class: "msg-body", text: s.output });
    }
    messages.append(el("div", { class: `msg msg-${role}` }, [head, bodyNode]));
  }
  if (!detail.steps.length) messages.append(el("p", { class: "hint", text: _t("表示できるメッセージがありません。") }));

  viewer.replaceChildren(
    el("section", { class: "monitor-section viewer-section" }, [
      el("div", { class: "section-head" }, [
        el("h3", { text: detail.name || detail.id }),
        el("button", {
          text: _t("閉じる"),
          onclick: () => {
            nextRequestSeq(viewer); // 読み込み中の応答で開き直さないように
            viewer.replaceChildren();
          },
        }),
      ]),
      el("div", { class: "thread-meta hint" }, [
        _t(`ユーザー: {0} ／ 作成: {1} ／ 最終更新: {2} ／ thread_id: {3}`, detail.owner, fmtTime(detail.created_at), fmtTime(detail.updated_at), detail.id),
        detail.work_dir ? _t(` ／ 作業ディレクトリ: {0}`, detail.work_dir) : "",
      ]),
      el("div", { class: "stat-row" }, [
        statTile(_t("トークン累計（合計）"), fmtNum(cum.total), cumSource),
        statTile(_t("入力"), fmtNum(cum.input), cumSource),
        statTile(_t("出力"), fmtNum(cum.output), cumSource),
        statTile(_t("LLM呼び出し回数"), fmtNum(tokens.points.length), _t("アプリログに残っている分")),
      ]),
      buildTokenCharts(tokens.points),
      el("div", { class: "section-head" }, [
        el("h3", { text: _t("会話内容") }),
        el("div", { class: "filter-row" }, [
          el("label", { class: "checkbox" }, [internalCb, _t(" ツール実行・思考・UI制御メッセージも表示")]),
          linkBtn(_t("このスレッドのログを見る"), () => selectInstanceSubview("logs", { threadId: detail.id })),
        ]),
      ]),
      messages,
    ])
  );
  viewer.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* --- トークン推移グラフ（インラインSVG。外部ライブラリは使わない＝完全オフライン） --- */

// 系列色は濃色パネル（--panel）上で配色検証済み（明度帯・色覚多様性での識別・コントラスト）。
const SERIES_BLUE = "#3987e5";
const SERIES_ORANGE = "#d95926";
const SERIES_AQUA = "#199e70";

const TOKEN_KIND_LABELS = { main: _t("メイン"), sub: _t("サブ"), compaction: _t("圧縮処理") };

function buildTokenCharts(points) {
  const wrap = el("div", { class: "token-charts" });
  if (!points.length) {
    wrap.append(
      el("p", {
        class: "hint",
        text: _t("トークン推移のデータがありません（[llm] track_token_usage=false、またはアプリログがローテーション・削除済み）。"),
      })
    );
    return wrap;
  }
  const compactions = points.map((p, i) => (p.compacted ? i : -1)).filter((i) => i >= 0);
  const hasSub = points.some((p) => p.kind === "sub");
  const hasCompactionCall = points.some((p) => p.kind === "compaction");
  const grid = el("div", { class: "chart-grid-row" });
  wrap.append(grid);
  grid.append(
    lineChart({
      title: _t("会話の累積トークン使用量の推移"),
      note: _t("各LLM呼び出しの入力＋出力を足し上げた値です。縦の破線はコンテキスト圧縮の実行位置です。"),
      points,
      series: [
        { key: "cumulative_total", label: _t("全体（サブエージェント・圧縮処理を含む）"), color: SERIES_BLUE },
        { key: "cumulative_main_total", label: _t("メインエージェントのみ"), color: SERIES_ORANGE },
      ],
      markers: compactions,
    }),
    lineChart({
      title: _t("LLMリクエストごとの入力トークン（コンテキスト長）"),
      note: _t("圧縮が効くと、破線の後でメインエージェントの値が下がります。") + (hasSub || hasCompactionCall ? _t("サブエージェント・圧縮処理の呼び出しは点で表示します。") : ""),
      points,
      series: [
        { key: "call_in", label: _t("メインエージェント"), color: SERIES_BLUE, filter: (p) => p.kind === "main" },
        ...(hasSub ? [{ key: "call_in", label: _t("サブエージェント"), color: SERIES_ORANGE, filter: (p) => p.kind === "sub", dotsOnly: true }] : []),
        ...(hasCompactionCall ? [{ key: "call_in", label: _t("圧縮処理"), color: SERIES_AQUA, filter: (p) => p.kind === "compaction", dotsOnly: true }] : []),
      ],
      markers: compactions,
    })
  );
  const table = dataTable(
    ["#", _t("日時"), _t("種別"), _t("入力"), _t("出力"), _t("このターン累計"), _t("全体累計"), _t("メイン累計")],
    points.map((p, i) => [
      String(i + 1),
      fmtTime(p.ts),
      (TOKEN_KIND_LABELS[p.kind] || p.kind) + (p.compacted ? _t("（圧縮後）") : ""),
      fmtNum(p.call_in),
      fmtNum(p.call_out),
      fmtNum(p.turn_total),
      fmtNum(p.cumulative_total),
      fmtNum(p.cumulative_main_total),
    ])
  );
  wrap.append(el("details", { class: "chart-table" }, [el("summary", { text: _t("数値を表で表示") }), table]));
  return wrap;
}

function niceTicks(max, count) {
  if (max <= 0) return [0];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v);
  if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
  return ticks;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

/* x = LLM呼び出しの順番（時刻は呼び出しが偏在して読みにくいため、ツールチップで示す）。 */
function lineChart({ title, note, points, series, markers }) {
  const W = 720, H = 240, M = { top: 16, right: 16, bottom: 30, left: 56 };
  const iw = W - M.left - M.right, ih = H - M.top - M.bottom;
  const n = points.length;
  const yMax = Math.max(1, ...series.flatMap((s) => points.filter(s.filter || (() => true)).map((p) => p[s.key])));
  const ticks = niceTicks(yMax, 4);
  const top = ticks[ticks.length - 1];
  const x = (i) => M.left + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v) => M.top + ih - (v / top) * ih;

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", "aria-label": title });
  for (const t of ticks) {
    svg.append(svgEl("line", { x1: M.left, x2: W - M.right, y1: y(t), y2: y(t), class: t === 0 ? "chart-baseline" : "chart-grid" }));
    const label = svgEl("text", { x: M.left - 8, y: y(t) + 4, "text-anchor": "end", class: "chart-axis-text" });
    label.textContent = fmtCompact(t);
    svg.append(label);
  }
  // x軸: 最初・最後と途中数か所に呼び出し番号。
  const xTickIdx = n <= 1 ? [0] : [...new Set([0, Math.round((n - 1) / 3), Math.round((2 * (n - 1)) / 3), n - 1])];
  for (const i of xTickIdx) {
    const t = svgEl("text", { x: x(i), y: H - 10, "text-anchor": "middle", class: "chart-axis-text" });
    t.textContent = _t(`{0}回目`, i + 1);
    svg.append(t);
  }
  for (const i of markers || []) {
    svg.append(svgEl("line", { x1: x(i), x2: x(i), y1: M.top, y2: M.top + ih, class: "chart-marker" }));
    const t = svgEl("text", { x: x(i) + 3, y: M.top + 10, class: "chart-axis-text" });
    t.textContent = _t("圧縮");
    svg.append(t);
  }
  for (const s of series) {
    const idx = points.map((p, i) => i).filter((i) => !s.filter || s.filter(points[i]));
    if (!s.dotsOnly && idx.length > 1) {
      const d = idx.map((i, k) => `${k ? "L" : "M"}${x(i).toFixed(1)},${y(points[i][s.key]).toFixed(1)}`).join("");
      svg.append(svgEl("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    if (s.dotsOnly || idx.length <= 1 || n <= 40) {
      for (const i of idx) {
        svg.append(svgEl("circle", { cx: x(i), cy: y(points[i][s.key]), r: s.dotsOnly ? 4 : 3, fill: s.color, stroke: "var(--panel)", "stroke-width": 2 }));
      }
    }
  }
  // ホバー: 最寄りの呼び出し位置へ吸着する縦線＋全系列の値を出すツールチップ。
  const cross = svgEl("line", { y1: M.top, y2: M.top + ih, class: "chart-crosshair", visibility: "hidden" });
  svg.append(cross);
  const hit = svgEl("rect", { x: M.left, y: M.top, width: iw, height: ih, fill: "transparent", tabindex: 0 });
  svg.append(hit);

  const tooltip = el("div", { class: "chart-tooltip hidden" });
  const box = el("div", { class: "chart-box" }, [svg, tooltip]);
  const showAt = (i) => {
    const p = points[i];
    cross.setAttribute("x1", x(i));
    cross.setAttribute("x2", x(i));
    cross.setAttribute("visibility", "visible");
    const rows = [el("div", { class: "hint", text: _t(`{0}回目 ・ {1}{2}`, i + 1, fmtTime(p.ts), p.compacted ? _t(" ・ 圧縮後") : "") })];
    for (const s of series) {
      if (s.filter && !s.filter(p)) continue;
      rows.push(el("div", { class: "tt-row" }, [el("span", { class: "tt-key", style: `background:${s.color}` }), el("strong", { text: fmtNum(p[s.key]) }), el("span", { class: "hint", text: s.label })]));
    }
    tooltip.replaceChildren(...rows);
    tooltip.classList.remove("hidden");
    const rect = svg.getBoundingClientRect();
    const px = (x(i) / W) * rect.width;
    tooltip.style.left = `${px + 12 + tooltip.offsetWidth > rect.width ? Math.max(0, px - 12 - tooltip.offsetWidth) : px + 12}px`;
    tooltip.style.top = "8px";
  };
  const hide = () => {
    cross.setAttribute("visibility", "hidden");
    tooltip.classList.add("hidden");
  };
  let focusIdx = n - 1;
  hit.addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const sx = ((ev.clientX - rect.left) / rect.width) * W;
    focusIdx = n === 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round(((sx - M.left) / iw) * (n - 1))));
    showAt(focusIdx);
  });
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("focus", () => showAt(focusIdx));
  hit.addEventListener("blur", hide);
  hit.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowLeft") focusIdx = Math.max(0, focusIdx - 1);
    else if (ev.key === "ArrowRight") focusIdx = Math.min(n - 1, focusIdx + 1);
    else return;
    ev.preventDefault();
    showAt(focusIdx);
  });

  const legend =
    series.length > 1
      ? el("div", { class: "chart-legend" }, series.map((s) => el("span", { class: "legend-item" }, [el("span", { class: s.dotsOnly ? "legend-dot" : "legend-line", style: `background:${s.color}` }), s.label])))
      : null;
  return el("figure", { class: "chart-card" }, [el("figcaption", { text: title }), legend, box, note ? el("div", { class: "hint", text: note }) : null]);
}

/* --- アプリログ --- */

async function renderLogs(container, opts) {
  const level = el("select", { class: "inline-select" }, ["WARNING", "ERROR", "INFO", "DEBUG"].map((l) => el("option", { value: l, text: _t(`{0} 以上`, l) })));
  const search = el("input", { type: "search", placeholder: _t("メッセージで絞り込み...") });
  const threadInput = el("input", { type: "search", placeholder: _t("thread_id で絞り込み") });
  threadInput.value = opts.threadId || "";
  if (opts.threadId) level.value = "INFO";
  const listBox = el("div", { class: "log-list" });
  container.append(
    el("div", { class: "filter-row" }, [level, search, threadInput, el("button", { text: _t("再読み込み"), onclick: () => load() })]),
    el("p", { class: "hint", text: _t("[log] dir の app_*.log を新しい順に最大500件表示します。") }),
    listBox
  );
  async function load() {
    const params = new URLSearchParams({ level: level.value, limit: 500 });
    if (search.value.trim()) params.set("q", search.value.trim());
    if (threadInput.value.trim()) params.set("thread_id", threadInput.value.trim());
    const seq = nextRequestSeq(listBox);
    listBox.replaceChildren(el("p", { class: "hint", text: _t("読み込み中...") }));
    try {
      const data = await api(monitorPath(`/logs?${params}`));
      if (!isLatestRequest(listBox, seq)) return;
      listBox.replaceChildren(
        ...data.entries.map((e) =>
          el("div", { class: `log-entry log-${e.level.toLowerCase()}` }, [
            el("div", { class: "log-head" }, [
              el("span", { class: "badge " + ({ WARNING: "warn", ERROR: "critical", CRITICAL: "critical" }[e.level] || "") , text: e.level }),
              el("span", { class: "hint", text: _t(`{0} ・ {1}{2}`, fmtTime(e.ts), e.logger, e.thread_id ? _t(" ・ ") + e.thread_id : "") }),
            ]),
            el("pre", { class: "msg-pre", text: e.message }),
          ])
        )
      );
      if (!data.entries.length) listBox.append(el("p", { class: "hint", text: _t("該当するログはありません。") }));
    } catch (e) {
      if (isLatestRequest(listBox, seq)) listBox.replaceChildren(el("div", { class: "error", text: e.message }));
    }
  }
  level.addEventListener("change", load);
  let t = null;
  for (const input of [search, threadInput]) {
    input.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(load, 400);
    });
  }
  await load();
}

/* ---------------------------------------------------- APIリファレンス */

async function renderApiDocs() {
  mainEl.innerHTML = "";
  mainEl.appendChild(clone("tpl-api-docs"));
  try {
    // サーバー側で md をリクエストごとに読み直すので、キャッシュさせない。
    const data = await api(`/api/docs/api-reference?t=${Date.now()}`);
    // HTML はサーバー側（markdown-it-py、生HTML無効）で生成済み。
    $("#api-docs-body").innerHTML = data.html;
  } catch (e) {
    $("#api-docs-error").textContent = e.message || _t("APIリファレンスの読み込みに失敗しました。");
  }
}

/* ---------------------------------------------------------- 起動 */

boot();
