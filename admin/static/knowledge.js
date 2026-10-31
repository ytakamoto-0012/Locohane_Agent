"use strict";
/* 設定ダッシュボードの「ナレッジ」画面（インスタンス詳細のサブタブ）。
 * ベクターDB（src/vector_db/）のコレクションの作成・編集・削除と、文書の取り込み。
 * インスタンス間のコピーと、別のインスタンスのコレクションの参照も扱う。
 * API は admin/knowledge.py。共通ヘルパー（api / el / dataTable / fmtTime /
 * startMonitorPolling 等）は admin.js のものを使う。
 */

function knowledgePath(name, suffix) {
  return `/api/instances/${encodeURIComponent(name)}/knowledge${suffix || ""}`;
}

// ジョブの状態（admin/knowledge.py の IngestJob.state）。queued は同じインスタンスの削除・取り込みの終わり待ち。
const KNOWLEDGE_JOB_LABELS = { queued: _t("待機中"), running: _t("取り込み中"), done: _t("完了"), error: _t("エラー"), stopped: _t("停止") };
const KNOWLEDGE_COPY_LABELS = { queued: _t("コピー待ち"), running: _t("コピー中"), done: _t("コピー完了"), error: _t("コピーのエラー"), stopped: _t("コピーを停止") };
const KNOWLEDGE_DELETE_LABELS = { queued: _t("削除待ち"), running: _t("削除中"), done: _t("削除完了"), error: _t("削除のエラー"), stopped: _t("削除を取りやめ") };
// 参照の状態（admin/knowledge.py の _reference_dict）。
const KNOWLEDGE_REF_LABELS = { ok: _t("検索できます"), incompatible: _t("検索できません（設定が違います）"), missing: _t("検索できません（参照元がありません）") };
// 自動取り込みの曜日（catalog.sqlite の schedule_weekdays。0=月〜6=日、Python の weekday() と同じ）。
const KNOWLEDGE_WEEKDAYS = [_t("月"), _t("火"), _t("水"), _t("木"), _t("金"), _t("土"), _t("日")];

// コレクション一覧の並べ替え。列ごとの並べ替えの値（null の列は並べ替えない。値が null・空の行は向きによらず末尾）。
// 状態は、エラー → 取り込み中 → 待機中 → 停止 → 完了 → ジョブなし の順を昇順とする。
const KNOWLEDGE_JOB_STATE_ORDER = ["error", "running", "queued", "stopped", "done"];
const knowledgeTimeValue = (iso) => (iso ? Date.parse(iso) || null : null);
const KNOWLEDGE_SORT_VALUES = [
  (c) => c.name,
  null,
  (c) => (c.job ? KNOWLEDGE_JOB_STATE_ORDER.indexOf(c.job.state) : null),
  (c) => c.source_folders[0] || null,
  (c) => c.file_count,
  (c) => c.model || null,
  (c) => knowledgeTimeValue(c.last_ingested_at),
  (c) => (c.schedule_enabled ? knowledgeTimeValue(c.next_scheduled_at) : null),
];
// 並べ替えの状態はブラウザに保存し、画面の描き直し（取り込み中の自動更新）や再読み込みでも保つ。
// 列の順番を変えたときは、保存済みの列番号が別の列を指さないようキーを変える。
const KNOWLEDGE_SORT_STORAGE_KEY = "la-knowledge-sort-v2";

function knowledgeLoadSort() {
  try {
    const s = JSON.parse(localStorage.getItem(KNOWLEDGE_SORT_STORAGE_KEY) || "null");
    if (s && KNOWLEDGE_SORT_VALUES[s.index] && (s.dir === "asc" || s.dir === "desc")) return s;
  } catch (e) {
    // 壊れた値は無視して既定の並び（コレクションの登録順）に戻す。
  }
  return { index: null, dir: "asc" };
}

function knowledgeSortCollections(collections, sort) {
  const value = KNOWLEDGE_SORT_VALUES[sort.index];
  if (!value) return collections;
  const sign = sort.dir === "asc" ? 1 : -1;
  const isEmpty = (v) => v === null || v === undefined || v === -1;
  return [...collections].sort((a, b) => {
    const va = value(a);
    const vb = value(b);
    if (isEmpty(va) || isEmpty(vb)) return isEmpty(va) - isEmpty(vb);
    const cmp = typeof va === "string" ? va.localeCompare(vb, I18N_LOCALE, { numeric: true, sensitivity: "base" }) : va - vb;
    return cmp * sign || a.name.localeCompare(b.name, I18N_LOCALE, { numeric: true });
  });
}

function knowledgeJobActive(job) {
  return !!job && (job.state === "queued" || job.state === "running");
}

// 送信中のリクエストがあるのに、この秒数より長く埋め込みが進まないファイルを「詰まっている」として目立たせる。
const KNOWLEDGE_STALL_SECONDS = 60;

function knowledgeFileStalled(f) {
  return f.requests > 0 && f.idle_seconds >= KNOWLEDGE_STALL_SECONDS;
}

// 準備の枠（全インスタンス共通の並列数の枠。admin/knowledge.py の IngestSlots.snapshot() の load）のアイコン。
// 空きは灰の輪、準備中（読み取り・抽出・画像化）は回る輪（admin.css の .kn-worker）。1アイコン＝1ファイル。
function knowledgeLoadIcon(slot, mine) {
  if (!slot) return el("span", { class: "kn-worker", title: _t("準備: 空き") });
  const where = slot.instance ? `${slot.instance}/${slot.collection}\n` : "";
  return el("span", { class: `kn-worker load${mine ? "" : " other"}`, title: `${_t("準備")}: ${where}${slot.detail}` });
}

