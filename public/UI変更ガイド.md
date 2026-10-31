# UIの変更方法まとめ

このプロジェクトのUIは2種類に分かれる。

- **再ビルド不要**: `public/settings/` 配下のテキスト/画像ファイルを編集するだけで反映される
- **再ビルド必要**: `frontend/src/` のソースを編集し、`npm run build` で `public/build/` に出力し直す必要がある

---

## 1. 再ビルド不要（`public/settings/` を編集するだけ）

Chainlitの `/public/{filename:path}` ルートで静的配信される（[app.py:101-104](../app.py#L101-L104)）。
SPAの `public/build/` とは別経路なので、フロントエンドの再ビルドは不要。

| 変更したいもの | 編集するファイル | 備考 |
|---|---|---|
| ヘッダーの左アイコン | `public/settings/icon.{png,svg,jpg,jpeg}` | png→svg→jpg→jpegの順で試し、最初に見つかったものを表示。全て無い/失敗時は非表示になるだけ（エラーにはならない）。[Header.tsx](../frontend/src/components/Header.tsx) |
| ヘッダーのタイトル文言 | `public/settings/header.md` | 1行目（`#`等の見出し記号は除去される）がタイトルとして使われる。[Header.tsx:12-23](../frontend/src/components/Header.tsx#L12-L23) |
| 起動時のWelcomeメッセージ | `public/settings/welcome.md` | `{skills}` はスキル一覧に置換される。存在しない場合は [app.py:164-167](../app.py#L164-L167) のデフォルト文言にフォールバック |
| チャット画像のプレビューサイズ等、簡単な見た目調整 | `public/custom.css` | CSS変数を上書きするだけで反映。`.chainlit/config.toml` の `custom_css = "/public/custom.css"` で読み込まれる（Chainlit標準機能） |

アイコンを表示したい場合は `public/settings/` に画像ファイルを追加する。

### `public/custom.css` で上書きできるCSS変数

`frontend/src/styles.css` の `:root` で定義されているCSS変数を `public/custom.css` 側で再定義すると、ビルド無しで上書きできる（`custom_css` は `public/build/` のCSSより後に読み込まれるため優先される）。

| 変数名 | デフォルト値 | 効果 |
|---|---|---|
| `--message-image-width` | `320px` | 生成ファイルの自動プレビュー添付（`cl.Image`。`analyze_image` の `show_in_chat=True` 引数を含む）で表示するインライン画像の幅（`height` は `auto` でアスペクト比維持。指定サイズより小さい画像は拡大され、大きい画像は縮小される） |
| `--message-bubble-max-width` | `560px` | メッセージ吹き出し（`.message-bubble`）の最大幅 |
| `--header-icon-size` | `22px` | ヘッダー左アイコン（`.app-header-icon`）の幅・高さ |
| `--header-title-font-size` | `14px` | ヘッダータイトル文字列（`.app-header-title`）のフォントサイズ |
| `--plan-card-max-height` | `260px` | サイドパネルの実行計画カード（`.plan-card-list`）の最大高さ。項目数がこれを超えるとパネル内スクロールになる |
| `--ask-form-max-height` | `60vh` | `AskUserQuestion`/`ask_user_choice` の質問フォーム（`.ask-form-bar`）全体の最大高さ。質問・選択肢が多い場合にチャット画面を占有しないよう、入力欄部分（`.ask-form-fields`）がこの高さでスクロールする（質問文・送信ボタンは常に表示） |

編集後はブラウザをリロードするだけで反映される（Chainlitサーバー再起動・`npm run build` は不要）。

---

## 2. 再ビルド必要（`frontend/src/` を編集して `npm run build`）

### 主なコンポーネント（`frontend/src/components/`）

| ファイル | 役割 |
|---|---|
| `Header.tsx` | ヘッダー全体（アイコン・タイトル・新規チャットボタン・テーマピッカー） |
| `Icon.tsx` | SVGアイコン定義。新しいアイコン種別を追加する場合はここに `IconName` と `IconPath` を追加 |
| `ThemePicker.tsx` / `theme.ts` | アクセントカラー・ライト/ダーク切り替え。プリセット色は `theme.ts` の `ACCENT_PRESETS` |
| `Composer.tsx` | メッセージ入力欄 |
| `MessageThread.tsx` | チャット履歴表示 |
| `SidePanel.tsx` | サイドパネル |
| `WorkDirButton.tsx` / `WorkDirCard.tsx` | 作業ディレクトリ選択UI |
| `TokenUsageCard.tsx` | トークン使用量表示 |
| `PlanCard.tsx` | 実行計画（チェックリスト）表示 |
| `StepList.tsx` / `StepItem.tsx` | エージェントの実行ステップ表示 |
| `index.css` | 全体スタイル（CSS変数 `--accent` 等はここで定義） |

### ビルド手順

```bash
cd frontend
npm run build
```

- `frontend/vite.config.ts` の設定により、ビルド成果物は直接 `public/build/` に出力される（`.chainlit/config.toml` の `custom_build = "./public/build"` と対応）。
- 開発中にブラウザで即座に確認したい場合は `npm run dev`（Vite開発サーバー）を使う。

---

## 3. 迷ったときの判断基準

- **文言・画像だけ変えたい** → `public/settings/` を編集するだけで十分（再ビルド不要）
- **レイアウト・色・アイコン種類・挙動を変えたい** → `frontend/src/` を編集して `npm run build`
