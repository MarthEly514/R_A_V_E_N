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


/**
 * One chat message. memo()'d on purpose: rendering a reply means parsing markdown and
 * syntax-highlighting its code, which is slow, and a long history can hold hundreds of
 * them. Without memo every keystroke in the input re-rendered (and re-parsed) ALL of
 * them -- measured at ~800 ms per keypress with 200 messages.
 */
// const MessageBubble = memo(function MessageBubble({ m }: { m: Message }) {
//   return (
//     <div
//       className={[
//         'max-w-[78%] px-4 py-3 text-[0.92rem] leading-normal [overflow-wrap:anywhere]',
//         m.role === 'user' ? 'self-end bg-ink whitespace-pre-wrap text-white' : 'markdown self-start border border-line bg-panel',
//       ].join(' ')}
//     >
//       {m.role === 'raven' ? (
//         // No rehype-raw: raw HTML in a reply stays inert text, never live markup.
//         <Markdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
//           {m.text}
//         </Markdown>
//       ) : (
//         m.text
//       )}
//     </div>
//   );
// });

/**
 * The text box owns its own state, so typing re-renders ONLY this component, not the
 * whole App (header, rails, every message).
 */
// function Composer({
//   connected, busy, blocked, onSend,
// }: {
//   connected: boolean;
//   busy: boolean;
//   blocked: boolean; // an approval is pending
//   onSend: (text: string) => void;
// }) {
//   const [input, setInput] = useState('');
//   const send = () => {
//     if (!input.trim() || busy) return;
//     onSend(input);
//     setInput('');
//   };
//   return (
//     <div className="flex gap-2.5 border-t border-line-soft py-3.5">
//       <input
//         value={input}
//         onChange={(e) => setInput(e.target.value)}
//         onKeyDown={(e) => e.key === 'Enter' && send()}
//         placeholder={!connected ? 'Waiting for R.A.V.E.N...' : busy ? 'R.A.V.E.N is working...' : 'Message R.A.V.E.N'}
//         disabled={!connected || blocked}
//         autoFocus
//         className="flex-1 border border-line bg-panel px-3.5 py-2.5 text-[0.92rem] outline-none focus:border-ink disabled:cursor-not-allowed disabled:opacity-45"
//       />
//       <button
//         onClick={send}
//         disabled={!connected || busy || blocked || !input.trim()}
//         className="cursor-pointer bg-ink px-6 text-[0.85rem] font-semibold text-white disabled:cursor-not-allowed disabled:opacity-45"
//       >
//         Send
//       </button>
//     </div>
//   );
// }

/**
 * Layout follows UI_Model.jpg: date/year header, a left rail (Activity | History),
 * a central stage (the conversation), a right rail (status + approvals), the
 * logo centred in the header and on the empty stage. Styled with Tailwind
 * utilities; tokens live in index.css's @theme.
 */
