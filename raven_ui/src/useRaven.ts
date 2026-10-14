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

// Mirrors raven/design.py's element schema exactly -- geometry (points/d/
// children) is always DERIVED server-side from params; the UI never
// computes it itself, only sends param/position patches and renders
// whatever the server hands back.
export type CanvasElement = {
  id: string;
  type: 'polygon' | 'star' | 'path' | 'group';
  generator?: string;
  x: number;
  y: number;
  rotation: number;
  scale: number;
  fill: string;
  stroke: string;
  stroke_width: number;
  z: number;
  params: Record<string, unknown>;
  points?: [number, number][];
  d?: string;
  children?: CanvasElement[];
};

type ServerMessage =
  | { type: 'history'; session: number; items: { role: 'user' | 'assistant'; text: string }[] }
  | { type: 'sessions'; current: number; items: ChatSession[] }
  | { type: 'reply'; text: string }
  | { type: 'tool_call'; name: string; args: Record<string, unknown> }
  | { type: 'confirm_request'; id: string; prompt: string }
  | { type: 'error'; message: string }
  | { type: 'stats'; segments: { name: string; text: string }[] }
  | { type: 'transcript'; id: string; text: string }
  | { type: 'transcript_error'; id: string; message: string }
  | { type: 'model_config'; provider: string; name: string }
  | { type: 'models'; provider: string; items: string[] }
  | { type: 'models_error'; provider: string; message: string }
  | { type: 'user_config'; name: string; has_key: Record<string, boolean>; version: string }
  | { type: 'cancel_requested' }
  | { type: 'cancelled' }
  | { type: 'canvas'; session: number; elements: CanvasElement[] }
  | { type: 'canvas_update'; element: CanvasElement }
  | { type: 'canvas_delete'; element_id: string };