// 埋め込み中のファイル（snapshot の files。src/vector_db/ingest.py の _Pipeline.embedding_files）のアイコン。1アイコン＝1ファイル。
// そのファイルの埋め込み済みの割合だけ緑で塗り（--kn-progress）、リクエストを送信中なら脈打つ。送信中なのに
// KNOWLEDGE_STALL_SECONDS 秒より長く進まないファイルは警告色（.stalled）。
function knowledgeFileIcon(f, mine) {
  const where = f.instance ? `${f.instance}/${f.collection}\n` : "";
  const lines = [
    `${_t("埋め込み")}: ${where}${f.detail}`,
    _t(`埋め込み済み {0}/{1} 件・送信中のリクエスト {2}`, f.done, f.total, f.requests),
    _t(`最後に進んでから {0} 秒`, Math.round(f.idle_seconds)),
  ];
  const state = knowledgeFileStalled(f) ? " stalled" : f.requests > 0 ? "" : " idle";
  const icon = el("span", { class: `kn-worker embed${state}${mine ? "" : " other"}`, title: lines.join("\n") });
  icon.style.setProperty("--kn-progress", `${f.total ? Math.round((100 * f.done) / f.total) : 0}%`);
  return icon;
}

// 埋め込みの段階のファイルのうち、アイコンで出す limit 個（取り込みの並列数）。まだ1件も送っていないものは出さない。
// 画像ファイルのパス等は多くのファイルを1つのバッチにまとめて送るため、送信中・一部済みのファイルは並列数より多くなる。
// 詰まっているもの → 送信中のリクエストが多いもの → 長く進んでいないものの順に選ぶ。
function knowledgeEmbedFiles(files, limit) {
  return files
    .filter((f) => f.requests > 0 || f.done > 0)
    .sort((a, b) => knowledgeFileStalled(b) - knowledgeFileStalled(a) || b.requests - a.requests || b.idle_seconds - a.idle_seconds)
    .slice(0, limit);
}

// 送信中の埋め込みのリクエスト数のメーター（縦のセグメントを並べ、使っている分を光らせる）。admin.css の .kn-meter。
// セグメントは上限の数だけ並べるが、KNOWLEDGE_METER_MAX_SEGMENTS 本までにする（並列数 50 等でも幅が増えないように）。
// それを超える上限では、使っている割合の本数を光らせる。実際の数は右の「使用数/上限」の文字で出す。
const KNOWLEDGE_METER_MAX_SEGMENTS = 10;

function knowledgeRequestMeter(used, capacity) {
  const title = _t("同時に送っている埋め込みのリクエストの数 / 並列数（管理者設定の「埋め込みのリクエストの並列数」）");
  const cap = Math.max(capacity || 0, used);
  const segments = Math.min(cap, KNOWLEDGE_METER_MAX_SEGMENTS);
  // 1件でも送っていれば1本は光らせる（割合が小さくて 0 本に丸まり、動いていないように見えないため）。
  const lit = cap ? Math.min(segments, used > 0 ? Math.max(1, Math.round((segments * used) / cap)) : 0) : 0;
  const bar = el("span", { class: "kn-meter" }, Array.from({ length: segments }, (_, i) => el("span", { class: i < lit ? "on" : "" })));
  return el("span", { class: "kn-workers-count kn-request-meter", title }, [
    el("span", { class: "kn-workers-label", text: _t("リクエスト") }),
    bar,
    el("span", { class: "kn-meter-text", text: `${used}/${cap}` }),
  ]);
}

// 全インスタンス共通の取り込みワーカーの状態（このインスタンス以外の取り込みの分は薄く出す）。
function knowledgeWorkersPanel(workers, instance) {
  if (!workers) return el("div");
  const used = workers.embed.filter(Boolean).length;
  const active = knowledgeEmbedFiles(workers.files || [], workers.workers);
  // アイコンの数は取り込みの並列数（ingest_workers）。足りない分は空きの輪で埋める。
  const empty = Array.from({ length: Math.max(0, workers.workers - active.length) }, () =>
    el("span", { class: "kn-worker", title: _t("埋め込み: 空き") })
  );
  const title = workers.processes
    ? _t(`取り込みワーカー（全インスタンス共通。並列数 {0}・子プロセス {1}）`, workers.workers, workers.processes)
    : _t(`取り込みワーカー（全インスタンス共通。並列数 {0}）`, workers.workers);
  const embedGroup = el("span", { class: "kn-workers" }, [
    el("span", { class: "kn-workers-label", text: _t("埋め込み") }),
    ...active.map((f) => knowledgeFileIcon(f, f.instance === instance)),
    ...empty,
    knowledgeRequestMeter(used, workers.requests),
  ]);
  const parts = [
    el("span", { text: title }),
    el("span", { class: "kn-workers" }, [
      el("span", { class: "kn-workers-label", text: _t("準備") }),
      ...workers.load.map((s) => knowledgeLoadIcon(s, !!s && s.instance === instance)),
    ]),
    embedGroup,
  ];
  if (workers.waiting.length) {
    const names = workers.waiting.map((w) => `${w.instance}/${w.collection}`).join(", ");
    parts.push(
      el("span", { class: "kn-workers", title: _t(`空きを待っている取り込み: {0}`, names) }, [
        el("span", { class: "kn-worker waiting" }),
        el("span", { text: _t(`空き待ち {0}`, workers.waiting.length) }),
      ])
    );
  }
  return el("div", { class: "kn-workers-panel" }, parts);
}

// snapshot のうち instance/collection の取り込みの分（admin/knowledge.py の job_workers と同じ形）。
function knowledgeJobWorkers(workers, instance, collection) {
  if (!workers) return null;
  const mine = (s) => !!s && s.instance === instance && s.collection === collection;
  const strip = ({ instance: _i, collection: _c, ...rest }) => rest;
  return {
    load: workers.load.filter(mine).map(strip),
    files: (workers.files || []).filter(mine).map(strip),
    requests: workers.embed.filter(mine).length,
    slots: workers.workers,
    request_slots: workers.requests,
    waiting: workers.waiting.some(mine),
  };
}

