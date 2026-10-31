"use strict";
/* 設定ダッシュボードの表示言語（日本語 / 英語）。ほかの JS より先に読み込む。
 *
 * 画面の文言は日本語の原文をそのままキーにする（_t("インスタンス")）。日本語なら原文を
 * そのまま、英語なら i18n_en.js の I18N_EN の訳を返す（訳が無ければ原文）。可変部分は
 * {0}・{1}… で書き、引数で埋める（_t("{0} を停止します", name)）。
 * index.html のテンプレートの文言は clone() が applyI18n() で訳す。
 *
 * 言語の決め方はチャット画面（frontend/src/i18n/）と同じ:
 * 画面で選んだ言語（localStorage）→ config.ini [ui].default_language（/ui-config.js）→ ブラウザの言語。
 * 切り替えたら再読み込みする（読み込み時に作る文言も多いため）。
 */

const I18N_STORAGE_KEY = "la-language";
const I18N_LANGUAGES = [
  { id: "ja", label: "日本語" },
  { id: "en", label: "English" },
];

function i18nNormalize(value) {
  if (typeof value !== "string") return null;
  const code = value.trim().toLowerCase().split(/[-_]/)[0];
  return code === "ja" || code === "en" ? code : null;
}

const I18N_LANG =
  i18nNormalize(localStorage.getItem(I18N_STORAGE_KEY)) ||
  i18nNormalize(window.LOCOHANE_DEFAULT_LANGUAGE) ||
  (i18nNormalize(navigator.language) === "en" ? "en" : "ja");

document.documentElement.lang = I18N_LANG;
// 日付・時刻の表示形式（toLocaleString 等）に使うロケール。
const I18N_LOCALE = I18N_LANG === "en" ? "en-US" : "ja-JP";

function _t(text, ...args) {
  const translated = I18N_LANG === "en" && typeof I18N_EN !== "undefined" ? I18N_EN[text] : undefined;
  const result = translated !== undefined ? translated : text;
  return args.length ? result.replace(/\{(\d+)\}/g, (m, i) => (i < args.length ? String(args[i]) : m)) : result;
}

const I18N_ATTRS = ["title", "placeholder", "aria-label"];

/** ファイル選択欄をボタンと選択中のファイル名の表示に置き換える。
 * ブラウザ標準の表示（「ファイルの選択」等）は OS・ブラウザの言語で出て、画面の言語に従わないため。 */
function localizeFileInput(input) {
  if (input.dataset.localized) return;
  input.dataset.localized = "1";
  input.hidden = true;
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = _t("ファイルを選択");
  const name = document.createElement("span");
  name.className = "file-pick-name";
  const update = () => {
    const files = Array.from(input.files || []);
    name.textContent = files.length ? files.map((f) => f.name).join(", ") : _t("選択されていません");
  };
  button.addEventListener("click", () => input.click());
  input.addEventListener("change", update);
  update();
  input.after(button, name);
}

/** テンプレート（index.html）由来の DOM の文言を表示言語へ置き換える。 */
function applyI18n(root) {
  if (root.querySelectorAll) root.querySelectorAll("input[type=file]").forEach(localizeFileInput);
  if (I18N_LANG === "ja") return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue.trim();
    if (!text) continue;
    const translated = _t(text);
    if (translated !== text) node.nodeValue = node.nodeValue.replace(text, translated);
  }
  const elements = root.querySelectorAll ? root.querySelectorAll("*") : [];
  for (const el of elements) {
    for (const attr of I18N_ATTRS) {
      const value = el.getAttribute(attr);
      if (value) el.setAttribute(attr, _t(value));
    }
  }
}

/** 言語の選択欄を作る（選ぶと保存して再読み込みする）。 */
function createLanguageSelect() {
  const select = document.createElement("select");
  select.className = "lang-select";
  select.title = "言語 / Language";
  for (const lang of I18N_LANGUAGES) {
    const option = document.createElement("option");
    option.value = lang.id;
    option.textContent = lang.label;
    option.selected = lang.id === I18N_LANG;
    select.appendChild(option);
  }
  select.addEventListener("change", () => {
    localStorage.setItem(I18N_STORAGE_KEY, select.value);
    location.reload();
  });
  return select;
}

// スクリプトは body の末尾で読み込むため、この時点で初期表示（「読み込み中...」）がある。
document.title = _t(document.title);
if (document.getElementById("app")) applyI18n(document.getElementById("app"));