export type StatSegment = { name: string; text: string };
export type ModelConfig = { provider: string; name: string };
// has_key never carries the actual secret -- see server.py's _send_user_config.
export type UserConfig = { name: string; has_key: Record<string, boolean>; version: string };


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
  // True from the moment cancel_ask is sent until the server confirms the
  // turn actually settled ("cancelled") -- the underlying call can't be
  // forcibly stopped (raven/server.py's _AskToken), so it keeps running in
  // the background for however long it takes; this just distinguishes
  // "waiting for a reply" from "waiting for a cancelled reply to be discarded"
  // in the UI. busy stays true the whole time either way -- see cancelAsk().
  const [cancelling, setCancelling] = useState(false);
  const [statusText, setStatusText] = useState<string | null>(null);
  const [toolStatus, setToolStatus] = useState<string | null>(null);
  const [stats, setStats] = useState<StatSegment[]>([]);

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [currentSession, setCurrentSession] = useState<number | null>(null);
  const [toolLog, setToolLog] = useState<ToolEvent[]>([]); // tool calls of the current/last turn
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [modelConfig, setModelConfig] = useState<ModelConfig | null>(null);
  const [modelsByProvider, setModelsByProvider] = useState<Record<string, string[]>>({});
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [userConfig, setUserConfig] = useState<UserConfig | null>(null);
  const [canvasElements, setCanvasElements] = useState<CanvasElement[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const toolId = useRef(0);
  // Pending push-to-talk transcriptions: id -> the promise's resolve/reject,
  // so requestTranscribe() can be awaited from VoiceStage even though the
  // answer arrives later as an ordinary, id-tagged websocket message.
  const pendingTranscriptions = useRef(new Map<string, { resolve: (text: string) => void; reject: (err: Error) => void }>());

  // for randomly switching between thinking words
  useEffect(() => {
    if (!busy) {
      setStatusText(null);
      return;
    }
    if (cancelling) {
      setStatusText('Cancelling...');
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
  }, [busy, toolStatus, cancelling]);

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
        setCancelling(false);
        setToolStatus(null);
        setPendingConfirm(null);
        setStats([]);
        // A dead socket can never deliver these -- fail every push-to-talk
        // still awaiting a transcript instead of leaving it hanging forever.
        pendingTranscriptions.current.forEach(({ reject }) => reject(new Error('Connection lost while transcribing.')));
        pendingTranscriptions.current.clear();
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
            setCancelling(false);
            setToolStatus(null);
            setMessages((m) => [...m, { role: 'raven', text: data.text }]);
            break;
          case 'tool_call': {
            const label = toolStatusText(data.name, data.args);
            setToolStatus(label);
            setToolLog((log) => [...log, { id: ++toolId.current, label }]);
            break;
          }
          case 'stats':
            setStats(data.segments);
            break;
          case 'transcript': {
            const pending = pendingTranscriptions.current.get(data.id);
            pendingTranscriptions.current.delete(data.id);
            pending?.resolve(data.text);
            break;
          }
          case 'transcript_error': {
            const pending = pendingTranscriptions.current.get(data.id);
            pendingTranscriptions.current.delete(data.id);
            pending?.reject(new Error(data.message));
            break;
          }
          case 'model_config':
            setModelConfig({ provider: data.provider, name: data.name });
            break;
          case 'models':
            setModelsLoading(false);
            setModelsError(null);
            setModelsByProvider((m) => ({ ...m, [data.provider]: data.items }));
            break;
          case 'models_error':
            setModelsLoading(false);
            setModelsError(data.message);
            break;
          case 'user_config':
            setUserConfig({ name: data.name, has_key: data.has_key, version: data.version });
            break;
          case 'confirm_request':
            setPendingConfirm({ id: data.id, prompt: data.prompt });
            break;
          case 'cancel_requested':
            setCancelling(true);
            // The server auto-denies (and stops sending) any confirm_request
            // for this turn from this point on -- drop whatever dialog is
            // showing rather than leaving it stuck on screen with no reply
            // ever coming for it.
            setPendingConfirm(null);
            break;
          case 'cancelled':
            setBusy(false);
            setCancelling(false);
            setToolStatus(null);
            // The server rolled this turn's user message + reply out of
            // live history -- mirror that here, or the user's own prompt
            // sits in the chat forever with no reply ever coming for it,
            // looking stuck rather than genuinely discarded. Only one ask
            // runs at a time, so the last message is always this turn's own.
            setMessages((m) => (m.length && m[m.length - 1].role === 'user' ? m.slice(0, -1) : m));
            break;
          case 'canvas':
            // The session's whole saved canvas -- replaces what's shown,
            // same "connect/switch restores everything" moment as 'history'.
            setCanvasElements(data.elements);
            break;
          case 'canvas_update': {
            // One created/edited/grouped element -- upsert by id rather than
            // replacing the whole array, so it can't clobber a DIFFERENT
            // element's in-flight local drag state elsewhere in the UI.
            const el = data.element;
            setCanvasElements((prev) => {
              const i = prev.findIndex((e) => e.id === el.id);
              if (i === -1) return [...prev, el];
              const next = prev.slice();
              next[i] = el;
              return next;
            });
            break;
          }
          case 'canvas_delete':
            setCanvasElements((prev) => prev.filter((e) => e.id !== data.element_id));
            break;
          case 'error':
            setBusy(false);
            setCancelling(false);
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

  /**
   * Cancel the in-flight ask(), if any. The underlying call can't actually be
   * stopped (see server.py's _AskToken) -- this just tells the server to
   * discard whatever it eventually returns, and switches the UI from
   * "Thinking" to "Cancelling..." until that settles ("cancelled" comes
   * back). Composer stays disabled the whole time: allowing a new message
   * before the old one truly settles would risk two turns mutating history
   * at once.
   */
  const cancelAsk = useCallback(() => {
    const ws = wsRef.current;
    if (!busy || cancelling || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'cancel_ask' }));
  }, [busy, cancelling]);

  /**
   * Send captured audio (16-bit PCM, little-endian, mono, base64-encoded --
   * see VoiceStage.tsx for the capture side) for transcription and resolve
   * with the resulting text. Deliberately NOT surfaced through the shared
   * `error` banner -- a failed push-to-talk (mic denied, model not
   * downloaded) is VoiceStage's own concern, not something that should
   * interrupt whatever's shown in the Chat stage.
   */
  const requestTranscribe = useCallback((audioBase64: string): Promise<string> => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error('Not connected to R.A.V.E.N.'));
    }
    const id = `t${++toolId.current}-${Date.now()}`;
    return new Promise<string>((resolve, reject) => {
      pendingTranscriptions.current.set(id, { resolve, reject });
      ws.send(JSON.stringify({ type: 'transcribe', audio: audioBase64, id }));
    });
  }, []);

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

  // Model provider/name (SettingsPanel.tsx): current config, the NVIDIA
  // catalog for a given API key, and switching. Plain request/state (not a
  // promise, unlike requestTranscribe) -- Settings is a view, not something
  // driven imperatively from a single call site.
  const getModelConfig = useCallback(() => sendControl({ type: 'get_model_config' }), [sendControl]);
  const listModels = useCallback(
    (provider: string) => {
      setModelsLoading(true);
      setModelsError(null);
      sendControl({ type: 'list_models', provider });
    },
    [sendControl],
  );
  const setModel = useCallback(
    (provider: string, name: string) => sendControl({ type: 'set_model', provider, name }),
    [sendControl],
  );

  // User name + API keys (SettingsPanel.tsx): lets a fresh install be fully
  // configured without touching .env or settings.json by hand. setApiKey
  // never reads the key back -- user_config only ever reports has_key booleans.
  const getUserConfig = useCallback(() => sendControl({ type: 'get_user_config' }), [sendControl]);
  const setApiKey = useCallback(
    (provider: string, key: string) => sendControl({ type: 'set_api_key', provider, key }),
    [sendControl],
  );
  const setUserName = useCallback(
    (name: string) => sendControl({ type: 'set_user_name', name }),
    [sendControl],
  );

  // Design mode (DesignStage.tsx): the canvas itself lives here (canvasElements,
  // pushed/patched by the 'canvas'/'canvas_update'/'canvas_delete' cases
  // above); selection, pan/zoom, and undo history are ephemeral UI-only state
  // that stays local to DesignStage, same reasoning ChatStage keeps its own
  // edit-draft state instead of putting it in this hook.
  const getCanvas = useCallback(() => sendControl({ type: 'get_canvas' }), [sendControl]);
  const editCanvasElement = useCallback(
    (elementId: string, patch: Record<string, unknown>) =>
      sendControl({ type: 'canvas_edit', element_id: elementId, patch }),
    [sendControl],
  );
  const deleteCanvasElement = useCallback(
    (elementId: string) => sendControl({ type: 'canvas_delete', element_id: elementId }),
    [sendControl],
  );
  const groupCanvasElements = useCallback(
    (elementIds: string[]) => sendControl({ type: 'canvas_group', element_ids: elementIds }),
    [sendControl],
  );
  const ungroupCanvasElement = useCallback(
    (groupId: string) => sendControl({ type: 'canvas_ungroup', group_id: groupId }),
    [sendControl],
  );
  // Undo's own restore-a-deletion step: the client already holds the exact
  // element it asked to delete, so it just hands it straight back.
  const restoreCanvasElement = useCallback(
    (element: CanvasElement) => sendControl({ type: 'canvas_restore', element }),
    [sendControl],
  );

  return {
    connected, messages, busy, cancelling, statusText, toolLog, pendingConfirm, error, sessions, currentSession, stats,
    modelConfig, modelsByProvider, modelsLoading, modelsError, userConfig, canvasElements,
    ask, cancelAsk, respondToConfirm, dismissError, newSession, openSession, deleteSession, requestTranscribe,
    getModelConfig, listModels, setModel, getUserConfig, setApiKey, setUserName,
    getCanvas, editCanvasElement, deleteCanvasElement, groupCanvasElements, ungroupCanvasElement,
    restoreCanvasElement,
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
    case 'draw_polygon':
      return `Drawing a ${args.sides ?? 6}-sided polygon`;
    case 'draw_star':
      return `Drawing a ${args.points ?? 5}-pointed star`;
    case 'draw_golden_rectangle':
      return 'Drawing a golden rectangle';
    case 'draw_golden_spiral':
      return 'Drawing a golden spiral';
    case 'draw_radial_pattern':
      return `Drawing a radial pattern (${args.motif ?? 'a shape'})`;
    case 'draw_grid':
      return 'Drawing a grid';
    case 'draw_bezier':
      return 'Drawing a Bezier curve';
    case 'draw_lissajous':
      return 'Drawing a Lissajous curve';
    case 'list_canvas_elements':
      return 'Looking at the canvas';
    case 'update_canvas_element':
      return `Updating shape ${args.element_id ?? ''}`;
    case 'delete_canvas_element':
      return `Deleting shape ${args.element_id ?? ''}`;
    default:
      return name.replace(/_/g, ' ');
  }
}