// 1つの取り込みの準備中・埋め込み中のファイルのアイコン（インスタンス一覧のカードでも使う）。
function knowledgeJobWorkerIcons(w) {
  if (!w) return null;
  const active = knowledgeEmbedFiles(w.files || [], w.slots);
  const icons = [...w.load.map((s) => knowledgeLoadIcon(s, true)), ...active.map((f) => knowledgeFileIcon(f, true))];
  if (w.requests) icons.push(knowledgeRequestMeter(w.requests, w.request_slots));
  if (w.waiting) icons.push(el("span", { class: "kn-worker waiting", title: _t("ほかの取り込みが並列数の枠を使っているため、空きを待っています") }));
  if (!icons.length) return null;
  return el("div", { class: "kn-workers kn-workers-row" }, icons);
}

function knowledgeWorkersBusy(workers) {
  return !!workers && (workers.load.some(Boolean) || workers.embed.some(Boolean) || (workers.files || []).length > 0 || workers.waiting.length > 0);
}

async function knowledgeAction(fn) {
  try {
    return await fn();
  } catch (e) {
    alert(e.message);
    return null;
  }
}

async function renderKnowledgePanel(container, name) {
  stopMonitorPolling();
  container.innerHTML = "";
  const data = await api(knowledgePath(name));

  const settings = [
    _t(`機能: {0}`, data.enabled ? _t("有効") : _t("無効")),
    _t(`保存先: {0}、距離関数 {1}`, data.backend === "qdrant" ? _t(`Qdrant（{0}、接頭辞 {1}、プリセット {2}）`, data.qdrant_url || _t("URL 未設定"), data.qdrant_collection_prefix, data.qdrant_preset) : _t(`sqlite-vec（{0}）`, data.dir), data.distance),
    _t(`埋め込みモデル: {0}`, data.embedding_model || _t("未設定")),
    _t(`接頭辞: 検索語 {0}・文書 {1}`, JSON.stringify(data.query_prefix), JSON.stringify(data.document_prefix)),
    _t(`対象の拡張子: {0}`, data.include_extensions.join(" ")),
    _t(`埋め込み方: {0}`, data.image_input ? (data.embedded_images
      ? _t("本文＋画像（image_input = true。{0} はページ画像も、ほかの Office ファイルは中の画像も、画像ファイルは画像で埋め込む）", (data.image_input_extensions || []).join(" ") || _t("（なし）"))
      : _t("本文＋画像（image_input = true。{0} はページ画像も、画像ファイルは画像で埋め込む）", (data.image_input_extensions || []).join(" ") || _t("（なし）"))) : _t("テキスト")),
  ];
  container.append(el("h3", { text: _t("ナレッジ（社内文書の意味検索）") }));
  container.append(el("p", { class: "hint", text: settings.join(" / ") }));
  if (!data.enabled) {
    container.append(
      el("p", {
        class: "error",
        text: _t("このインスタンスでは [vector_db] enabled が false です。config.ini タブで enabled・embedding_url（Qdrant を使う場合は backend・qdrant_url も）を設定してから取り込んでください。コレクションの登録だけは先にできます。"),
      })
    );
  }

  const workersBox = el("div");
  container.append(workersBox);
  const list = el("div");
  container.append(list);
  container.append(buildCreateCollectionForm(container, name));
  container.append(buildReferencesSection(container, name, data.references));

  let sort = knowledgeLoadSort();
  let latestData = data;
  // 同じ列をもう一度押すと向きを逆にし、3回目で既定の並び（登録順）に戻す。
  const onSort = (index) => {
    if (sort.index !== index) sort = { index, dir: "asc" };
    else if (sort.dir === "asc") sort = { index, dir: "desc" };
    else sort = { index: null, dir: "asc" };
    localStorage.setItem(KNOWLEDGE_SORT_STORAGE_KEY, JSON.stringify(sort));
    draw(latestData);
  };

  const draw = (d) => {
    latestData = d;
    const collections = knowledgeSortCollections(d.collections, sort);
    workersBox.replaceChildren(knowledgeWorkersPanel(d.workers, name));
    list.innerHTML = "";
    const rows = collections.map((c) => [
      el("div", {}, [el("strong", { text: c.name }), c.description ? el("div", { class: "hint", text: c.description }) : null]),
      knowledgeActions(container, name, c, data.enabled),
      knowledgeJobCell(c.job, data.max_file_mb, knowledgeJobWorkers(d.workers, name, c.name)),
      el("div", {}, [
        ...c.source_folders.map((p) => el("div", { text: p })),
        el("div", { class: "hint", text: c.recursive ? _t("サブフォルダも含む") : _t("直下のみ") }),
        c.include_keywords.length ? el("div", { class: "hint", text: _t("対象: {0}", c.include_keywords.join(", ")) }) : null,
        c.exclude_keywords.length ? el("div", { class: "hint", text: _t("除外: {0}", c.exclude_keywords.join(", ")) }) : null,
      ]),
      _t(`{0} 件 / {1} チャンク`, c.file_count, c.chunk_count),
      c.model ? _t(`{0}（{1} 次元）`, c.model, c.dim) : "-",
      fmtTime(c.last_ingested_at),
      knowledgeScheduleCell(c, data.enabled),
    ]);
    list.append(
      dataTable([_t("コレクション"), _t("操作"), _t("状態"), _t("取り込み元フォルダ"), _t("文書"), _t("埋め込みモデル"), _t("最終取り込み"), _t("自動取り込み")], rows, _t("まだコレクションはありません。"), {
        sortable: KNOWLEDGE_SORT_VALUES.map(Boolean),
        index: sort.index,
        dir: sort.dir,
        onSort,
      })
    );
    // コピーを始めた直後（コピー先の定義ができる前）と、定義を作れずに終わったコピーのジョブ。
    for (const job of d.copy_jobs || []) {
      list.append(el("div", { class: "hint" }, [_t(`{0}（{1} から）: `, job.collection, job.source), knowledgeJobCell(job, d.max_file_mb, null)]));
    }
  };
  draw(data);

  // 取り込み・コピー・削除中（順番待ちを含む）のジョブか、ほかのインスタンスの取り込みが枠を使っている間（と、
  // 終わった直後の1回）だけ表示を更新する（編集中の追加フォームを描き直さないよう、一覧の表と枠の表示だけを差し替える）。
  // ポーリングはジョブと枠の状態だけを返す軽い API で行い、ジョブが始まった・終わったときと KNOWLEDGE_FULL_REFRESH_MS ごとに
  // だけ一覧（ファイル数の集計・ほかのインスタンスの参照を読むため重い）を取り直す。状態が前回と同じなら描き直さない。
  const hasRunning = (d) =>
    d.collections.some((c) => knowledgeJobActive(c.job)) || (d.copy_jobs || []).some(knowledgeJobActive) || knowledgeWorkersBusy(d.workers);
  let wasRunning = hasRunning(data);
  let activeKey = knowledgeActiveJobsKey(knowledgeDataJobs(data));
  let lastStatus = "";
  let lastFull = Date.now();
  startMonitorPolling(container, async () => {
    if (!wasRunning) return;
    const status = await api(knowledgePath(name, "/jobs"));
    let latest;
    // 取り込み中も、文書の件数（記録まで終わったファイル）が進み具合とずれ続けないよう、一覧を間隔を空けて取り直す。
    if (knowledgeActiveJobsKey(Object.values(status.jobs)) !== activeKey || Date.now() - lastFull >= KNOWLEDGE_FULL_REFRESH_MS) {
      latest = await api(knowledgePath(name));
      lastFull = Date.now();
      lastStatus = "";
    } else {
      const sig = JSON.stringify(status);
      if (sig === lastStatus) return;
      lastStatus = sig;
      latest = knowledgeMergeJobs(latestData, status);
    }
    activeKey = knowledgeActiveJobsKey(knowledgeDataJobs(latest));
    wasRunning = hasRunning(latest);
    draw(latest);
  });
}

