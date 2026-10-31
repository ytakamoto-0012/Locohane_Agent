"use strict";
/* 管理アシスタント（右上の「AIに相談」）。admin.js の後に読み込む。
 * バックエンドは admin/assistant.py（POST /api/assistant/chat が NDJSON で少しずつ返す）。
 * AI は助言するだけで、設定の変更は管理者が画面で行う。回答中の設定キーには
 * config.ini 画面でそのキーを開くボタンを付ける。
 * 会話履歴はこのタブの sessionStorage にだけ置く（サーバーには保存しない）。
 */

const ASSISTANT_STORAGE_KEY = "locohane-admin-assistant";

let assistantState = { user: null, instance: null, messages: [] };
let assistantAbort = null;

function loadAssistantState(username) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(ASSISTANT_STORAGE_KEY) || "null");
    // 別の管理者でログインし直した場合は前の人の会話を引き継がない。
    if (saved && saved.user === username && Array.isArray(saved.messages)) return saved;
  } catch (e) {
    /* ignore */
  }
  return { user: username, instance: null, messages: [] };
}

function saveAssistantState() {
  try {
    sessionStorage.setItem(ASSISTANT_STORAGE_KEY, JSON.stringify(assistantState));
  } catch (e) {
    /* ignore */
  }
}

/* admin.js の showShell() から呼ばれる。 */
async function initAssistant(username) {
  const toggle = $("#assistant-toggle-btn");
  if (!toggle) return;
  let status;
  try {
    status = await api("/api/assistant/status");
  } catch (e) {
    return;
  }
  if (!status.enabled) return;
  assistantState = loadAssistantState(username);
  toggle.classList.remove("hidden");
  toggle.addEventListener("click", () => {
    if ($("#assistant-panel")) closeAssistant();
    else openAssistant(status);
  });
}

/* 管理者が今開いている画面（AI への手がかり。表示名で送る）。 */
function currentAssistantView() {
  const tab = $(".tab-btn.active");
  const inDetail = !!$(".view-instance-detail");
  const subtab = inDetail ? $(".subtab-btn.active") : null;
  return {
    tab: tab ? tab.textContent.trim() : null,
    instance: inDetail ? currentInstanceName : null,
    subview: subtab ? subtab.textContent.trim() : null,
  };
}

async function openAssistant(status) {
  const shell = $(".shell");
  if (!shell) return;
  shell.appendChild(clone("tpl-assistant"));
  shell.classList.add("assistant-open");
  // 「管理者設定」で回答に使う LLM を変えられるため、開くたびに読み直す。
  try {
    status = await api("/api/assistant/status");
  } catch (e) {
    // 読めなければ起動時の状態のまま表示する
  }
  $("#assistant-llm-note").textContent = status.llm_instance
    ? _t(`回答にはインスタンス {0} の LLM 接続先を使います（「管理者設定」で変えられます）。`, status.llm_instance)
    : _t("回答には相談対象のインスタンスの LLM 接続先を使います。つながらないときは「管理者設定」の「回答に使う LLM」で、動いている LLM を持つインスタンスを選んでください。");
  $("#assistant-close-btn").addEventListener("click", closeAssistant);
  $("#assistant-clear-btn").addEventListener("click", () => {
    if (assistantAbort) assistantAbort.abort();
    assistantState.messages = [];
    saveAssistantState();
    renderAssistantMessages();
  });
  $("#assistant-stop-btn").addEventListener("click", () => {
    if (assistantAbort) assistantAbort.abort();
  });
  $("#assistant-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    sendAssistantMessage();
  });
  $("#assistant-input").addEventListener("keydown", (ev) => {
    // Enter で送信、Shift+Enter で改行。日本語入力の変換確定の Enter（isComposing / keyCode 229）では送らない。
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && ev.keyCode !== 229) {
      ev.preventDefault();
      sendAssistantMessage();
    }
  });
  $("#assistant-instance").addEventListener("change", (ev) => {
    assistantState.instance = ev.target.value;
    saveAssistantState();
  });
  await fillAssistantInstances();
  renderAssistantMessages();
  $("#assistant-input").focus();
}

function closeAssistant() {
  if (assistantAbort) assistantAbort.abort();
  const panel = $("#assistant-panel");
  if (panel) panel.remove();
  const shell = $(".shell");
  if (shell) shell.classList.remove("assistant-open");
}

async function fillAssistantInstances() {
  const select = $("#assistant-instance");
  let list = instanceCache;
  try {
    list = (await api("/api/instances")).instances;
  } catch (e) {
    /* 取得できなければ一覧画面のキャッシュを使う */
  }
  select.replaceChildren(...list.map((i) => el("option", { value: i.name, text: i.display_name || i.name })));
  // 開いた時点でインスタンス詳細を見ていればそのインスタンス、なければ前回の選択か既定のインスタンス。
  const names = list.map((i) => i.name);
  const preferred = [
    $(".view-instance-detail") ? currentInstanceName : null,
    assistantState.instance,
    "default",
    names[0],
  ].find((n) => n && names.includes(n));
  if (preferred) select.value = preferred;
  assistantState.instance = select.value || null;
}

function renderAssistantMessages() {
  const box = $("#assistant-messages");
  if (!box) return;
  box.replaceChildren();
  if (!assistantState.messages.length) {
    box.append(
      el("p", {
        class: "hint",
        text: _t("例:「応答が遅い」「チャット画面にログインできない」「社内文書を検索させたい」「長い会話の途中でエラーになる」"),
      })
    );
  }
  for (const m of assistantState.messages) box.append(buildAssistantMessage(m));
  box.scrollTop = box.scrollHeight;
}

