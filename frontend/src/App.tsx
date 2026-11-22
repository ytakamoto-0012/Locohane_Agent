import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useRecoilValue, useSetRecoilState } from 'recoil';
import {
  useAuth,
  useChatSession,
  useChatInteract,
  useChatMessages,
  useChatData,
  useConfig,
  ChainlitContext,
  currentThreadIdState,
  sessionIdState,
  messagesState,
  elementState,
  tasklistState
} from '@chainlit/react-client';
import type { IMessageElement } from '@chainlit/react-client';
import { BACKEND_URL } from './chainlitClient';
import { useI18n } from './i18n';
import { useBrowserNotify } from './notify';
import { fetchOlderMessages, fetchThreadSnapshot, threadStepsToMessages, splitThreadElements } from './threadSnapshot';
import type { OlderStatus } from './components/MessageThread';
import { Header } from './components/Header';
import { LoginForm } from './components/LoginForm';
import { MessagePane } from './components/MessagePane';
import { Sidebar } from './components/Sidebar';
import { SidePanel } from './components/SidePanel';
import { Composer, type PendingAttachment } from './components/Composer';
import { AskActionBar } from './components/AskActionBar';
import { AskFormBar } from './components/AskFormBar';
import { AskChoiceFormBar } from './components/AskChoiceFormBar';
import { CadViewerBar } from './components/CadViewerBar';
import { AskTextBar } from './components/AskTextBar';
import { AskFileDropzone } from './components/AskFileDropzone';
import { StarterPrompts } from './components/StarterPrompts';
import { ChatTutorial } from './components/ChatTutorial';
import { countSteps, deriveThreadView, pruneMessages, selectRunningStepChain } from './utils/messageTree';
import type { IStep } from '@chainlit/react-client';
import { useIsMobile } from './utils/useIsMobile';
import './styles.css';

// 他セッション（別スレッドを開いた同じタブ・別タブ）が裏で処理中かどうかを
// 知るライブpushの仕組みが無いため、ポーリングで代替する（app.pyの
// _generating_thread_ids/on_message、/locohane/threads/{id}/status参照）。
const REMOTE_GENERATING_POLL_INTERVAL_MS = 3000;

// index.html の <title> にある既定値。public/settings/tab_title.md が
// 存在しない・読み込めない場合のフォールバックとして使う。
const DEFAULT_TAB_TITLE = 'Locohane';

// 生成中の間引き（pruneMessages）を調べ始めるStepの件数。これ以下なら間引かない。
const PRUNE_MIN_STEPS = 1000;
const NO_STEPS: IStep[] = [];

