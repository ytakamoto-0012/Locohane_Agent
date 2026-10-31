import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

// Chainlitの custom_css は、ビルド後 index.html 内の
// `<!-- CSS INJECTION PLACEHOLDER -->` を <link> タグへ置換する仕組み。
// Vite はビルド後 CSS の <link> を transformIndexHtml で head 末尾に追加するため、
// プレースホルダーを head 内のどこに置いても Vite 生成 CSS より先に読み込まれてしまい、
// public/custom.css での上書きが効かない（後に読み込まれた方が優先されるため）。
// order: 'post' で Vite 自身の変換が終わった後にプレースホルダーを再度末尾へ
// 移動し、custom.css が確実に最後に読み込まれるようにする。
function moveCssPlaceholderToEnd(): Plugin {
  const placeholder = '<!-- CSS INJECTION PLACEHOLDER -->'
  return {
    name: 'move-css-placeholder-to-end',
    transformIndexHtml: {
      order: 'post',
      handler(html) {
        if (!html.includes(placeholder)) return html
        return html.replace(placeholder, '').replace('</head>', `    ${placeholder}\n  </head>`)
      },
    },
  }
}

// https://vite.dev/config/
// ビルド成果物は .chainlit/config.toml の custom_build = "./public/build" に
// そのまま乗せるため、public/build を直接出力先にする。
export default defineConfig({
  plugins: [react(), moveCssPlaceholderToEnd()],
  build: {
    outDir: resolve(import.meta.dirname, '../public/build'),
    emptyOutDir: true,
  },
})
