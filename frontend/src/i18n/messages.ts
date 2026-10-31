// 画面の表示言語（日本語 / 英語）。バックエンド側の文言は src/i18n.py、設定ダッシュボードは
// admin/static/i18n.js が持つ。言語の決め方は3者で揃える:
// 画面で選んだ言語（localStorage）→ config.ini [ui].default_language → ブラウザの言語。
export type Lang = 'ja' | 'en';

export const LANGUAGE_OPTIONS: { id: Lang; label: string }[] = [
  { id: 'ja', label: '日本語' },
  { id: 'en', label: 'English' }
];

export const STORAGE_KEY = 'la-language';

export const ja = {
  'common.cancel': 'キャンセル',
  'common.cancelWithIcon': '❌ キャンセル',
  'common.send': '送信',
  'common.stop': '停止',
  'common.newChat': '新規チャット',
  'common.untitledThread': '無題の会話',

  'header.openThreads': '会話一覧を開く',
  'header.openDetails': '実行の詳細を開く',

  'theme.title': '表示設定',
  'theme.scheme': '配色モード',
  'theme.accent': 'テーマカラー',
  'theme.language': '言語 / Language',
  'theme.light': 'ライト',
  'theme.dark': 'ダーク',
  'theme.system': '自動',
  'accent.purple': 'パープル',
  'accent.blue': 'ブルー',
  'accent.green': 'グリーン',
  'accent.orange': 'オレンジ',
  'accent.pink': 'ピンク',
  'accent.teal': 'ティール',

  'login.title': 'ログイン',
  'login.username': 'ユーザー名',
  'login.password': 'パスワード',
  'login.submit': 'ログイン',
  'login.submitting': 'ログイン中…',
  'login.invalid': 'ユーザー名またはパスワードが正しくありません',

  'sidebar.rename': '名前を変更',
  'sidebar.renamePrompt': '会話の名前を変更',
  'sidebar.delete': '削除',
  'sidebar.deleteConfirm': '「{name}」を削除しますか？',
  'sidebar.deleteBusy': 'この会話は生成中のため削除できません。停止してから削除してください。',
  'sidebar.cannotDeleteWhileGenerating': '生成中は削除できません',
  'sidebar.close': '会話一覧を閉じる',
  'sidebar.collapse': 'サイドバーを折りたたむ',
  'sidebar.waitingForUser': 'ユーザーの操作待ち',
  'sidebar.generating': '生成中',
  'sidebar.logout': 'ログアウト',

  'side.details': '実行の詳細',
  'side.detailsCollapsed': '◂ 詳細',
  'side.closeDetails': '実行の詳細を閉じる',
  'side.noSteps': 'ツール呼び出しはまだありません。',
  'side.scrollToLatestStep': '最新のStepへスクロール',
  'side.latestStep': '最新のStepへ',

  'workdir.title': '📁 作業ディレクトリ',
  'workdir.serverScope': '（サーバーPC上）',
  'workdir.serverScopeTitle': 'お使いのPCではなく、Locohane が動いているサーバーPCから見たパスです',
  'workdir.setFailed': 'フォルダを設定できませんでした。',
  'workdir.networkError': 'サーバーとの通信に失敗しました。',
  'workdir.lockedTitle': '作業フォルダは新規チャットの最初の送信前のみ設定できます',
  'workdir.setRemoteTitle': '作業フォルダ（サーバーPC上のフォルダ）を設定',
  'workdir.setTitle': '作業フォルダを設定',
  'workdir.popoverTitleBefore': '作業フォルダ（サーバーPC ',
  'workdir.popoverTitleAfter': ' 上のパス）',
  'workdir.popoverNote1': 'お使いのPCではなく、Locohane が動いているサーバーPCから見たフォルダを指定します。',
  'workdir.popoverNote2': 'お使いのPCのフォルダは、サーバーPCと共有されていない限り指定できません。',
  'workdir.placeholderRemote': 'サーバーPC上の絶対パス（例: C:\\Users\\you\\project）',
  'workdir.placeholder': '作業フォルダの絶対パス（例: C:\\Users\\you\\project）',
  'workdir.suggestions': 'サーバーPC上のフォルダ候補',
  'workdir.resetTitle': 'config.ini の既定フォルダに戻す',
  'workdir.reset': '既定値に戻す',
  'workdir.apply': '設定',

  'effort.title': '推論の深さ（reasoning_effort）',
  'effort.setTitle': '推論の深さ（reasoning_effort）を設定',
  'effort.lockedTitle': '生成中は推論の深さを変更できません',
  'effort.useConfig': 'config.ini の設定',
  'effort.useConfigWithValue': 'config.ini の設定（{value}）',
  'effort.unset': '未指定',
  'effort.noneLabel': 'none（思考しない）',
  'effort.note': '次の送信から反映されます。使える値はモデルのテンプレート次第です。',

  'usage.input': '入力',
  'usage.output': '出力',
  'usage.total': '合計',

  'plan.title': '実行計画',
  'plan.done': '✅ 計画完了',
  'plan.noPlan': '計画がまだありません',
  'plan.toPlanMode': 'クリックで Plan Mode に戻す',
  'plan.toEditAutomatically': 'クリックで Edit Automatically に切り替える',

  'step.tool': 'ツール',
  'step.llm': '思考中',
  'step.retrieval': '検索',
  'step.system': 'システム',
  'step.running': '実行中',
  'step.done': '完了',
  'step.error': 'エラー',
  'step.stopped': '停止',
  'step.background': 'バックグラウンド',
  'step.thinking': '思考',
  'step.input': '入力',
  'step.output': '出力',

  'messages.empty': 'まだメッセージはありません。下の入力欄から話しかけてください。',
  'messages.scrollToLatest': '最新のメッセージへスクロール',
  'messages.latest': '最新のメッセージへ',
  'messages.continue': '続ける',
  'messages.continueTitle': '中断したところから続きを実行します（「続けて」と送信します）',
  'messages.continuePrompt': '続けて',

  'composer.pasteTooLong':
    '貼り付けたテキスト（{length}文字）を追加すると上限（{limit}文字）を超えるため、追加しませんでした。',
  'composer.busyThisThread': '現在、この会話はタスク処理中です。',
  'composer.busyOtherThread': '他の会話が処理中です。完了するまで新しい送信はできません。',
  'composer.pastedText': '貼り付けテキスト',
  'composer.removePastedText': '貼り付けテキストを削除',
  'composer.pastedTextStats': '{chars}文字 · {lines}行',
  'composer.removeAttachment': '添付を削除',
  'composer.placeholderBusy': 'タスク処理中です...',
  'composer.placeholderOtherBusy': '他の会話が処理中です...',
  'composer.placeholderReply': '応答を入力...',
  'composer.placeholder': 'メッセージを入力...',
  'composer.attachFile': 'ファイルを添付',
  'composer.charCountTitle': '入力欄と貼り付けテキストの合計文字数 / 上限',
  'composer.overLimit': '文字数の上限を超えています',

  'ask.otherFreeText': 'その他（自由記述）',
  'ask.answerPlaceholder': '回答を入力...',
  'ask.selectFile': 'ファイルを選択してください',
  'ask.chooseFile': 'ファイルを選択',
  'ask.maxSize': '（最大 {size}MB）',
  'ask.uploading': 'アップロード中... {percent}%',

  'file.preparing': '{name}（準備中...）',
  'file.tapAgain': '{name}（もう一度タップで開く）',
  'file.failed': '{name}（開けませんでした。もう一度タップ）'
};