function App() {
  const { data: authData, isReady: authReady, isAuthenticated } = useAuth();
  const { connect, session } = useChatSession();
  const { setIdToResume, uploadFile } = useChatInteract();
  const { messages } = useChatMessages();
  const { loading } = useChatData();
  const { config } = useConfig();
  const currentThreadId = useRecoilValue(currentThreadIdState);
  const sessionId = useRecoilValue(sessionIdState);
  const client = useContext(ChainlitContext);
  const { lang, ready: langReady } = useI18n();
  // 完了・質問待ち・Plan 承認待ちのブラウザ通知（表示設定で ON のときだけ。notify.ts 参照）
  const { notifyRemoteComplete } = useBrowserNotify();
  const notifyRemoteCompleteRef = useRef(notifyRemoteComplete);
  notifyRemoteCompleteRef.current = notifyRemoteComplete;

  // スマホ幅では左右のパネルを引き出し式にし、同時に開くのは片方だけにする
  // （PC 幅では各パネルが自分の折りたたみ状態を持つため使わない）。
  const isMobile = useIsMobile();
  const [mobileDrawer, setMobileDrawer] = useState<'threads' | 'details' | null>(null);
  const closeMobileDrawer = useCallback(() => setMobileDrawer(null), []);
  useEffect(() => {
    if (!isMobile) setMobileDrawer(null);
  }, [isMobile]);

  const requireLogin = authData?.requireLogin ?? false;
  const canConnect = authReady && (!requireLogin || isAuthenticated);

  // ?thread=<id> があれば再開対象としてrecoil状態へ書き込んでからconnectする。
  // useChatSession().connect は threadIdToResumeState を読むdebounce関数のため、
  // setIdToResume 直後に同期的にconnectするとReactのstate反映タイミングと
  // 競合し古い値（未指定）が送られるリスクがある。seeded で「recoilへの
  // 書き込み確定後にのみconnectする」ことを保証する（Sidebar.tsx参照）。
  const [seeded, setSeeded] = useState(false);

  // 添付ファイルのstate。Composer（送信欄）とStarterPrompts（定型文ボタン）の
  // 両方から参照・送信できるよう、この階層で保持する（どちらから送信しても
  // 同じ添付が使われるようにするため）。
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);

  const uploadAttachment = useCallback(
    (file: File, pastedText?: string) => {
      const entry: PendingAttachment = { name: file.name, uploading: true, pastedText };
      setAttachments((prev) => [...prev, entry]);
      const { promise } = uploadFile(file, () => {});
      promise
        .then((fileRef) => {
          setAttachments((prev) => prev.map((a) => (a === entry ? { ...a, fileRef, uploading: false } : a)));
        })
        .catch(() => {
          setAttachments((prev) => prev.filter((a) => a !== entry));
        });
    },
    [uploadFile]
  );

  const handleAttach = useCallback(
    (files: FileList | File[] | null) => {
      if (!files) return;
      Array.from(files).forEach((file) => uploadAttachment(file));
    },
    [uploadAttachment]
  );

  // 入力欄へ長文が貼り付けられた場合、textareaへ展開せず .txt 添付として
  // アップロードする（Composer.tsx の onPaste 参照）。ファイル名は app.py の
  // PASTED_TEXT_FILENAME_RE と一致させること（一致した添付は保存先パスではなく
  // 本文としてLLMへ渡される）。同一ミリ秒の連続貼り付けに備えて連番を付ける。
  const pastedSeqRef = useRef(0);
  const handleAttachPastedText = useCallback(
    (text: string) => {
      pastedSeqRef.current += 1;
      const name = `pasted-text-${Date.now()}-${pastedSeqRef.current}.txt`;
      uploadAttachment(new File([text], name, { type: 'text/plain' }), text);
    },
    [uploadAttachment]
  );

  const handleRemoveAttachment = useCallback((index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleAttachmentsSent = useCallback(() => {
    setAttachments([]);
  }, []);

  // 生成中の追加指示に付けた添付が伝わらなかったとき、入力欄へ戻す（Composer.tsx の restoreToInput）。
  const handleRestoreAttachments = useCallback((restored: PendingAttachment[]) => {
    setAttachments((prev) => [...restored, ...prev]);
  }, []);

  useEffect(() => {
    const threadId = new URLSearchParams(window.location.search).get('thread');
    if (threadId) setIdToResume(threadId);
    setSeeded(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ブラウザのタブタイトルを public/settings/tab_title.md から読み込む
  // （Header.tsx の header.md 読み込みと同じ仕組み。再ビルド無しで変更可能にする）。
  useEffect(() => {
    fetch(`${BACKEND_URL}/public/settings/tab_title.md`)
      .then((res) => (res.ok ? res.text() : Promise.reject()))
      .then((text) => {
        const firstLine = text
          .split('\n')
          .map((line) => line.replace(/^#+\s*/, '').trim())
          .find((line) => line.length > 0);
        document.title = firstLine || DEFAULT_TAB_TITLE;
      })
      .catch(() => {});
  }, []);

  // 表示言語は userEnv でバックエンドへ渡す（app.py の _init_session_lang。システム通知・
  // ボタン・AI の応答言語に使う）。言語が確定する（i18n.tsx の ready）まで接続を待つ。
  const connectedLangRef = useRef(lang);
  useEffect(() => {
    if (!canConnect || !seeded || !langReady) return;
    connectedLangRef.current = lang;
    connect({ userEnv: { LOCOHANE_LANG: lang } });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canConnect, seeded, langReady]);

  // 接続中に表示設定で言語を切り替えたら、バックエンドのセッションにも伝える
  // （app.py の set_language。AI の応答言語は次の送信から切り替わる）。
  useEffect(() => {
    if (!sessionId || !langReady || connectedLangRef.current === lang) return;
    connectedLangRef.current = lang;
    client
      .callAction(
        { id: '', name: 'set_language', payload: { lang }, label: '', tooltip: '', forId: '', onClick: () => {} },
        sessionId
      )
      .catch(() => {});
  }, [lang, langReady, sessionId, client]);

  // ページを離れる（スレッド切り替え・新規チャット・再読み込み・タブを閉じる）時点で
  // Socket.IO 接続を明示的に閉じる。socket.io-client は既定ではページ離脱時に接続を
  // 閉じない（closeOnBeforeunload=false）ため、サーバーは ping 応答が途絶えるまで
  // 切断に気付けず、[websocket] ping_timeout（既定120秒）＋ping_interval の間、
  // 古いセッションが「接続中」のまま残っていた（2026-10-08、設定ダッシュボードの
  // 「接続中のセッション」がスレッドを切り替えるたびに積み上がって見えた）。
  // 生成中に離れた場合もバックエンドの処理は on_chat_end 経由で継続する（切断の
  // 検知が早まるだけで、従来の遅れて検知された場合と同じ経路）。
  useEffect(() => {
    const socket = session?.socket;
    if (!socket) return;
    const onPageHide = () => socket.close();
    // 戻る/進むでページが bfcache から復元された場合、閉じた接続は自動では
    // 繋ぎ直されないため、読み込み直して新しいセッションで接続する。
    const onPageShow = (ev: PageTransitionEvent) => {
      if (ev.persisted) window.location.reload();
    };
    window.addEventListener('pagehide', onPageHide);
    window.addEventListener('pageshow', onPageShow);
    return () => {
      window.removeEventListener('pagehide', onPageHide);
      window.removeEventListener('pageshow', onPageShow);
    };
  }, [session?.socket]);

  // 生成中に別スレッドへ移動すると、元のセッションはソケット切断され
  // ライブストリーミングが届かなくなる（バックエンド側の処理自体は
  // on_chat_end 経由でバックグラウンド継続するがpushする相手がいない）。
  // そのまま何も表示しないと「会話が終了したように見える」ため、開いている
  // スレッドが他セッションで処理中かをポーリングし、入力欄を無効化した上で
  // 完了を検知したらこのスレッドの最新スナップショットを取り直して確定内容を
  // 取り込む。
  //
  // 「他セッションで処理中」の判定は sessionIdState（このセッション自身の
  // Chainlit セッションID）をバックエンドへ渡し、生成中セッションと突き合わせて
  // もらう（app.py の /locohane/threads/{id}/status 参照）。以前は
  // useChatData().loading で自分自身のターンを除外していたが、Plan Mode承認待ち
  // 等の一時的な "ask" 中は自分自身のターンでも loading が false になる
  // （session.emit_call 経由でtask_end/task_startを挟むため）ため、
  // 自分自身の送信中にもかかわらず「他セッションで生成中」と誤検知し、
  // window.location.reload() が発火して自分自身のターンを見失い、URLに
  // ?thread が無い新規チャットとして再接続され続ける不具合があった
  // （2026-08-19 ユーザー報告: 送信するたびに無題の会話が増殖するバグ）。
  //
  // 完了検知時に以前はページ全体を window.location.reload() していたが、
  // 生成中のライブ中継自体は _make_relayed_emit（app.py）が既に担っているため、
  // 完了の瞬間にもう一度フルリロードする必要は薄い。ここでは
  // messagesState/elementState/tasklistState を直接置き換えるだけの軽量な
  // 再取得（syncThreadSnapshot）に置き換える（2026-08-22）。
  const [remoteGenerating, setRemoteGenerating] = useState(false);
  const wasRemoteGeneratingRef = useRef(false);

  const setMessages = useSetRecoilState(messagesState);
  const setDisplayElements = useSetRecoilState(elementState);
  const setTasklistElements = useSetRecoilState(tasklistState);

  const syncThreadSnapshot = useCallback(
    async (threadId: string) => {
      const thread = await fetchThreadSnapshot(threadId);
      setMessages(threadStepsToMessages(thread));
      const { tasklist, display } = splitThreadElements(thread);
      setTasklistElements(tasklist);
      setDisplayElements(display);
    },
    [setMessages, setDisplayElements, setTasklistElements]
  );

  useEffect(() => {
    wasRemoteGeneratingRef.current = false;
    setRemoteGenerating(false);
    if (!currentThreadId || !sessionId || !config?.dataPersistence) return;

    let cancelled = false;
    const poll = () => {
      const query = `?session_id=${encodeURIComponent(sessionId)}`;
      fetch(`${BACKEND_URL}/locohane/threads/${currentThreadId}/status${query}`, { credentials: 'include' })
        .then((res) => (res.ok ? res.json() : Promise.reject()))
        .then((data: { isGenerating: boolean }) => {
          if (cancelled) return;
          if (data.isGenerating) {
            wasRemoteGeneratingRef.current = true;
            setRemoteGenerating(true);
            return;
          }
          if (wasRemoteGeneratingRef.current) {
            // 取得に失敗した場合は wasRemoteGeneratingRef.current を true のまま
            // 残し、次のポーリングで再試行する（_make_relayed_emit と同じ
            // ベストエフォート方針）。remoteGenerating=true のままなので
            // 入力欄は無効化され続け、中途半端な状態で操作させることはない。
            syncThreadSnapshot(currentThreadId)
              .then(() => {
                if (cancelled) return;
                wasRemoteGeneratingRef.current = false;
                setRemoteGenerating(false);
                notifyRemoteCompleteRef.current();
              })
              .catch(() => {});
            return;
          }
          setRemoteGenerating(false);
        })
        .catch(() => {});
    };
    poll();
    const timer = setInterval(poll, REMOTE_GENERATING_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [currentThreadId, sessionId, config?.dataPersistence, syncThreadSnapshot]);

  // このセッションには生成中タスクへの参照（session.current_task）が無いため、
  // Chainlit純正の stopTask は機能しない。代わりにバックエンドへ「このスレッドの
  // 生成中タスクをcancel()して」と依頼する（app.pyの _stop_thread_generating参照）。
  const stopRemoteGenerating = () => {
    if (!currentThreadId) return;
    fetch(`${BACKEND_URL}/locohane/threads/${currentThreadId}/stop`, {
      method: 'POST',
      credentials: 'include'
    }).catch(() => {});
  };

  // 同じ所有者（匿名モードでは全会話が"anonymous"に一元化）が別スレッドで
  // 既に生成中の場合、新規チャット・他の会話履歴からの並列送信をフロント側でも
  // 事前に防ぐ（実際の拒否は on_message 側の _generating_owner_threads
  // チェックが最終防衛線。ここはUIへの反映のみ）。
  const [blockingThreadId, setBlockingThreadId] = useState<string | null>(null);
  useEffect(() => {
    if (!config?.dataPersistence) {
      setBlockingThreadId(null);
      return;
    }
    let cancelled = false;
    const poll = () => {
      const query = currentThreadId ? `?exclude=${encodeURIComponent(currentThreadId)}` : '';
      fetch(`${BACKEND_URL}/locohane/threads/generating${query}`, { credentials: 'include' })
        .then((res) => (res.ok ? res.json() : Promise.reject()))
        .then((data: { threadId: string | null }) => {
          if (!cancelled) setBlockingThreadId(data.threadId);
        })
        .catch(() => {});
    };
    poll();
    const timer = setInterval(poll, REMOTE_GENERATING_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [currentThreadId, config?.dataPersistence]);

  // 木を1回だけ辿って、画面に要る値をまとめて求める（messages が変わったときだけ）。
  const view = useMemo(() => deriveThreadView(messages), [messages]);
  const {
    mainMessages,
    sideSteps,
    tokenUsage,
    workDir,
    plan,
    starterPrompts,
    inputLimits,
    interrupted,
    maxDisplayMessages,
    maxDisplaySideSteps
  } = view;
  // 作業ディレクトリの変更UIは新規チャットの最初の送信前のみ有効にする。
  // 1通でもuser_messageが存在すれば（＝新規チャットで送信済み、または
  // 既存スレッドを再開）ロックする。
  const hasSentMessage = mainMessages.some((m) => m.type === 'user_message');

  // 表示専用の間引き。会話コンテキスト(LangGraph側のstate)やログには一切影響しない。
  // 上限0または未取得（値未着信/パース失敗）の場合は無制限（従来どおり全件描画）。
  const displayMessages = useMemo(
    () =>
      maxDisplayMessages && maxDisplayMessages > 0 ? mainMessages.slice(-maxDisplayMessages) : mainMessages,
    [mainMessages, maxDisplayMessages]
  );

  // サイドパネル（Step一覧）も同様に表示専用の間引きを適用する。
  const displaySideSteps = useMemo(
    () => (maxDisplaySideSteps && maxDisplaySideSteps > 0 ? sideSteps.slice(-maxDisplaySideSteps) : sideSteps),
    [sideSteps, maxDisplaySideSteps]
  );

  // 生成中に増え続ける古いStepを messagesState から外す（表示専用。messageTree.ts の pruneMessages）。
  // 毎回は調べず、前回調べたときの件数の2倍（最低 PRUNE_MIN_STEPS 件）を超えたときだけ間引く。
  // 件数は deriveThreadView が木を辿ったときに数えたもの（ここで木をもう一度辿らない）。
  // 間引きは今の messagesState に対して行う（描画した時点の messages を元に置き換えると、描画から effect までの
  // 間にソケットで届いた新しいStep・ストリーミングの文字が消えるため）。
  const prunedSizeRef = useRef(0);
  const stepCount = view.stepCount;
  useEffect(() => {
    if (!maxDisplayMessages && !maxDisplaySideSteps) return;
    if (stepCount <= Math.max(PRUNE_MIN_STEPS, prunedSizeRef.current * 2)) return;
    setMessages((current) => {
      const pruned = pruneMessages(current, maxDisplayMessages ?? 0, maxDisplaySideSteps ?? 0);
      prunedSizeRef.current = countSteps(pruned);
      return pruned;
    });
  }, [stepCount, maxDisplayMessages, maxDisplaySideSteps, setMessages]);
  // スレッドを切り替えたら数え直す。
  useEffect(() => {
    prunedSizeRef.current = 0;
  }, [currentThreadId]);

  // --- 前の会話の読み足し（会話欄を一番上までスクロールしたとき） ---
  // スレッドを開いたときは表示件数ぶんしか届かない（src/thread_display.py）ため、それより前をサーバーから
  // 少しずつ読む（app.py の /locohane/threads/{id}/older）。読み足した分は messagesState の外に持つ
  // （表示件数の間引き・pruneMessages・生成中の再計算の対象にしない＝増えても重くならない）。
  // 読み足した後に表示件数の窓から押し出されたメッセージも older へ移し、会話の途中が抜けないようにする。
  const [older, setOlder] = useState<IStep[]>(NO_STEPS);
  const [olderStatus, setOlderStatus] = useState<OlderStatus>(null);
  const olderRef = useRef(older);
  olderRef.current = older;
  const olderBusyRef = useRef(false);
  // 読み足したメッセージの要素（画像・ファイル）。elementState は再接続（resume_thread）や
  // syncThreadSnapshot で表示件数ぶんの要素に丸ごと置き換わるため、ここに持っておき、消えたら足し直す
  // （足し直さないと、画面に残っている読み足したメッセージの画像・ファイルのリンクだけが消える）。
  const [olderElements, setOlderElements] = useState<IMessageElement[]>([]);
  const displayElements = useRecoilValue(elementState);
  useEffect(() => {
    setOlder(NO_STEPS);
    setOlderStatus(null);
    setOlderElements([]);
    olderBusyRef.current = false;
  }, [currentThreadId]);
  useEffect(() => {
    if (olderElements.length === 0) return;
    const have = new Set(displayElements.map((e) => e.id));
    if (olderElements.every((e) => have.has(e.id))) return;
    setDisplayElements((prev) => {
      const present = new Set(prev.map((e) => e.id));
      const missing = olderElements.filter((e) => !present.has(e.id));
      return missing.length ? [...prev, ...missing] : prev;
    });
  }, [displayElements, olderElements, setDisplayElements]);
  const olderStatusRef = useRef(olderStatus);
  olderStatusRef.current = olderStatus;
  const prevWindowRef = useRef<{ thread: string | undefined; list: IStep[] }>({ thread: undefined, list: NO_STEPS });
  useEffect(() => {
    const prev = prevWindowRef.current;
    prevWindowRef.current = { thread: currentThreadId, list: displayMessages };
    if (prev.thread !== currentThreadId) return;
    const shown = new Set(displayMessages.map((m) => m.id));
    const dropped = prev.list.filter((m) => !shown.has(m.id));
    if (olderRef.current.length === 0) {
      // まだ何も読み足していない: 押し出された分はサーバーから読み直せる。「これより前は無い」と分かった後に
      // 押し出されたなら、その印を外して読み足しをもう一度できるようにする（外さないと二度と読めない）。
      if (dropped.length && olderStatusRef.current === 'done') setOlderStatus(null);
      return;
    }
    if (dropped.length) {
      setOlder((list) => {
        const have = new Set(list.map((m) => m.id));
        return [...list, ...dropped.filter((m) => !have.has(m.id))];
      });
    }
  }, [displayMessages, currentThreadId]);
  const shownMessages = useMemo(() => {
    if (older.length === 0) return displayMessages;
    const shown = new Set(displayMessages.map((m) => m.id));
    return [...older.filter((m) => !shown.has(m.id)), ...displayMessages];
  }, [older, displayMessages]);
  const loadOlder = useCallback(() => {
    const threadId = currentThreadId;
    const first = shownMessages[0];
    if (!threadId || !first || !config?.dataPersistence || olderBusyRef.current) return;
    olderBusyRef.current = true;
    setOlderStatus('loading');
    fetchOlderMessages(threadId, first.id, maxDisplayMessages && maxDisplayMessages > 0 ? maxDisplayMessages : 50)
      .then((result) => {
        if (threadId !== currentThreadIdRef.current) return;
        if (result.messages.length) {
          setOlder((list) => {
            const have = new Set(list.map((m) => m.id));
            return [...result.messages.filter((m) => !have.has(m.id)), ...list];
          });
          const fresh = result.elements.filter((e) => (e.type as string) !== 'avatar');
          if (fresh.length) {
            setOlderElements((prev) => {
              const have = new Set(prev.map((e) => e.id));
              return [...prev, ...fresh.filter((e) => !have.has(e.id))];
            });
          }
        }
        setOlderStatus(result.hasMore ? null : 'done');
      })
      .catch(() => {
        if (threadId === currentThreadIdRef.current) setOlderStatus('error');
      })
      .finally(() => {
        if (threadId === currentThreadIdRef.current) olderBusyRef.current = false;
      });
  }, [currentThreadId, shownMessages, config?.dataPersistence, maxDisplayMessages]);
  const currentThreadIdRef = useRef(currentThreadId);
  currentThreadIdRef.current = currentThreadId;

  const lastMain = mainMessages[mainMessages.length - 1];
  // 送信直後〜最終回答のストリーミング開始までの「空白時間」を可視化する。
  // 既に回答トークンが届き始めていれば streaming カーソル側で表現されるため不要。
  const showTyping = loading && !(lastMain?.type === 'assistant_message' && lastMain.streaming);
  // 「...」の横に出す、今している処理（サイドパネルで実行中のStep）。
  // 表示件数の間引き前の messages から辿る（間引きで実行中のStepが落ちないように）。
  const runningSteps = useMemo(() => (showTyping ? selectRunningStepChain(messages) : NO_STEPS), [showTyping, messages]);

  if (!authReady) return null;
  if (requireLogin && !isAuthenticated) return <LoginForm />;

  return (
    <div className="app-shell">
      <Sidebar mobile={isMobile} open={mobileDrawer === 'threads'} onClose={closeMobileDrawer} />
      <div className="main-column">
        <Header
          mobile={isMobile}
          onOpenThreads={config?.dataPersistence ? () => setMobileDrawer('threads') : undefined}
          onOpenDetails={() => setMobileDrawer('details')}
        />
        <MessagePane
          messages={shownMessages}
          showTyping={showTyping}
          runningSteps={runningSteps}
          olderStatus={olderStatus}
          onReachTop={currentThreadId && config?.dataPersistence ? loadOlder : undefined}
        />
        <AskActionBar />
        <AskFormBar />
        <AskChoiceFormBar />
        <CadViewerBar />
        <AskTextBar />
        <AskFileDropzone />
        {!hasSentMessage && (
          <StarterPrompts
            prompts={starterPrompts}
            attachments={attachments}
            onAttachmentsSent={handleAttachmentsSent}
          />
        )}
        <Composer
          plan={plan}
          remoteGenerating={remoteGenerating}
          onStopRemote={stopRemoteGenerating}
          blockedByOtherThread={blockingThreadId !== null}
          workDirEditable={!hasSentMessage}
          attachments={attachments}
          onAttach={handleAttach}
          onAttachPastedText={handleAttachPastedText}
          onRemoveAttachment={handleRemoveAttachment}
          onAttachmentsSent={handleAttachmentsSent}
          onRestoreAttachments={handleRestoreAttachments}
          inputLimits={inputLimits}
          interrupted={interrupted}
        />
      </div>
      <SidePanel
        sideSteps={displaySideSteps}
        tokenUsage={tokenUsage}
        workDir={workDir}
        plan={plan}
        mobile={isMobile}
        open={mobileDrawer === 'details'}
        onClose={closeMobileDrawer}
      />
      {isMobile && mobileDrawer ? <div className="drawer-backdrop" onClick={closeMobileDrawer} /> : null}
      {/* 初めて使うユーザー（会話が1件も無い）向けの使い方のチュートリアル。最後に実際に1回送ってもらう */}
      <ChatTutorial
        hasSentMessage={hasSentMessage}
        loading={loading}
        hasAnswer={lastMain?.type === 'assistant_message' && !lastMain.streaming}
        answerStarted={lastMain?.type === 'assistant_message'}
        hasStarters={!hasSentMessage && starterPrompts.length > 0}
        messageCount={mainMessages.length}
      />
    </div>
  );
}

export default App;