function buildAssistantMessage(m) {
  const node = el("div", { class: `assistant-msg ${m.role}${m.error ? " failed" : ""}` });
  if (m.role === "assistant" && m.html) {
    // HTML はサーバー側（markdown-it-py、生HTML無効）で生成済み。
    node.innerHTML = m.html;
  } else {
    node.textContent = m.content;
  }
  if (m.keys && m.keys.length) node.append(buildAssistantKeyButtons(m.keys, m.instance));
  if (m.sources && m.sources.length) {
    // AI に渡したドキュメントの段落（回答の根拠を確かめられるように）。
    node.append(
      el("details", { class: "assistant-sources" }, [
        el("summary", { text: _t(`参考にした説明（{0}件）`, m.sources.length) }),
        el("ul", {}, m.sources.map((s) => el("li", { text: `${s.source} > ${s.title}` }))),
      ])
    );
  }
  return node;
}

function buildAssistantKeyButtons(keys, instance) {
  return el(
    "div",
    { class: "assistant-keys" },
    keys.map((k) =>
      el("button", {
        type: "button",
        text: _t(`[{0}] {1} を開く`, k.section, k.key),
        title: _t(`インスタンス {0} の config.ini 画面でこのキーを表示します（変更・保存はご自身で行ってください）`, instance),
        onclick: () => openConfigKeyFromAssistant(instance, k.section, k.key),
      })
    )
  );
}

/* インスタンス詳細の config.ini 画面を開き、検索欄にキーを入れて絞り込む。 */
async function openConfigKeyFromAssistant(instance, section, key) {
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === "instances"));
  await renderInstanceDetail(instance, "config");
  const search = $("#config-search");
  if (!search) return;
  search.value = `${section}.${key}`;
  search.dispatchEvent(new Event("input"));
}

/* POST して NDJSON を1行ずつ onEvent に渡す（401 なら再ログインして1回だけ送り直す）。 */
async function streamAssistant(body, signal, onEvent) {
  const doFetch = () =>
    fetch("/api/assistant/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Locohane-Admin": "1", "X-Locohane-Lang": I18N_LANG },
      credentials: "same-origin",
      body: JSON.stringify(body),
      signal,
    });
  let res = await doFetch();
  if (res.status === 401) {
    await promptRelogin();
    res = await doFetch();
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch (e) {
      /* ignore */
    }
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
}

async function sendAssistantMessage() {
  const input = $("#assistant-input");
  const text = input.value.trim();
  if (!text || assistantAbort) return;
  const instance = $("#assistant-instance").value;
  if (!instance) {
    $("#assistant-error").textContent = _t("相談対象のインスタンスを選んでください。");
    return;
  }
  $("#assistant-error").textContent = "";
  input.value = "";
  assistantState.messages.push({ role: "user", content: text });
  saveAssistantState();
  renderAssistantMessages();

  const box = $("#assistant-messages");
  let statusText = _t("考えています…");
  const live = el("div", { class: "assistant-msg assistant streaming", text: statusText });
  box.append(live);
  box.scrollTop = box.scrollHeight;

  let partial = "";
  let finished = null;
  let failure = null;
  // 思考するモデルでは最初の文字が出るまで数分かかることがあるため、経過時間を見せる。
  const startedAt = Date.now();
  const waitTimer = setInterval(() => {
    if (!partial) live.textContent = _t(`{0}（{1}秒）`, statusText, Math.round((Date.now() - startedAt) / 1000));
  }, 1000);
  assistantAbort = new AbortController();
  $("#assistant-stop-btn").classList.remove("hidden");
  $("#assistant-send-btn").disabled = true;
  try {
    await streamAssistant(
      {
        instance,
        // 失敗した回答（error）は AI に渡さない。
        messages: assistantState.messages.filter((m) => !m.error).map((m) => ({ role: m.role, content: m.content })),
        view: currentAssistantView(),
      },
      assistantAbort.signal,
      (ev) => {
        if (ev.type === "status") {
          statusText = ev.text;
          if (!partial) live.textContent = statusText;
        } else if (ev.type === "delta") {
          partial += ev.text;
          live.textContent = partial;
          box.scrollTop = box.scrollHeight;
        } else if (ev.type === "done") {
          finished = ev;
        } else if (ev.type === "error") {
          failure = ev.message;
        }
      }
    );
  } catch (e) {
    failure = e.name === "AbortError" ? null : e.message || _t("相談に失敗しました。");
  } finally {
    clearInterval(waitTimer);
    assistantAbort = null;
    if ($("#assistant-panel")) {
      $("#assistant-stop-btn").classList.add("hidden");
      $("#assistant-send-btn").disabled = false;
    }
  }
  if (finished) {
    assistantState.messages.push({ role: "assistant", content: finished.text, html: finished.html, keys: finished.keys, sources: finished.sources, instance });
  } else if (partial && !failure) {
    // 停止ボタンで止めた場合は、そこまでの回答を残す。
    assistantState.messages.push({ role: "assistant", content: partial + _t("\n（停止しました）"), instance });
  } else if (failure) {
    assistantState.messages.push({ role: "assistant", content: failure, error: true });
  }
  saveAssistantState();
  renderAssistantMessages();
  if ($("#assistant-input")) $("#assistant-input").focus();
}