export type MessageKey = keyof typeof ja;

const en: Record<MessageKey, string> = {
  'common.cancel': 'Cancel',
  'common.cancelWithIcon': '❌ Cancel',
  'common.send': 'Send',
  'common.stop': 'Stop',
  'common.newChat': 'New chat',
  'common.untitledThread': 'Untitled conversation',

  'header.openThreads': 'Open conversations',
  'header.openDetails': 'Open run details',

  'theme.title': 'Display settings',
  'theme.scheme': 'Color mode',
  'theme.accent': 'Theme color',
  'theme.language': '言語 / Language',
  'theme.light': 'Light',
  'theme.dark': 'Dark',
  'theme.system': 'Auto',
  'accent.purple': 'Purple',
  'accent.blue': 'Blue',
  'accent.green': 'Green',
  'accent.orange': 'Orange',
  'accent.pink': 'Pink',
  'accent.teal': 'Teal',

  'login.title': 'Sign in',
  'login.username': 'Username',
  'login.password': 'Password',
  'login.submit': 'Sign in',
  'login.submitting': 'Signing in…',
  'login.invalid': 'Incorrect username or password',

  'sidebar.rename': 'Rename',
  'sidebar.renamePrompt': 'Rename conversation',
  'sidebar.delete': 'Delete',
  'sidebar.deleteConfirm': 'Delete "{name}"?',
  'sidebar.deleteBusy': 'This conversation is still generating and cannot be deleted. Stop it first, then delete.',
  'sidebar.cannotDeleteWhileGenerating': 'Cannot delete while generating',
  'sidebar.close': 'Close conversations',
  'sidebar.collapse': 'Collapse sidebar',
  'sidebar.waitingForUser': 'Waiting for your input',
  'sidebar.generating': 'Generating',
  'sidebar.logout': 'Sign out',

  'side.details': 'Run details',
  'side.detailsCollapsed': '◂ Details',
  'side.closeDetails': 'Close run details',
  'side.noSteps': 'No tool calls yet.',
  'side.scrollToLatestStep': 'Scroll to the latest step',
  'side.latestStep': 'Latest step',

  'workdir.title': '📁 Working directory',
  'workdir.serverScope': ' (on the server PC)',
  'workdir.serverScopeTitle': 'This path is as seen from the server PC running Locohane, not from your PC',
  'workdir.setFailed': 'Could not set the folder.',
  'workdir.networkError': 'Failed to communicate with the server.',
  'workdir.lockedTitle': 'The working folder can only be set before the first message of a new chat',
  'workdir.setRemoteTitle': 'Set the working folder (a folder on the server PC)',
  'workdir.setTitle': 'Set the working folder',
  'workdir.popoverTitleBefore': 'Working folder (a path on the server PC ',
  'workdir.popoverTitleAfter': ')',
  'workdir.popoverNote1': 'Specify a folder as seen from the server PC running Locohane, not from your PC.',
  'workdir.popoverNote2': 'Folders on your PC cannot be used unless they are shared with the server PC.',
  'workdir.placeholderRemote': 'Absolute path on the server PC (e.g. C:\\Users\\you\\project)',
  'workdir.placeholder': 'Absolute path of the working folder (e.g. C:\\Users\\you\\project)',
  'workdir.suggestions': 'Folder suggestions on the server PC',
  'workdir.resetTitle': 'Go back to the default folder in config.ini',
  'workdir.reset': 'Use default',
  'workdir.apply': 'Set',

  'effort.title': 'Reasoning depth (reasoning_effort)',
  'effort.setTitle': 'Set the reasoning depth (reasoning_effort)',
  'effort.lockedTitle': 'The reasoning depth cannot be changed while generating',
  'effort.useConfig': 'config.ini setting',
  'effort.useConfigWithValue': 'config.ini setting ({value})',
  'effort.unset': 'not set',
  'effort.noneLabel': 'none (no thinking)',
  'effort.note': 'Applies from your next message. Supported values depend on the model template.',

  'usage.input': 'Input',
  'usage.output': 'Output',
  'usage.total': 'Total',

  'plan.title': 'Plan',
  'plan.done': '✅ Plan completed',
  'plan.noPlan': 'No plan yet',
  'plan.toPlanMode': 'Click to return to Plan Mode',
  'plan.toEditAutomatically': 'Click to switch to Edit Automatically',

  'step.tool': 'Tool',
  'step.llm': 'Thinking',
  'step.retrieval': 'Search',
  'step.system': 'System',
  'step.running': 'Running',
  'step.done': 'Done',
  'step.error': 'Error',
  'step.stopped': 'Stopped',
  'step.background': 'Background',
  'step.thinking': 'Thinking',
  'step.input': 'Input',
  'step.output': 'Output',

  'messages.empty': 'No messages yet. Type in the box below to get started.',
  'messages.scrollToLatest': 'Scroll to the latest message',
  'messages.latest': 'Latest message',
  'messages.continue': 'Continue',
  'messages.continueTitle': 'Resume from where it was interrupted (sends "Continue")',
  'messages.continuePrompt': 'Continue',

  'composer.pasteTooLong':
    'The pasted text ({length} characters) was not added because it would exceed the limit ({limit} characters).',
  'composer.busyThisThread': 'This conversation is currently processing a task.',
  'composer.busyOtherThread': 'Another conversation is being processed. You cannot send until it finishes.',
  'composer.pastedText': 'Pasted text',
  'composer.removePastedText': 'Remove pasted text',
  'composer.pastedTextStats': '{chars} chars · {lines} lines',
  'composer.removeAttachment': 'Remove attachment',
  'composer.placeholderBusy': 'Processing a task...',
  'composer.placeholderOtherBusy': 'Another conversation is being processed...',
  'composer.placeholderReply': 'Type your reply...',
  'composer.placeholder': 'Type a message...',
  'composer.attachFile': 'Attach files',
  'composer.charCountTitle': 'Total characters in the input box and pasted text / limit',
  'composer.overLimit': 'Over the character limit',

  'ask.otherFreeText': 'Other (free text)',
  'ask.answerPlaceholder': 'Type your answer...',
  'ask.selectFile': 'Please choose a file',
  'ask.chooseFile': 'Choose file',
  'ask.maxSize': ' (max {size} MB)',
  'ask.uploading': 'Uploading... {percent}%',

  'file.preparing': '{name} (preparing...)',
  'file.tapAgain': '{name} (tap again to open)',
  'file.failed': '{name} (could not open; tap again)'
};

export const MESSAGES: Record<Lang, Record<MessageKey, string>> = { ja, en };

export type Translate = (key: MessageKey, vars?: Record<string, string | number>) => string;

export function normalizeLang(value: unknown): Lang | null {
  if (typeof value !== 'string') return null;
  const code = value.trim().toLowerCase().split(/[-_]/)[0];
  return code === 'ja' || code === 'en' ? code : null;
}

export function getStoredLang(): Lang | null {
  return normalizeLang(localStorage.getItem(STORAGE_KEY));
}

export function getBrowserLang(): Lang {
  return normalizeLang(navigator.language) === 'en' ? 'en' : 'ja';
}

export function format(text: string, vars?: Record<string, string | number>): string {
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (match, name: string) => (name in vars ? String(vars[name]) : match));
}