// 取り込み中に一覧（ファイル数の集計を含む重い API）を取り直す間隔（ミリ秒）。間のポーリングはジョブと枠の状態だけを取る。
const KNOWLEDGE_FULL_REFRESH_MS = 10000;

// 一覧（GET /api/instances/{name}/knowledge）に含まれるジョブ。
function knowledgeDataJobs(d) {
  return [...d.collections.map((c) => c.job).filter(Boolean), ...(d.copy_jobs || [])];
}

// 動いている（順番待ちを含む）ジョブの組み合わせ。変わったら（始まった・終わった）一覧を取り直す。
function knowledgeActiveJobsKey(jobs) {
  return jobs
    .filter(knowledgeJobActive)
    .map((j) => `${j.kind}:${j.collection}:${j.state}`)
    .sort()
    .join("|");
}

// 一覧 d のジョブ・枠の状態を、軽い API（GET /api/instances/{name}/knowledge/jobs）の status で置き換える。
function knowledgeMergeJobs(d, status) {
  const names = new Set(d.collections.map((c) => c.name));
  return {
    ...d,
    collections: d.collections.map((c) => ({ ...c, job: status.jobs[c.name] || null })),
    copy_jobs: Object.values(status.jobs).filter((j) => j.kind === "copy" && !names.has(j.collection)),
    workers: status.workers,
  };
}

function knowledgeScheduleText(c) {
  const days = c.schedule_weekdays.length ? c.schedule_weekdays.map((d) => KNOWLEDGE_WEEKDAYS[d]).join(_t("・")) : _t("毎日");
  return `${days} ${c.schedule_times.join(", ")}`;
}

function knowledgeScheduleCell(c, enabled) {
  if (!c.schedule_enabled) return _t("なし");
  const parts = [el("div", { text: knowledgeScheduleText(c) })];
  if (!enabled) parts.push(el("div", { class: "hint", text: _t("[vector_db] enabled が false のため実行しません") }));
  else if (c.next_scheduled_at) parts.push(el("div", { class: "hint", text: _t(`次回 {0}`, fmtTime(c.next_scheduled_at)) }));
  return el("div", {}, parts);
}

