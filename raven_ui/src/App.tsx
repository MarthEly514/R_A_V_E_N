import { memo, useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
// import 'highlight.js/styles/github-dark.css';
import logo from './assets/logo.png';
import { useRaven, type Message } from './useRaven';
import MessageBubble from './ui/MessageBubble';
import Composer from './ui/Composer';
import StatsBlock from './ui/StatsBlock';
import ChatStage from './stages/ChatStage';
import VoiceStage from './stages/VoiceStage';
import DesignStage from './stages/DesignStage';
import SettingsPanel from './ui/SettingsPanel';
import { useApplyTheme, useSettings } from './useSettings';


/**
 * One chat message. memo()'d on purpose: rendering a reply means parsing markdown and
 * syntax-highlighting its code, which is slow, and a long history can hold hundreds of
 * them. Without memo every keystroke in the input re-rendered (and re-parsed) ALL of
 * them -- measured at ~800 ms per keypress with 200 messages.
 */

/**
 * The text box owns its own state, so typing re-renders ONLY this component, not the
 * whole App (header, rails, every message).
 */

/**
 * Layout follows UI_Model.jpg: date/year header, a left rail (Activity | History),
 * a central stage (the conversation), a right rail (status + approvals), the
 * logo centred in the header and on the empty stage. Styled with Tailwind
 * utilities; tokens live in index.css's @theme.
 */
export default function App() {
  const {
    connected, messages, busy, cancelling, statusText, toolLog, pendingConfirm, error, sessions, currentSession, stats,
    modelConfig, modelsByProvider, modelsLoading, modelsError, userConfig, canvasElements,
    ask, cancelAsk, respondToConfirm, dismissError, newSession, openSession, deleteSession, requestTranscribe,
    getModelConfig, listModels, setModel, getUserConfig, setApiKey, setUserName,
    editCanvasElement, deleteCanvasElement, groupCanvasElements, ungroupCanvasElement, restoreCanvasElement,
  } = useRaven();
  const [tab, setTab] = useState<'activity' | 'history'>('activity');
  const [mode, setMode] = useState<'chat' | 'voice' | 'design'>('chat');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const { theme, showStats } = useSettings();
  useApplyTheme(theme);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, statusText, error, pendingConfirm]);

  const canSwitch = connected && !busy;

  const removeSession = (id: number, title: string) => {
    if (window.confirm(`Delete the chat "${title}"? This can't be undone.`)) deleteSession(id);
  };

  // console.log(stats);

  const now = new Date();
  const dayName = now.toLocaleDateString('en-GB', { weekday: 'short' });
  const dayMonth = now.toLocaleDateString('en-GB', { day: 'numeric', month: 'long' });

  const tabClass = (on: boolean) =>
    `cursor-pointer border-b-2 pb-1 text-[0.85rem] ${on ? 'border-ink text-ink' : 'border-transparent text-ink-dim'
    }`;

  const modeClass = (on: boolean) =>
    `cursor-pointer px-3 py-0.5 text-[0.95rem] font-semibold w-full flex items-center gap-4 hover:text-ink transition-colors duration-200 ${on ? 'text-ink border-l-2 ' : 'text-ink-dim'
    }`;


  return (
    <div className="relative flex h-full flex-col px-10 pt-6 pb-5">
      <header className="flex shrink-0 items-center gap-5 text-[1.35rem] font-header">
        <span>{dayName}</span>
        <span className="h-px w-9 bg-ink" />
        <span>{dayMonth}</span>
        <span className="flex-1" />
        <span>{now.getFullYear()}</span>
        {/* .raven-logo (index.css): the artwork is white, so light mode blackens it and dark mode leaves it as-is. */}
        <img
          className="raven-logo absolute top-[1.1rem] left-1/2 h-[1.9rem] -translate-x-1/2"
          src={logo}
          alt="R.A.V.E.N"
        />
      </header>

      <div className="mt-6 mb-12 grid min-h-0 flex-1 grid-cols-[13rem_1fr_15rem] gap-10 max-[900px]:grid-cols-1 font-sora">
        {/* Left rail */}
        <aside className="min-h-0 relative overflow-y-auto max-[900px]:hidden noScrollbar">
          <button
            onClick={newSession}
            disabled={!canSwitch}
            className="mb-5 w-full cursor-pointer border-2 border-ink px-3 py-2 text-left text-[1rem] font-semibold hover:bg-strong hover:text-onstrong disabled:cursor-not-allowed disabled:opacity-45 transition-colors duration-200"
          >
            + New chat
          </button>

          {/* Activity and history  */}
          <div className='h-[45%] overflow-auto noScrollbar mb-6'>
            <div className="mb-3.5 flex gap-5" role="tablist">
              <button role="tab" aria-selected={tab === 'activity'} className={tabClass(tab === 'activity')} onClick={() => setTab('activity')}>
                Activity
              </button>
              <button role="tab" aria-selected={tab === 'history'} className={tabClass(tab === 'history')} onClick={() => setTab('history')}>
                History
              </button>
            </div>

            {tab === 'activity' ? (
              toolLog.length === 0 ? (
                <div className="text-[0.85rem] text-ink-dim">{busy ? 'Thinking' : 'Nothing running'}</div>
              ) : (
                <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
                  {toolLog.map((t, i) => (
                    <li
                      key={t.id}
                      className={`border-l px-3 py-2 text-[0.85rem] wrap-break-words ${busy && i === toolLog.length - 1
                        ? 'border-strong bg-strong font-semibold text-onstrong'
                        : 'border-line'
                        }`}
                    >
                      {t.label}
                    </li>
                  ))}
                </ul>
              )
            ) : sessions.length === 0 ? (
              <div className="text-[0.85rem] text-ink-dim">No chats yet</div>
            ) : (
              <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
                {sessions.map((c) => (
                  <li key={c.id} className="group flex items-stretch border-2 border-transparent hover:border-ink">
                    <button
                      onClick={() => openSession(c.id)}
                      disabled={!canSwitch}
                      title={c.title}
                      className={`min-w-0 flex-1 cursor-pointer overflow-hidden border-l-2 px-3 py-2 text-left text-[0.83rem] text-ellipsis whitespace-nowrap disabled:cursor-not-allowed ${c.id === currentSession
                        ? 'border-strong bg-strong font-semibold text-onstrong'
                        : 'border-ink'
                        }`}
                    >
                      {c.title}
                    </button>
                    <button
                      onClick={() => removeSession(c.id, c.title)}
                      disabled={!canSwitch}
                      aria-label={`Delete chat ${c.title}`}
                      className="hidden cursor-pointer px-2 text-ink-dim hover:text-danger group-hover:block disabled:cursor-not-allowed"
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* Modes */}
          <div>
            <div className="mb-3.5 text-[0.85rem] gap-2 text-ink-dim flex flex-row items-center justify-center">
              <p className='w-max'>
                Modes
              </p>
              <div className='w-full h-px bg-ink'></div>
            </div>
            <ul className='flex flex-col gap-4 items-start'>
              <button aria-selected={mode === 'chat'} className={modeClass(mode === 'chat')} onClick={() => setMode('chat')}>Chat</button>
              <button aria-selected={mode === 'voice'} className={modeClass(mode === 'voice')} onClick={() => setMode('voice')}>Voice</button>
              <button aria-selected={mode === 'design'} className={modeClass(mode === 'design')} onClick={() => setMode('design')}>Designer</button>

            </ul>
          </div>

          <button
            onClick={() => setSettingsOpen(true)}
            className="mb-5 w-full absolute bottom-0 left-0 cursor-pointer bg-ink-dim/20 border-ink px-3 py-2 text-left text-[1rem] font-semibold hover:bg-strong hover:text-onstrong transition-colors duration-200"
          >
            Settings
          </button>
        </aside>

        {/* Stage */}
        {mode === 'chat' && <ChatStage
          messages={messages}
          busy={busy}
          cancelling={cancelling}
          statusText={statusText}
          error={error}
          connected={connected}
          pendingConfirm={pendingConfirm}
          onSend={ask}
          onCancel={cancelAsk}
          onDismissError={dismissError}
        />}
        {mode === 'voice' && <VoiceStage
          connected={connected}
          busy={busy}
          requestTranscribe={requestTranscribe}
          onSend={ask}
        />}
        {mode === 'design' && <DesignStage
          canvasElements={canvasElements}
          busy={busy}
          cancelling={cancelling}
          statusText={statusText}
          error={error}
          connected={connected}
          pendingConfirm={pendingConfirm}
          onSend={ask}
          onCancel={cancelAsk}
          onDismissError={dismissError}
          editCanvasElement={editCanvasElement}
          deleteCanvasElement={deleteCanvasElement}
          groupCanvasElements={groupCanvasElements}
          ungroupCanvasElement={ungroupCanvasElement}
          restoreCanvasElement={restoreCanvasElement}
        />}

        {/* Right rail */}
        <aside className="min-h-0 overflow-y-auto max-[900px]:hidden">
          <div className="mb-3.5 text-[0.85rem] text-ink-dim flex flex-row items-center justify-center gap-2">
            <span className='w-max text-nowrap'>
              Status
            </span>
            <div className='w-full h-px bg-ink'></div>
          </div>
          <div className="flex items-center gap-2.5 text-[0.9rem] mb-5">
            <span className={`size-2 rounded-full border border-ink ${connected ? 'bg-ink' : ''}`} />
            {connected ? 'Connected' : 'Reconnecting...'}
          </div>
          {showStats && (
            <>
              <div className="mb-3.5 text-[0.85rem] text-ink-dim flex flex-row items-center justify-center gap-2">
                <p className='w-max text-nowrap'>
                  Model & Stats
                </p>
                <div className='w-full h-px bg-ink'></div>
              </div>
              <StatsBlock segments={stats} />
            </>
          )}

          {pendingConfirm && (
            <div className="mt-6 border border-ink bg-panel p-3.5">
              <div className="mb-3.5 text-[0.78rem] text-ink-dim">Approval needed</div>
              <pre className="mb-3.5 max-h-56 overflow-y-auto font-[inherit] noScrollbar text-[0.82rem] whitespace-pre-wrap wrap-anywhere">
                {pendingConfirm.prompt}
              </pre>
              <div className="flex gap-2">
                <button
                  onClick={() => respondToConfirm(false)}
                  className="flex-1 cursor-pointer border border-ink p-2 text-[0.8rem] font-semibold text-ink"
                >
                  Deny
                </button>
                <button
                  onClick={() => respondToConfirm(true)}
                  className="flex-1 cursor-pointer border border-ink bg-strong p-2 text-[0.8rem] font-semibold text-onstrong"
                >
                  Approve
                </button>
              </div>
            </div>
          )}
        </aside>
      </div>

      {settingsOpen && (
        <SettingsPanel
          onClose={() => setSettingsOpen(false)}
          connected={connected}
          modelConfig={modelConfig}
          modelsByProvider={modelsByProvider}
          modelsLoading={modelsLoading}
          modelsError={modelsError}
          getModelConfig={getModelConfig}
          listModels={listModels}
          setModel={setModel}
          userConfig={userConfig}
          getUserConfig={getUserConfig}
          setApiKey={setApiKey}
          setUserName={setUserName}
        />
      )}
    </div>
  );
}