export default function App() {
  const {
    connected, messages, busy, statusText, toolLog, pendingConfirm, error, sessions, currentSession,
    ask, respondToConfirm, dismissError, newSession, openSession, deleteSession,
  } = useRaven();
  const [tab, setTab] = useState<'activity' | 'history'>('activity');
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, statusText, error, pendingConfirm]);

  const canSwitch = connected && !busy;

  const removeSession = (id: number, title: string) => {
    if (window.confirm(`Delete the chat "${title}"? This can't be undone.`)) deleteSession(id);
  };

  const now = new Date();
  const dayName = now.toLocaleDateString('en-GB', { weekday: 'short' });
  const dayMonth = now.toLocaleDateString('en-GB', { day: 'numeric', month: 'long' });

  const tabClass = (on: boolean) =>
    `cursor-pointer border-b pb-1 text-[0.78rem] ${on ? 'border-ink text-ink' : 'border-transparent text-ink-dim'
    }`;

  return (
    <div className="relative flex h-full flex-col px-10 pt-6 pb-5">
      <header className="flex shrink-0 items-center gap-5 text-[0.95rem]">
        <span>{dayName}</span>
        <span className="h-px w-9 bg-ink" />
        <span>{dayMonth}</span>
        <span className="flex-1" />
        <span>{now.getFullYear()}</span>
        {/* The logo is white artwork; brightness-0 turns it ink-black to fit the light theme, keeping its alpha. */}
        <img
          className="absolute top-[1.1rem] left-1/2 h-[1.9rem] -translate-x-1/2 brightness-0"
          src={logo}
          alt="R.A.V.E.N"
        />
      </header>

      <div className="mt-6 mb-12 grid min-h-0 flex-1 grid-cols-[13rem_1fr_15rem] gap-10 max-[900px]:grid-cols-1">
        {/* Left rail */}
        <aside className="min-h-0 overflow-y-auto max-[900px]:hidden noScrollbar">
          <button
            onClick={newSession}
            disabled={!canSwitch}
            className="mb-5 w-full cursor-pointer border border-ink px-3 py-2 text-left text-[0.83rem] font-semibold hover:bg-ink hover:text-white disabled:cursor-not-allowed disabled:opacity-45"
          >
            + New chat
          </button>
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
                        ? 'border-ink bg-ink font-semibold text-white'
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
                <li key={c.id} className="group flex items-stretch">
                  <button
                    onClick={() => openSession(c.id)}
                    disabled={!canSwitch}
                    title={c.title}
                    className={`min-w-0 flex-1 cursor-pointer overflow-hidden border-l px-3 py-2 text-left text-[0.83rem] text-ellipsis whitespace-nowrap disabled:cursor-not-allowed ${c.id === currentSession
                        ? 'border-ink bg-ink font-semibold text-white'
                        : 'border-line hover:border-ink hover:bg-ink hover:text-white'
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
        </aside>

        {/* Stage */}
        <main className="flex min-h-0 flex-col border-y border-line-soft">
          <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-1.5 py-5 noScrollbar">
            {messages.length === 0 ? (
              <div className="m-auto flex flex-col items-center gap-[1.1rem] text-[0.95rem] tracking-wide text-ink-dim">
                <img className="w-36 opacity-85 brightness-0" src={logo} alt="R.A.V.E.N" />
                <span>Ask anything.</span>
              </div>
            ) : (
              messages.map((m, i) => <MessageBubble key={i} m={m} />)
            )}
            {busy && statusText && <div className="self-start text-[0.82rem] text-ink-dim">{statusText}...</div>}
            {error && (
              <div role="alert" className="flex justify-between gap-4 self-stretch border border-danger px-3 py-2 text-[0.85rem] text-danger">
                <span>{error}</span>
                <button onClick={dismissError} aria-label="Dismiss error" className="cursor-pointer text-[1.1rem] leading-none">
                  ×
                </button>
              </div>
            )}
            <div ref={endRef} />
          </div>

          <Composer connected={connected} busy={busy} blocked={!!pendingConfirm} onSend={ask} />
        </main>

        {/* Right rail */}
        <aside className="min-h-0 overflow-y-auto max-[900px]:hidden">
          <div className="mb-3.5 text-[0.78rem] text-ink-dim">Status</div>
          <div className="flex items-center gap-2.5 text-[0.9rem]">
            <span className={`size-2 rounded-full border border-ink ${connected ? 'bg-ink' : ''}`} />
            {connected ? 'Connected' : 'Reconnecting...'}
          </div>
          <StatsBlock />

          {pendingConfirm && (
            <div className="mt-6 border border-ink bg-panel p-3.5">
              <div className="mb-3.5 text-[0.78rem] text-ink-dim">Approval needed</div>
              <pre className="mb-3.5 max-h-56 overflow-y-auto font-[inherit] text-[0.82rem] whitespace-pre-wrap wrap-anywhere">
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
                  className="flex-1 cursor-pointer border border-ink bg-ink p-2 text-[0.8rem] font-semibold text-white"
                >
                  Approve
                </button>
              </div>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
