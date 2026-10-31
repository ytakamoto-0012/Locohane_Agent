"use strict";
/* 設定ダッシュボードの「データバックアップ」画面（上部のタブ）。
 * [admin] instances_dir と全インスタンスの [paths] common_data_dir を、日時つきの zip にまとめて保存先へ退避する。
 * API は admin/data_backup.py。設定は config.ini の [data_backup] が既定値で、ここで保存した値が優先する（全インスタンス共通）。
 * 曜日の表記（KNOWLEDGE_WEEKDAYS）・進捗バー（knowledgeProgressBar）は knowledge.js のものを使う。
 */

const DATA_BACKUP_JOB_LABELS = { running: _t("バックアップ中"), done: _t("完了"), error: _t("エラー"), stopped: _t("停止") };

function fmtBytes(n) {
  if (n === null || n === undefined) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${i ? v.toFixed(1) : v} ${units[i]}`;
}

// notice: 描き直したあと設定フォームの下に出す完了メッセージ（保存・既定に戻した直後に渡す）。
async function renderDataBackup(notice = "") {
  mainEl.innerHTML = "";
  const container = el("section", { class: "data-backup" });
  mainEl.append(container);
  const data = await api("/api/data-backup");

  container.append(el("h2", { text: _t("データバックアップ") }));
  container.append(
    el("p", {
      class: "hint",
      text:
        _t("インスタンスの設定フォルダ（{0}）と、全インスタンスのデータフォルダ（[paths] common_data_dir）を、日時つきの zip にまとめて保存先へ退避します。", data.instances_dir) +
        _t("データ本体はローカルディスクに置き（ファイル共有には置けません）、保存先にファイル共有（UNC パス）を指定する使い方を想定しています。") +
        _t("会話履歴・ナレッジ等の SQLite は使用中でも壊れないように写します。Qdrant のデータ（Qdrant サーバー側）は含みません。管理ツールが起動している間だけ自動で動きます。"),
    })
  );
  if (data.settings_error) container.append(el("p", { class: "error", text: _t("設定に誤りがあります: {0}", data.settings_error) }));

  const status = el("div");
  container.append(status);
  container.append(buildDataBackupForm(data, notice));
  const list = el("div");
  container.append(list);

  const draw = (d) => {
    status.replaceChildren(dataBackupStatus(d));
    list.replaceChildren(dataBackupList(d));
  };
  draw(data);

  let wasRunning = data.job && data.job.state === "running";
  startMonitorPolling(container, async () => {
    if (!wasRunning) return;
    const latest = await api("/api/data-backup");
    wasRunning = latest.job && latest.job.state === "running";
    draw(latest);
  });
}

function dataBackupStatus(d) {
  const box = el("div", { class: "data-backup-status" });
  const job = d.job;
  const running = job && job.state === "running";
  const head = [
    el("strong", { text: _t("状態: ") }),
    job ? (DATA_BACKUP_JOB_LABELS[job.state] || job.state) + (job.trigger === "schedule" ? _t("（自動）") : "") : _t("まだ実行していません"),
  ];
  box.append(el("div", {}, head));
  if (d.settings.enabled) box.append(el("div", { class: "hint", text: _t(`次回の自動バックアップ: {0}`, fmtTime(d.next_run_at)) }));
  else box.append(el("div", { class: "hint", text: _t("自動バックアップは無効です（「今すぐバックアップ」は使えます）。") }));
  if (running) {
    box.append(knowledgeProgressBar(job.done, job.total));
    box.append(el("div", { class: "hint", text: _t(`{0} / {1} ファイル`, job.done, job.total || "?") }));
    if (job.current) box.append(el("div", { class: "hint", text: job.current }));
  }
  if (job && job.result) {
    const r = job.result;
    box.append(
      el("div", {
        class: "hint",
        text: _t(`{0}（{1}・{2} ファイル・{3} 秒）`, r.file, fmtBytes(r.size), r.files, r.seconds) + (r.removed.length ? _t(`。古い {0} 世代を削除しました`, r.removed.length) : ""),
      })
    );
    const problems = [...r.failed.map((f) => `${f.path}: ${f.reason}`), ...r.warnings];
    if (problems.length) {
      const details = el("details", {}, [el("summary", { text: _t(`バックアップできなかったもの（{0} 件）`, problems.length) })]);
      for (const p of problems) details.append(el("div", { class: "hint", text: p }));
      box.append(details);
    }
  }
  if (job && job.error) box.append(el("div", { class: "error", text: job.error }));
  if (job && job.finished_at) box.append(el("div", { class: "hint", text: _t(`終了 {0}`, fmtTime(job.finished_at)) }));

  const actions = el("div", { class: "inline-actions" });
  if (running) {
    const stopBtn = el("button", {
      text: job.stop_requested ? _t("停止中…") : _t("停止"),
      onclick: async () => {
        stopBtn.disabled = true;
        stopBtn.textContent = _t("停止中…");
        try {
          await api("/api/data-backup/stop", { method: "POST" });
        } catch (e) {
          alert(e.message);
        }
        renderDataBackup();
      },
    });
    stopBtn.disabled = !!job.stop_requested;
    actions.append(stopBtn);
  } else {
    const runBtn = el("button", {
      class: "primary",
      text: _t("今すぐバックアップ"),
      onclick: async () => {
        runBtn.disabled = true;
        try {
          await api("/api/data-backup/run", { method: "POST" });
        } catch (e) {
          alert(e.message);
        }
        renderDataBackup();
      },
    });
    runBtn.disabled = !d.settings.dest_dir;
    actions.append(runBtn);
  }
  box.append(actions);
  return box;
}

function dataBackupList(d) {
  const section = el("div", {}, [el("h3", { text: _t("保存先のバックアップ") })]);
  if (d.list_error) section.append(el("p", { class: "error", text: d.list_error }));
  const rows = d.backups.map((b) => [b.name, fmtBytes(b.size), fmtTime(b.modified)]);
  section.append(dataTable([_t("ファイル"), _t("サイズ"), _t("作成日時")], rows, d.settings.dest_dir ? _t("まだバックアップはありません。") : _t("保存先が設定されていません。")));
  section.append(
    el("p", {
      class: "hint",
      text: _t("復元するときは、管理ツールとインスタンスを止めてから zip を展開し、instances/ を [admin] instances_dir へ、data/<インスタンス名>/ を各インスタンスの common_data_dir へ戻してください（manifest.json に元の場所が書いてあります）。"),
    })
  );
  return section;
}

function buildDataBackupForm(d, notice) {
  const s = d.settings;
  const sourceLabel = (key) => (d.sources[key] === "dashboard" ? _t("（この画面で設定）") : _t("（config.ini の既定値）"));
  const enabled = el("input", { type: "checkbox" });
  enabled.checked = s.enabled;
  const dest = el("input", { type: "text", value: s.dest_dir, placeholder: _t("例: \\\\fileserver\\share\\locohane_backup（絶対パス。ファイル共有も可）") });
  const times = el("input", { type: "text", value: s.times.join(", "), placeholder: _t("例: 02:00（複数はカンマ区切り）") });
  const weekdays = KNOWLEDGE_WEEKDAYS.map((label, i) => {
    const box = el("input", { type: "checkbox" });
    box.checked = s.weekdays.includes(i);
    return { index: i, label, box };
  });
  const keep = el("input", { type: "number", min: "1", value: String(s.keep) });
  const excludeWorkdir = el("input", { type: "checkbox" });
  excludeWorkdir.checked = s.exclude_workdir;
  const excludeLogs = el("input", { type: "checkbox" });
  excludeLogs.checked = s.exclude_logs;
  const exclude = el("textarea", { rows: 2, placeholder: _t("例: */skill_drafts_archive/*（1行に1つ。バックアップ元のフォルダからの相対パスに対する glob）") });
  exclude.value = s.exclude.join("\n");
  const toggle = () => {
    times.disabled = !enabled.checked;
    for (const w of weekdays) w.box.disabled = !enabled.checked;
  };
  enabled.addEventListener("change", toggle);
  toggle();

  const error = el("div", { class: "error" });
  const message = el("div", { class: "hint ok-text", text: notice });
  const saveBtn = el("button", { type: "submit", class: "primary", text: _t("保存") });
  const form = el("form", { class: "knowledge-form" }, [
    el("h3", { text: _t("設定") }),
    el("label", {}, [_t("保存先フォルダ"), sourceLabel("dest_dir"), dest]),
    el("p", { class: "hint", text: _t("バックアップ元（instances_dir・各インスタンスの common_data_dir）の中は指定できません。") }),
    el("label", { class: "checkbox" }, [enabled, _t(" 時間指定で自動バックアップを行う"), sourceLabel("enabled")]),
    el("label", {}, [_t("時刻（24時間表記）"), sourceLabel("times"), times]),
    el("div", {}, [
      el("div", { text: _t("曜日（どれも選ばなければ毎日）") + sourceLabel("weekdays") }),
      el("div", { class: "inline-actions" }, weekdays.map((w) => el("label", { class: "checkbox" }, [w.box, ` ${w.label}`]))),
    ]),
    el("label", {}, [_t("残す世代数"), sourceLabel("keep"), keep]),
    el("label", { class: "checkbox" }, [excludeWorkdir, _t(" 作業フォルダ（[default_workdir] dir）を除く"), sourceLabel("exclude_workdir")]),
    el("label", { class: "checkbox" }, [excludeLogs, _t(" ログ（[log] dir・[chat_log] dir・管理ツールのログ）を除く"), sourceLabel("exclude_logs")]),
    el("label", {}, [_t("追加の除外パターン"), sourceLabel("exclude"), exclude]),
    error,
    message,
    el("div", { class: "inline-actions" }, [
      saveBtn,
      el("button", {
        type: "button",
        text: _t("config.ini の既定に戻す"),
        onclick: async () => {
          if (!confirm(_t("この画面で保存した設定を消して、config.ini の [data_backup] の値に戻します。よろしいですか？"))) return;
          try {
            await api("/api/data-backup/settings", { method: "DELETE" });
            renderDataBackup(_t("config.ini の値に戻しました。"));
          } catch (e) {
            error.textContent = e.message;
          }
        },
      }),
    ]),
  ]);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    message.textContent = "";
    saveBtn.disabled = true;
    try {
      await api("/api/data-backup/settings", {
        method: "PUT",
        body: {
          enabled: enabled.checked,
          dest_dir: dest.value.trim(),
          times: times.value.split(/[,、\s]+/).filter((t) => t),
          weekdays: weekdays.filter((w) => w.box.checked).map((w) => w.index),
          keep: Number(keep.value),
          exclude_workdir: excludeWorkdir.checked,
          exclude_logs: excludeLogs.checked,
          exclude: lineList(exclude),
        },
      });
      renderDataBackup(_t("保存しました。"));
    } catch (e) {
      error.textContent = e.message;
      saveBtn.disabled = false;
    }
  });
  return form;
}