// workers: このジョブが使っている取り込みの枠（knowledgeJobWorkers）。
function knowledgeJobCell(job, maxFileMb, workers) {
  if (!job) return "-";
  // 停止を求めた後は、処理中のファイルが終わるまで running のままのため「停止中」と出す。
  const labels = job.kind === "copy" ? KNOWLEDGE_COPY_LABELS : job.kind === "delete" ? KNOWLEDGE_DELETE_LABELS : KNOWLEDGE_JOB_LABELS;
  const label = job.state === "running" && job.stop_requested ? _t("停止中") : labels[job.state] || job.state;
  const parts = [el("strong", { text: job.trigger === "schedule" ? _t(`{0}（自動）`, label) : label })];
  if (job.kind === "copy" && job.source) parts.push(el("div", { class: "hint", text: _t(`コピー元 {0}`, job.source) }));
  if (job.state === "queued") {
    parts.push(
      el("div", {
        class: "hint",
        text: job.kind === "delete"
          ? _t("同じインスタンスの取り込み・コピーが終わってから削除します。")
          : _t("同じインスタンスのコレクションの削除が終わってから始めます。"),
      })
    );
  }
  if (job.state === "running" && job.kind !== "delete") {
    const bar = knowledgeProgressBar(job.done, job.total);
    if (job.kind === "ingest") {
      // 1秒当たりの取り込み容量（admin/knowledge.py の IngestJob.bytes_per_second。直近 30 秒ほどの平均）。
      const rate = el("span", { class: "kn-rate", text: `${fmtBytes(Math.round(job.bytes_per_second || 0))}/s` });
      parts.push(el("div", { class: "kn-progress-row" }, [bar, rate]));
    } else {
      parts.push(bar);
    }
    parts.push(el("div", { class: "hint", text: _t(`{0} / {1} ファイル`, job.done, job.total || "?") }));
    parts.push(knowledgeJobWorkerIcons(workers));
    // 取り込み中のファイルはワーカーのアイコンにカーソルを合わせると出るため、文字では出さない（コピーは枠を使わないため出す）。
    if (job.current && job.kind === "copy") parts.push(el("div", { class: "hint", text: job.current }));
  }
  if (job.result && job.result.kind === "copy") {
    const r = job.result;
    parts.push(
      el("div", {
        class: "hint",
        text: r.with_vectors
          ? _t(`{0} / {1} ファイル（{2} チャンク）をコピーしました。`, r.files, r.total, r.chunks)
          : _t("定義だけをコピーしました。「取り込み（差分）」で文書を取り込んでください。"),
      })
    );
    if (r.stopped) parts.push(el("div", { class: "hint", text: _t("途中で止めました。残りのファイルは「取り込み（差分）」で取り込めます。") }));
  } else if (job.result) {
    const r = job.result;
    parts.push(el("div", { class: "hint", text: _t(`追加 {0} / 更新 {1} / 変更なし {2} / 削除 {3} / 失敗 {4}`, r.added, r.updated, r.unchanged, r.removed, r.failed.length) }));
    // 中身を埋め込まずにファイル名・フォルダ名だけで探せるようにしたファイル（画像ファイル・文字の無い PDF 等）。
    if (r.path_only) {
      parts.push(
        el("div", {
          class: "hint",
          text:
            _t(`うち {0} 件は中身を取り出せない（[vector_db] image_input = false の画像ファイル・文字の無い PDF 等）ため、`, r.path_only) +
            _t("ファイル名・フォルダ名だけを登録しました（search_document_paths で探せます）。"),
        })
      );
    }
    if (r.failed.length) {
      const details = el("details", {}, [el("summary", { text: _t("失敗したファイル") })]);
      for (const f of r.failed) details.append(el("div", { class: "hint", text: `${f.path}: ${f.reason}` }));
      parts.push(details);
    }
    // 上限サイズ超えは設定で対処できるため、失敗一覧を開かなくても見えるように対処方法を出す
    // （src/vector_db/ingest.py の _too_large_hint と同じ内容）。
    if (r.too_large) {
      parts.push(
        el("div", {
          class: "warn-box",
          text:
            _t(`{0} 件は上限サイズ（[vector_db] max_file_mb = {1}MB。Locohane 側の制限）を超えたため取り込んでいません。`, r.too_large, maxFileMb) +
            _t("設定ダッシュボードの「管理者設定」で max_file_mb を大きくしてから「取り込み（差分）」を実行し直すと取り込めます。"),
        })
      );
    }
  }
  // ファイル数の上限超えも設定で対処できるため、上限の変え方まで出す（src/vector_db/ingest.py の _over_limit_hint と同じ内容）。
  if (job.result && job.result.over_limit) {
    const r = job.result;
    parts.push(
      el("div", {
        class: "warn-box",
        text:
          _t(`取り込み元に {0} 件のファイルがあり、1コレクションの上限（config.ini [vector_db] max_files_per_collection = {1}）を`, r.found_files, r.max_files) +
          _t(`超えたため、{0} 件は取り込んでいません（取り込み済みのファイルを優先し、残りはパス順の先頭から選んでいます）。`, r.over_limit) +
          _t("設定ダッシュボードの「管理者設定」で max_files_per_collection を大きくしてから「取り込み（差分）」を実行し直すと取り込めます") +
          _t("（取り込み中は対象のファイルの一覧をメモリーに持つため、管理ツールの PC のメモリーに注意してください）。") +
          _t("コレクションを取り込み元のフォルダごとに分けることもできます。"),
      })
    );
  }
  if (job.error) parts.push(el("div", { class: "error", text: job.error }));
  if (job.finished_at) parts.push(el("div", { class: "hint", text: _t(`終了 {0}`, fmtTime(job.finished_at)) }));
  return el("div", {}, parts);
}

// 取り込みの進捗バー。対象ファイルを数え終わるまで（total が 0）は割合が出せないため、流れる表示にする。
function knowledgeProgressBar(done, total) {
  if (!total) return el("div", { class: "kn-progress indeterminate" }, [el("div", { class: "kn-progress-fill" })]);
  const pct = Math.min(100, Math.floor((done / total) * 100));
  return el("div", { class: "kn-progress", title: `${pct}%` }, [
    el("div", { class: "kn-progress-fill", style: `width: ${pct}%` }),
    el("span", { class: "kn-progress-text", text: `${pct}%` }),
  ]);
}

function knowledgeActions(container, name, c, enabled) {
  const running = knowledgeJobActive(c.job);
  const path = `/collections/${encodeURIComponent(c.name)}`;
  const buttons = [];
  if (running && c.job.kind === "delete" && c.job.state === "running") {
    const busy = el("button", { text: _t("削除中…") });
    busy.disabled = true;
    buttons.push(busy);
  } else if (running) {
    // 順番待ちの削除は「削除を取りやめ」（すぐ取りやめる）。取り込み・コピーの停止は処理中のファイルが終わるまで待つ。
    const idleText = c.job.kind === "delete" ? _t("削除を取りやめ") : _t("停止");
    // 停止はすぐには効かない（処理中のファイルの埋め込みを待つ）ため、押したらその場で「停止中…」にして押せなくする。
    const stopBtn = el("button", {
      text: c.job.stop_requested ? _t("停止中…") : idleText,
      onclick: async () => {
        stopBtn.disabled = true;
        stopBtn.textContent = _t("停止中…");
        if (await knowledgeAction(() => api(knowledgePath(name, `${path}/stop`), { method: "POST" }))) renderKnowledgePanel(container, name);
        else {
          stopBtn.disabled = false;
          stopBtn.textContent = idleText;
        }
      },
    });
    stopBtn.disabled = !!c.job.stop_requested;
    buttons.push(stopBtn);
  } else {
    const ingest = (rebuild) => async () => {
      if (rebuild && !confirm(_t(`{0} のベクターをすべて消して、全ファイルを取り込み直します。よろしいですか？（埋め込みモデルを変えたときに必要です）`, c.name))) return;
      if (await knowledgeAction(() => api(knowledgePath(name, `${path}/ingest`), { method: "POST", body: { rebuild } }))) renderKnowledgePanel(container, name);
    };
    const ingestBtn = el("button", { class: "primary", text: _t("取り込み（差分）"), onclick: ingest(false) });
    const rebuildBtn = el("button", { text: _t("再構築"), onclick: ingest(true) });
    ingestBtn.disabled = !enabled;
    rebuildBtn.disabled = !enabled;
    buttons.push(ingestBtn, rebuildBtn);
    buttons.push(el("button", { text: _t("編集"), onclick: () => openEditCollectionForm(container, name, c) }));
    buttons.push(el("button", { text: _t("コピー"), onclick: () => openCopyCollectionDialog(container, name, c) }));
    // 削除はベクターの削除に時間がかかることがあるため、停止と同じく押したらその場で「削除中…」にして押せなくする。
    // 削除中に取り込み・編集・コピーが走らないよう、同じ行のボタンもすべて押せなくする（失敗したら元の状態に戻す）。
    const deleteBtn = el("button", {
      class: "btn-danger",
      text: _t("削除"),
      onclick: async () => {
        let message = _t(`コレクション {0} と取り込み済みのベクターを削除します（取り込み元の文書は消えません）。よろしいですか？`, c.name);
        if (c.referenced_by && c.referenced_by.length) {
          message += "\n" + _t(`このコレクションは {0} から参照されています（削除すると参照している側では検索できなくなります）。`, c.referenced_by.join(_t("、")));
        }
        if (!confirm(message)) return;
        const wasDisabled = buttons.map((b) => b.disabled);
        buttons.forEach((b) => (b.disabled = true));
        deleteBtn.textContent = _t("削除中…");
        if (await knowledgeAction(() => api(knowledgePath(name, path), { method: "DELETE" }))) renderKnowledgePanel(container, name);
        else {
          buttons.forEach((b, i) => (b.disabled = wasDisabled[i]));
          deleteBtn.textContent = _t("削除");
        }
      },
    });
    buttons.push(deleteBtn);
  }
  return el("div", { class: "inline-actions" }, buttons);
}

