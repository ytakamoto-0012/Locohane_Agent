"use strict";
/* 設定ダッシュボードの「管理者設定」画面（上部のタブ）。全インスタンス共通の設定を変える。
 * API は admin/admin_settings.py。値は <instances_dir>/admin_settings.json に保存し、config.ini より優先する
 * （環境変数で上書きされている項目は編集できない）。保存した値は再起動なしで、次の取り込み・次の相談から効く。
 */

const ADMIN_SETTING_SOURCE_LABELS = {
  dashboard: _t("この画面で設定"),
  "config.ini": _t("config.ini の値"),
  env: _t("環境変数で上書き中"),
};

function adminSettingInstanceLabel(i) {
  return i.display_name && i.display_name !== i.name ? `${i.display_name}（${i.name}）` : i.name;
}

// notice: 描き直したあと保存ボタンの上に出す完了メッセージ（保存・config.ini に戻した直後に渡す）。
async function renderAdminSettings(notice = "") {
  mainEl.innerHTML = "";
  const container = el("section", { class: "admin-settings" });
  mainEl.append(container);
  const data = await api("/api/admin-settings");
  const byKey = Object.fromEntries(data.settings.map((s) => [`${s.section}.${s.key}`, s]));
  const inputs = {};

  container.append(el("h2", { text: _t("管理者設定") }));
  container.append(
    el("p", {
      class: "hint",
      text: _t("全インスタンス共通の設定です（インスタンスごとの config.ini タブでは変えられません）。保存すると、管理ツールを再起動しなくても次の取り込み・次の相談から効きます。"),
    })
  );

  const sourceBadge = (s) =>
    el("span", { class: `admin-setting-source source-${s.source.replace(".", "-")}`, text: ADMIN_SETTING_SOURCE_LABELS[s.source] || s.source });

  const field = (name, label, control, hints) => {
    const s = byKey[name];
    inputs[name] = control;
    if (s.source === "env") control.disabled = true;
    return el("div", { class: "admin-setting" }, [
      el("label", {}, [el("span", { class: "admin-setting-label", text: label }), sourceBadge(s), control]),
      el("div", { class: "hint", text: _t(`[{0}] {1}（config.ini の値: {2}）`, s.section, s.key, s.default === "" ? _t("（空）") : s.default) }),
      s.source === "env" ? el("div", { class: "warn-box", text: _t(`環境変数 {0} で上書きされているため、ここでは変えられません。`, s.env) }) : null,
      ...hints.map((h) => el("p", { class: "hint", text: h })),
    ]);
  };

  // --- ナレッジの取り込み ---
  const number = (name, min) => el("input", { type: "number", min: String(min), value: byKey[name].value });
  // 0 より大きい小数（秒・MB）。下限の確認はサーバー側（admin/admin_settings.py の positive_float）。
  const decimal = (name) => el("input", { type: "number", min: "0", step: "any", value: byKey[name].value });
  container.append(el("h3", { text: _t("ナレッジの取り込み") }));
  container.append(
    field("vector_db.ingest_workers", _t("取り込みの並列数"), number("vector_db.ingest_workers", 1), [
      _t("全インスタンス・全コレクションの取り込みの合計で、同時に準備（読み取り・本文の抽出・画像化）するファイル数です。超える分は空きを待ちます。"),
    ])
  );
  container.append(
    field("vector_db.ingest_processes", _t("本文の抽出・画像化の子プロセス数"), number("vector_db.ingest_processes", 0), [
      _t("0 なら子プロセスを使わず、上の並列数のスレッドで行います。PDF・Excel 等が多く CPU に余裕があるときは、コア数を目安に増やすと速くなります（この PC のコア数: {0}）。", data.cpu_count || "?"),
      _t("1 以上のときは、同時に準備するファイル数もこの値になります。"),
    ])
  );
  container.append(
    field("vector_db.ingest_embed_requests", _t("埋め込みのリクエストの並列数"), number("vector_db.ingest_embed_requests", 0), [
      _t("全インスタンス・全コレクションの取り込みの合計で、同時に送る埋め込みのリクエスト数です。1ファイルは複数のリクエスト（本文のチャンクのバッチ・ページ画像1枚ずつ）に分けて送るため、ファイル数より多くできます。0 なら取り込みの並列数と同じです。"),
      _t("埋め込みサーバー（llama-server なら --parallel）が同時に処理できる数を目安にしてください。"),
    ])
  );
  container.append(
    field("vector_db.scan_workers", _t("取り込み元フォルダの走査の並列数"), number("vector_db.scan_workers", 1), [
      _t("取り込み元フォルダを走査するときに、同時に読むフォルダの数です。ファイルサーバー（ネットワーク共有）上のフォルダは並列に読むと速くなります（ローカルのディスクでは効果は小さい）。"),
    ])
  );
  container.append(
    field("vector_db.max_files_per_collection", _t("1コレクションの最大ファイル数"), number("vector_db.max_files_per_collection", 1), [
      _t("取り込み元にこれより多くのファイルがあると、上限までのファイルだけを取り込み、ナレッジ画面に警告を出します。取り込みでは対象のファイルをメモリーに持つため（100万件で数 GB 程度）、管理ツールのメモリーを使いすぎないための上限です。"),
    ])
  );
  container.append(
    field("vector_db.max_file_mb", _t("1ファイルの上限サイズ（MB）"), decimal("vector_db.max_file_mb"), [
      _t("これより大きいファイルは取り込まず、失敗一覧に理由を載せます。"),
    ])
  );
  container.append(
    field("vector_db.office_timeout_seconds", _t("Office の PDF 書き出しの上限秒数"), decimal("vector_db.office_timeout_seconds"), [
      _t("[vector_db] image_input = true で Office ファイルを PDF に書き出す1回の上限秒数です。超えたら見えない確認画面で止まったものとして Office のプロセスを終了し、そのファイルを失敗一覧に載せます。数百ページの文書で足りなければ大きくしてください。"),
    ])
  );
  container.append(
    field(
      "vector_db.ingest_temp_dir",
      _t("取り込みの一時ファイルの置き場所"),
      el("input", { type: "text", value: byKey["vector_db.ingest_temp_dir"].value, placeholder: _t("（空ならシステムの一時フォルダ %TEMP%）") }),
      [
        _t("取り込みの間だけ使う一時ファイル（ページ画像の PNG・ファイルの中から取り出した画像・Office から書き出した PDF）を置くフォルダです。空ならシステムの一時フォルダ（%TEMP%）を使います。相対パスはプロジェクトルート基準です。"),
        _t("無ければ作り、その下に取り込み1回ごとのフォルダ（locohane_vdb_pages_*）を作って、取り込みが終わったら消します。数百ページの PDF ではページ画像が数百 MB になることがあるため、システムドライブの空きが少ないときは別のドライブを指定してください（ネットワークドライブは遅くなるため避けてください）。"),
      ]
    )
  );
  container.append(
    field("vector_db.qdrant_max_retries", _t("Qdrant への送り直しの回数"), number("vector_db.qdrant_max_retries", -1), [
      _t("[vector_db] backend = qdrant のとき、Qdrant へ接続できない・接続を切られた・タイムアウトした・一時的なエラー（5xx・408・429）が返ったときに、[vector_db] qdrant_retry_interval_seconds 秒待って同じリクエストを送り直す回数です。-1 なら成功するまで無制限に送り直し（取り込み・コピーはナレッジ画面の「停止」で止められます）、0 なら送り直しません。"),
      _t("検索（AI が文書を探すとき）は止める手段が無いため、この値によらず最大1回だけ送り直します。"),
    ])
  );
  container.append(
    field("vector_db.qdrant_retry_interval_seconds", _t("Qdrant への送り直しの間隔（秒）"), decimal("vector_db.qdrant_retry_interval_seconds"), [
      _t("上の送り直しで、次に送るまで待つ秒数です（0 より大きい数）。検索の送り直しは、この値によらず最大1秒です。"),
    ])
  );

  // --- AIに相談 ---
  container.append(el("h3", { text: _t("AIに相談（管理アシスタント）") }));
  const instanceSelect = (name, emptyLabel) => {
    const select = el("select", {}, [el("option", { value: "", text: emptyLabel })]);
    for (const i of data.instances) {
      const llm = i.llm && i.llm.length ? ` — ${i.llm[0].model} @ ${i.llm[0].base_url}` : "";
      select.append(el("option", { value: i.name, text: `${adminSettingInstanceLabel(i)}${llm}` }));
    }
    select.value = byKey[name].value;
    return select;
  };
  const llmSelect = instanceSelect("admin.assistant_llm_instance", _t("（相談対象のインスタンス自身の LLM を使う）"));
  const checkResult = el("div", { class: "hint" });
  const checkBtn = el("button", {
    type: "button",
    text: _t("接続を確認"),
    onclick: async () => {
      const target = llmSelect.value;
      if (!target) {
        checkResult.className = "hint";
        checkResult.textContent = _t("インスタンスを選ぶと、その LLM に接続できるかを確認できます。");
        return;
      }
      checkBtn.disabled = true;
      checkResult.className = "hint";
      checkResult.textContent = _t("確認しています…");
      try {
        const r = await api("/api/admin-settings/check-llm", { method: "POST", body: { instance: target } });
        checkResult.className = r.ok ? "hint ok-text" : "error";
        const lines = r.endpoints.map((e) => `${e.ok ? "✓" : "✗"} ${e.model} @ ${e.base_url}${e.detail ? ` (${e.detail})` : ""}`);
        checkResult.textContent = (r.ok ? _t("接続できました。") : _t("接続できませんでした。")) + " " + (lines.join(" / ") || _t("[llm] main_url が設定されていません。"));
      } catch (e) {
        checkResult.className = "error";
        checkResult.textContent = e.message;
      } finally {
        checkBtn.disabled = false;
      }
    },
  });
  const llmField = field("admin.assistant_llm_instance", _t("回答に使う LLM"), llmSelect, [
    _t("「AIに相談」の回答を作る LLM を、どのインスタンスの [llm] main_url から取るかです。空なら、相談対象として選んだインスタンス自身の LLM を使います。"),
    _t("相談対象のインスタンスの LLM が止まっている・未設定だと回答できないため、確実に動いている LLM を持つインスタンス（スキル調整ワーカー等）を選んでおくと、最初のセットアップで詰まりません。"),
  ]);
  llmField.append(el("div", { class: "inline-actions" }, [checkBtn]), checkResult);
  container.append(llmField);
  container.append(
    field("admin.assistant_embedding_instance", _t("関係する設定を探す埋め込み"), instanceSelect("admin.assistant_embedding_instance", _t("（自動: 回答に使う LLM → 相談対象 → ほかのインスタンスの順）")), [
      _t("相談内容に近い設定・説明を選ぶのに使う埋め込みサーバーを、どのインスタンスの [vector_db] embedding_url から取るかです。見つからない・接続できないときは文字の重なりだけで選びます。"),
    ])
  );

  const error = el("div", { class: "error" });
  const message = el("div", { class: "hint ok-text", text: notice });
  const saveBtn = el("button", {
    class: "primary",
    text: _t("保存"),
    onclick: async () => {
      error.textContent = "";
      message.textContent = "";
      const values = {};
      for (const [name, control] of Object.entries(inputs)) if (!control.disabled) values[name] = control.value;
      saveBtn.disabled = true;
      try {
        await api("/api/admin-settings", { method: "PUT", body: { values } });
        await renderAdminSettings(_t("保存しました。"));
      } catch (e) {
        error.textContent = e.message;
        saveBtn.disabled = false;
      }
    },
  });
  const resetBtn = el("button", {
    text: _t("config.ini の値に戻す"),
    onclick: async () => {
      if (!confirm(_t("この画面で保存した設定を消して、config.ini の値に戻します。よろしいですか？"))) return;
      try {
        await api("/api/admin-settings", { method: "DELETE" });
        await renderAdminSettings(_t("config.ini の値に戻しました。"));
      } catch (e) {
        error.textContent = e.message;
      }
    },
  });
  container.append(error, message, el("div", { class: "inline-actions admin-settings-actions" }, [saveBtn, resetBtn]));
  container.append(el("p", { class: "hint", text: _t(`保存先: {0}`, data.path) }));
}
