import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * WebSocket client for raven/server.py's /ws protocol. Mirrors the message
 * shapes defined there exactly -- see server.py's own docstring for the
 * server-side half of this contract (Session.handle_message /
 * Session._confirm_run / Session._on_tool_call).
 */

export type Message = { role: 'user' | 'raven'; text: string };
export type ChatSession = { id: number; title: string; updated_at: number };
export type PendingConfirm = { id: string; prompt: string };
export type ToolEvent = { id: number; label: string };

type ServerMessage =
  | { type: 'history'; session: number; items: { role: 'user' | 'assistant'; text: string }[] }
  | { type: 'sessions'; current: number; items: ChatSession[] }
  | { type: 'reply'; text: string }
  | { type: 'tool_call'; name: string; args: Record<string, unknown> }
  | { type: 'confirm_request'; id: string; prompt: string }
  | { type: 'error'; message: string };

const SERVER_URL = 'ws://127.0.0.1:8756/ws';
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 10000;

const THINKING_WORDS = [
  "Working on it...",
  "Thinking...",
  "Ravening...",
  "Swimming...",
  "Calculating...",
  "Casting...",
  "Mogging...",
  "Smoking...",
  "Pasting...",
  "Burning...",
  "Evaporating...",
  "Hallucinating...",
  "Demotivating...",
  "Catastrophing...",
  "Ctrl+C-ing...",
  "Ctrl+V-ing...",
  "Whispering...",
  "Traveling...",
  "Cooking...",
  "Eating...",
  "Combusting...",
  "Vibe coding...",
  "Crushing...",

]

export function useRaven() {
  const [connected, setConnected] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false); // an ask is in flight
  const [statusText, setStatusText] = useState<string | null>(null);
  const [toolStatus, setToolStatus] = useState<string | null>(null);

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [currentSession, setCurrentSession] = useState<number | null>(null);
  const [toolLog, setToolLog] = useState<ToolEvent[]>([]); // tool calls of the current/last turn
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const toolId = useRef(0);

  // for randomly switching between thinking words
  useEffect(() => {
    if (!busy) {
      setStatusText(null);
      return;
    }
    if (toolStatus) {
      setStatusText(toolStatus);
      return;
    }
    const pick = () => THINKING_WORDS[Math.floor(Math.random() * THINKING_WORDS.length)];
    setStatusText(pick());
    const id = setInterval(() => setStatusText(pick()), 5000);
    return () => clearInterval(id);
  }, [busy, toolStatus]);

  useEffect(() => {
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let delay = RECONNECT_MIN_MS;

    const connect = () => {
      const ws = new WebSocket(SERVER_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        delay = RECONNECT_MIN_MS;
        setConnected(true);
        setError(null);
        ws.send(JSON.stringify({ type: 'get_history' }));
        ws.send(JSON.stringify({ type: 'list_sessions' }));
      };

      ws.onclose = () => {
        setConnected(false);
        // Whatever was in flight can never complete on a dead socket -- don't leave the UI "thinking" forever.
        setBusy(false);
        setToolStatus(null); 
        setPendingConfirm(null);
        if (disposed) return;
        retryTimer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, RECONNECT_MAX_MS);
      };

      // onerror is always followed by onclose, which owns the retry; just say what's wrong.
      ws.onerror = () => setError('Cannot reach R.A.V.E.N -- start it with: python -m raven.server (retrying...)');

      ws.onmessage = (event) => {
        let data: ServerMessage;
        try {
          data = JSON.parse(event.data);
        } catch {
          setError('Received an unreadable message from the server.');
          return;
        }
        switch (data.type) {
          case 'history':
            // The server's open chat: replaces what's shown (also on reconnect and when switching chats).
            setMessages(data.items.map((it) => ({ role: it.role === 'user' ? 'user' : 'raven', text: it.text })));
            setCurrentSession(data.session);
            setToolLog([]);
            setError(null);
            break;
          case 'sessions':
            setSessions(data.items);
            setCurrentSession(data.current);
            break;
          case 'reply':
            setBusy(false);
            setToolStatus(null); 
            setMessages((m) => [...m, { role: 'raven', text: data.text }]);
            break;
          case 'tool_call': {
            const label = toolStatusText(data.name, data.args);
            setToolStatus(label); 
            setToolLog((log) => [...log, { id: ++toolId.current, label }]);
            break;
          }
          case 'confirm_request':
            setPendingConfirm({ id: data.id, prompt: data.prompt });
            break;
          case 'error':
            setBusy(false);
            setToolStatus(null); 
            setError(data.message);
            break;
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      wsRef.current?.close();
    };
  }, []);

  const ask = useCallback((text: string) => {
    if (!text.trim()) return;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setError('Not connected to R.A.V.E.N -- your message was not sent.');
      return;
    }
    setError(null);
    setToolLog([]);
    setToolStatus(null)
    setMessages((m) => [...m, { role: 'user', text }]);
    setBusy(true);
    // setStatusText('Thinking');
    ws.send(JSON.stringify({ type: 'ask', text }));
  }, []);

  const respondToConfirm = useCallback(
    (approved: boolean) => {
      const ws = wsRef.current;
      if (!pendingConfirm || !ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: 'confirm_response', id: pendingConfirm.id, approved }));
      setPendingConfirm(null);
    },
    [pendingConfirm],
  );

  const dismissError = useCallback(() => setError(null), []);

  const sendControl = useCallback((payload: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setError('Not connected to R.A.V.E.N.');
      return;
    }
    ws.send(JSON.stringify(payload));
  }, []);
  const newSession = useCallback(() => sendControl({ type: 'new_session' }), [sendControl]);
  const openSession = useCallback((id: number) => sendControl({ type: 'open_session', id }), [sendControl]);
  const deleteSession = useCallback((id: number) => sendControl({ type: 'delete_session', id }), [sendControl]);

  return {
    connected, messages, busy, statusText, toolLog, pendingConfirm, error, sessions, currentSession,
    ask, respondToConfirm, dismissError, newSession, openSession, deleteSession,
  };
}

// Same spirit as cli.py's TOOL_STATUS -- a short, human phrase per tool,
// falling back to a generic one for anything not explicitly listed here.
function toolStatusText(name: string, args: Record<string, unknown>): string {
  const path = typeof args.path === 'string' ? args.path : undefined;
  switch (name) {
    case 'read_file':
      return `Reading ${path ?? 'a file'}`;
    case 'write_file':
      return `Writing ${path ?? 'a file'}`;
    case 'edit_file':
      return `Editing ${path ?? 'a file'}`;
    case 'grep':
      return `Searching "${args.pattern ?? ''}"`;
    case 'run_command':
      return `Running ${args.command ?? 'a command'}`;
    case 'load_skill':
      return `Loading skill ${args.name ?? ''}`;
    default:
      return name.replace(/_/g, ' ');
  }
}