function collectionFields(c) {
  const weekdays = KNOWLEDGE_WEEKDAYS.map((label, i) => {
    const box = el("input", { type: "checkbox" });
    box.checked = !!c && c.schedule_weekdays.includes(i);
    return { index: i, label, box };
  });
  const scheduleEnabled = el("input", { type: "checkbox" });
  scheduleEnabled.checked = !!c && c.schedule_enabled;
  // 取り込み元フォルダは1行に1つ（複数指定できる）。
  const folder = el("textarea", { rows: 3, placeholder: _t("例: D:\\shared\\manuals（管理ツールの PC から見える絶対パス。複数は1行に1つ）") });
  folder.value = c ? c.source_folders.join("\n") : "";
  // 取り込み除外キーワードも1行に1つ。
  const excludes = el("textarea", { rows: 2, placeholder: _t("例: 旧版（1行に1つ。パス・フォルダ名・ファイル名に含むものを除外）") });
  excludes.value = c ? c.exclude_keywords.join("\n") : "";
  // 取り込み対象キーワードも1行に1つ（空なら絞り込まない）。
  const includes = el("textarea", { rows: 2, placeholder: _t("例: *.pdf（1行に1つ。空ならすべて。パス・フォルダ名・ファイル名に含むものだけを取り込む）") });
  includes.value = c ? c.include_keywords.join("\n") : "";
  return {
    description: el("input", { type: "text", value: c ? c.description : "", placeholder: _t("例: 品質管理規程・手順書") }),
    folder,
    excludes,
    includes,
    recursive: el("input", { type: "checkbox" }),
    scheduleEnabled,
    scheduleTimes: el("input", { type: "text", value: c ? c.schedule_times.join(", ") : "", placeholder: _t("例: 02:00（複数はカンマ区切り。例: 07:30, 19:00）") }),
    weekdays,
  };
}

// 自動取り込みの入力欄（追加フォームと編集ダイアログで共通）。
function scheduleFieldElements(f) {
  const toggle = () => {
    f.scheduleTimes.disabled = !f.scheduleEnabled.checked;
    for (const w of f.weekdays) w.box.disabled = !f.scheduleEnabled.checked;
  };
  f.scheduleEnabled.addEventListener("change", toggle);
  toggle();
  return [
    el("label", { class: "checkbox" }, [f.scheduleEnabled, _t(" 時間指定で自動取り込み（差分）を行う")]),
    el("label", {}, [_t("時刻（24時間表記）"), f.scheduleTimes]),
    el("div", {}, [
      el("div", { text: _t("曜日（どれも選ばなければ毎日）") }),
      el(
        "div",
        { class: "inline-actions" },
        f.weekdays.map((w) => el("label", { class: "checkbox" }, [w.box, ` ${w.label}`]))
      ),
    ]),
    el("p", { class: "hint", text: _t("管理ツール（admin.bat）が起動している間だけ実行します。止まっていた間の時刻の分は後から実行しません。") }),
  ];
}

function lineList(textarea) {
  return textarea.value.split(/\r?\n/).map((s) => s.trim()).filter((s) => s);
}

function folderList(f) {
  return lineList(f.folder);
}

// 取り込み対象・除外キーワードの入力欄（追加フォームと編集ダイアログで共通）。
function keywordFieldElements(f) {
  return [
    el("label", {}, [_t("取り込み対象キーワード"), f.includes]),
    el("p", {
      class: "hint",
      text: _t("書いた場合は、取り込み元フォルダより下のパス（フォルダ名・ファイル名を含む）にどれかのキーワードを含むファイルだけを取り込みます。書き方は除外キーワードと同じで、除外キーワードにも当てはまるファイルは取り込みません。"),
    }),
    el("label", {}, [_t("取り込み除外キーワード"), f.excludes]),
    el("p", {
      class: "hint",
      text: _t("取り込み元フォルダより下のパス（フォルダ名・ファイル名を含む）にキーワードを含むものは取り込みません（大文字小文字は区別しない）。/old/ のように / で囲むと、名前がちょうど old のフォルダだけを除外します。*.xlsx のように *. で始めると、その拡張子のファイルだけを除外します。"),
    }),
  ];
}

function scheduleBody(f) {
  return {
    schedule_enabled: f.scheduleEnabled.checked,
    schedule_times: f.scheduleTimes.value.split(/[,、\s]+/).filter((t) => t),
    schedule_weekdays: f.weekdays.filter((w) => w.box.checked).map((w) => w.index),
  };
}

function buildCreateCollectionForm(container, name) {
  const nameInput = el("input", { type: "text", placeholder: _t("例: manuals（半角英数字・_・- の48文字以内）"), maxlength: "48" });
  const f = collectionFields(null);
  f.recursive.checked = true;
  const error = el("div", { class: "error" });
  const form = el("form", { class: "knowledge-form" }, [
    el("h3", { text: _t("コレクションを追加") }),
    el("label", {}, [_t("コレクション名"), nameInput]),
    el("label", {}, [_t("説明（LLM にも一覧で見せる）"), f.description]),
    el("label", {}, [_t("取り込み元フォルダ"), f.folder]),
    el("label", { class: "checkbox" }, [f.recursive, _t(" サブフォルダも取り込む")]),
    ...keywordFieldElements(f),
    ...scheduleFieldElements(f),
    error,
    el("button", { type: "submit", class: "primary", text: _t("追加") }),
  ]);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    try {
      await api(knowledgePath(name, "/collections"), {
        method: "POST",
        body: {
          name: nameInput.value.trim(),
          description: f.description.value,
          source_folders: folderList(f),
          recursive: f.recursive.checked,
          exclude_keywords: lineList(f.excludes),
          include_keywords: lineList(f.includes),
          ...scheduleBody(f),
        },
      });
      renderKnowledgePanel(container, name);
    } catch (e) {
      error.textContent = e.message;
    }
  });
  return form;
}

function openEditCollectionForm(container, name, c) {
  const f = collectionFields(c);
  f.recursive.checked = c.recursive;
  const error = el("div", { class: "error" });
  const backdrop = el("div", { class: "modal-backdrop" });
  const close = () => backdrop.remove();
  const form = el("form", {}, [
    el("h3", { text: _t(`コレクション {0} を編集`, c.name) }),
    el("label", {}, [_t("説明"), f.description]),
    el("label", {}, [_t("取り込み元フォルダ"), f.folder]),
    el("label", { class: "checkbox" }, [f.recursive, _t(" サブフォルダも取り込む")]),
    el("p", { class: "hint", text: _t("フォルダを変えた場合、次の取り込みで新しいフォルダに無いファイルのベクターは削除されます。") }),
    ...keywordFieldElements(f),
    el("p", { class: "hint", text: _t("キーワードを変えて取り込み対象から外れた取り込み済みのファイルは、次の取り込みで削除されます。") }),
    ...scheduleFieldElements(f),
    error,
    el("div", { class: "modal-actions" }, [el("button", { type: "button", text: _t("キャンセル"), onclick: close }), el("button", { type: "submit", class: "primary", text: _t("保存") })]),
  ]);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    try {
      await api(knowledgePath(name, `/collections/${encodeURIComponent(c.name)}`), {
        method: "PUT",
        body: {
          description: f.description.value,
          source_folders: folderList(f),
          recursive: f.recursive.checked,
          exclude_keywords: lineList(f.excludes),
          include_keywords: lineList(f.includes),
          ...scheduleBody(f),
        },
      });
      close();
      renderKnowledgePanel(container, name);
    } catch (e) {
      error.textContent = e.message;
    }
  });
  backdrop.append(el("div", { class: "modal" }, form));
  document.body.append(backdrop);
}

// コピー先・参照元に選べるインスタンスとそのコレクション（admin/knowledge.py の /api/knowledge/sources）。
function knowledgeInstanceLabel(i) {
  return i.display_name && i.display_name !== i.name ? `${i.display_name}（${i.name}）` : i.name;
}

// コレクション c のベクターをコピー先 dest で使えない理由（admin/knowledge.py の check_compatible と同じ条件）。
function knowledgeCopyProblems(c, source, dest) {
  if (!c.model) return [_t("まだ取り込まれていません")];
  const problems = [];
  if (dest.embedding_model !== c.model) problems.push(_t(`埋め込みモデルが違います（コピー元 {0}、コピー先 {1}）`, c.model, dest.embedding_model || _t("未設定")));
  if (c.document_prefix !== null && c.document_prefix !== dest.document_prefix) {
    problems.push(_t(`document_prefix が違います（コピー元 {0}、コピー先 {1}）`, JSON.stringify(c.document_prefix), JSON.stringify(dest.document_prefix)));
  }
  if (source && source.distance !== dest.distance) problems.push(_t(`距離関数が違います（コピー元 {0}、コピー先 {1}）`, source.distance, dest.distance));
  return problems;
}

async function openCopyCollectionDialog(container, name, c) {
  const sources = await knowledgeAction(() => api("/api/knowledge/sources"));
  if (!sources) return;
  const instances = sources.instances;
  const source = instances.find((i) => i.name === name);
  const destSelect = el(
    "select",
    {},
    instances.map((i) => el("option", { value: i.name, text: knowledgeInstanceLabel(i) }))
  );
  const other = instances.find((i) => i.name !== name);
  if (other) destSelect.value = other.name;
  const nameInput = el("input", { type: "text", value: c.name, maxlength: "48" });
  const withVectors = el("input", { type: "checkbox" });
  const schedule = el("input", { type: "checkbox" });
  const compat = el("p", { class: "hint" });
  const update = () => {
    const dest = instances.find((i) => i.name === destSelect.value);
    const problems = knowledgeCopyProblems(c, source, dest);
    withVectors.checked = problems.length === 0;
    withVectors.disabled = problems.length > 0;
    compat.className = problems.length ? "warn-box" : "hint";
    compat.textContent = problems.length
      ? _t("ベクターはコピーできないため、定義（説明・取り込み元フォルダ等）だけをコピーします。コピー後にコピー先で「取り込み（差分）」を実行してください。") + " " + problems.join(_t("、"))
      : _t("ベクター・キーワード索引・取り込み済みファイルの記録までコピーします（コピー先で埋め込み直しません）。");
  };
  destSelect.addEventListener("change", update);
  update();
  const error = el("div", { class: "error" });
  const backdrop = el("div", { class: "modal-backdrop" });
  const close = () => backdrop.remove();
  const form = el("form", {}, [
    el("h3", { text: _t(`コレクション {0} をコピー`, c.name) }),
    el("label", {}, [_t("コピー先のインスタンス"), destSelect]),
    el("label", {}, [_t("コピー先のコレクション名（半角英数字・_・- の48文字以内）"), nameInput]),
    el("label", { class: "checkbox" }, [withVectors, _t(" ベクターごとコピーする")]),
    compat,
    el("label", { class: "checkbox" }, [schedule, _t(" 自動取り込みの設定も引き継ぐ")]),
    el("p", { class: "hint", text: _t("進捗はコピー先のインスタンスのナレッジ画面に出ます。コピー中はコピー元のコレクションの取り込み・削除はできません。") }),
    error,
    el("div", { class: "modal-actions" }, [el("button", { type: "button", text: _t("キャンセル"), onclick: close }), el("button", { type: "submit", class: "primary", text: _t("コピー") })]),
  ]);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    try {
      await api(knowledgePath(name, `/collections/${encodeURIComponent(c.name)}/copy`), {
        method: "POST",
        body: { dest_instance: destSelect.value, dest_name: nameInput.value.trim(), with_vectors: withVectors.checked, include_schedule: schedule.checked },
      });
      close();
      renderKnowledgePanel(container, name);
    } catch (e) {
      error.textContent = e.message;
    }
  });
  backdrop.append(el("div", { class: "modal" }, form));
  document.body.append(backdrop);
}

function knowledgeReferenceStatus(r) {
  const parts = [el("strong", { text: KNOWLEDGE_REF_LABELS[r.status] || r.status })];
  if (r.reason) parts.push(el("div", { class: r.status === "ok" ? "hint" : "error", text: r.reason }));
  return el("div", {}, parts);
}

function buildReferencesSection(container, name, references) {
  const section = el("div", { class: "knowledge-references" }, [
    el("h3", { text: _t("他のインスタンスのナレッジを参照") }),
    el("p", {
      class: "hint",
      text: _t("別のインスタンスのコレクションを、複製せずにこのインスタンスの検索対象に加えます。取り込み・更新は参照元のインスタンスで行います（このインスタンスと同じ埋め込みモデル・document_prefix で取り込まれている必要があります）。"),
    }),
  ]);
  const rows = (references || []).map((r) => [
    el("strong", { text: r.name }),
    `${r.source_instance} / ${r.source_collection}`,
    r.collection && r.collection.description ? r.collection.description : "-",
    r.collection ? _t(`{0} 件 / {1} チャンク`, r.collection.file_count, r.collection.chunk_count) : "-",
    r.collection ? fmtTime(r.collection.last_ingested_at) : "-",
    knowledgeReferenceStatus(r),
    el("button", {
      class: "btn-danger",
      text: _t("参照をやめる"),
      onclick: async () => {
        if (!confirm(_t(`参照 {0} をやめます（参照元のコレクションは消えません）。よろしいですか？`, r.name))) return;
        if (await knowledgeAction(() => api(knowledgePath(name, `/references/${encodeURIComponent(r.name)}`), { method: "DELETE" }))) renderKnowledgePanel(container, name);
      },
    }),
  ]);
  section.append(dataTable([_t("参照名"), _t("参照元（インスタンス / コレクション）"), _t("説明"), _t("文書"), _t("最終取り込み"), _t("状態"), _t("操作")], rows, _t("参照はありません。")));
  section.append(buildAddReferenceForm(container, name));
  return section;
}

function buildAddReferenceForm(container, name) {
  const instanceSelect = el("select", {});
  const collectionSelect = el("select", {});
  const nameInput = el("input", { type: "text", maxlength: "48", placeholder: _t("例: org_a_manuals（半角英数字・_・- の48文字以内）") });
  const error = el("div", { class: "error" });
  let instances = [];
  // 参照名は、利用者が書き換えるまで「インスタンス名_コレクション名」に追従させる。
  let nameEdited = false;
  nameInput.addEventListener("input", () => {
    nameEdited = true;
  });
  const suggestName = () => {
    if (!nameEdited && instanceSelect.value && collectionSelect.value) nameInput.value = `${instanceSelect.value}_${collectionSelect.value}`.slice(0, 48);
  };
  const fillCollections = () => {
    const i = instances.find((x) => x.name === instanceSelect.value);
    collectionSelect.innerHTML = "";
    for (const c of i ? i.collections : []) {
      collectionSelect.append(el("option", { value: c.name, text: c.description ? `${c.name}（${c.description}）` : c.name }));
    }
    suggestName();
  };
  instanceSelect.addEventListener("change", fillCollections);
  collectionSelect.addEventListener("change", suggestName);
  api("/api/knowledge/sources")
    .then((data) => {
      instances = data.instances.filter((i) => i.name !== name && i.collections.length);
      for (const i of instances) instanceSelect.append(el("option", { value: i.name, text: knowledgeInstanceLabel(i) }));
      if (!instances.length) error.textContent = _t("参照できるコレクションを持つ他のインスタンスがありません。");
      fillCollections();
    })
    .catch((e) => {
      error.textContent = e.message;
    });
  const form = el("form", { class: "knowledge-form" }, [
    el("h3", { text: _t("参照を追加") }),
    el("label", {}, [_t("参照元のインスタンス"), instanceSelect]),
    el("label", {}, [_t("参照元のコレクション"), collectionSelect]),
    el("label", {}, [_t("参照名（このインスタンスでのコレクション名。LLM にもこの名前で見せる）"), nameInput]),
    error,
    el("button", { type: "submit", class: "primary", text: _t("追加") }),
  ]);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    try {
      await api(knowledgePath(name, "/references"), {
        method: "POST",
        body: { name: nameInput.value.trim(), source_instance: instanceSelect.value, source_collection: collectionSelect.value },
      });
      renderKnowledgePanel(container, name);
    } catch (e) {
      error.textContent = e.message;
    }
  });
  return form;
}
