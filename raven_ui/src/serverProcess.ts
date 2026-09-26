import { spawn, ChildProcess } from 'node:child_process';
import net from 'node:net';

/**
 * Starts R.A.V.E.N's Python server (raven/server.py) for the desktop app, so
 * users don't have to run `raven-server` by hand -- and stops it again on quit.
 * If something is already listening on the port (a server you started yourself,
 * or a second app window) it is reused and never killed by us.
 *
 * Needs the Python package installed: `pip install .` from the repo (or
 * `pip install raven` once published). Set RAVEN_PYTHON to pick an interpreter.
 */
export const SERVER_HOST = '127.0.0.1';
export const SERVER_PORT = 8756;

export type Candidate = { command: string; args: string[] };

/** Interpreters to try, in order, for this OS. Pure function so it's testable. */
export function pythonCandidates(
  platform: NodeJS.Platform,
  env: NodeJS.ProcessEnv,
): Candidate[] {
  const tail = ['-m', 'raven.server'];
  const list: Candidate[] = [];
  if (env.RAVEN_PYTHON) list.push({ command: env.RAVEN_PYTHON, args: tail });
  if (platform === 'win32') {
    list.push({ command: 'py', args: ['-3', ...tail] }, { command: 'python', args: tail });
  } else {
    list.push({ command: 'python3', args: tail }, { command: 'python', args: tail });
  }
  return list;
}

/** True if something already accepts TCP connections on host:port. */
export function isListening(host: string, port: number, timeoutMs = 700): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port });
    const done = (ok: boolean) => {
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(timeoutMs, () => done(false));
    socket.once('connect', () => done(true));
    socket.once('error', () => done(false));
  });
}

let child: ChildProcess | null = null;

function tryStart(candidates: Candidate[]): void {
  const [next, ...rest] = candidates;
  if (!next) {
    console.error(
      '[raven] Could not start the Python server. Install it (pip install .) or run: python -m raven.server',
    );
    return;
  }
  const proc: ChildProcess = spawn(next.command, next.args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  let started = true;
  proc.once('error', () => {
    // ENOENT etc: this interpreter isn't there; try the next candidate.
    started = false;
    tryStart(rest);
  });
  proc.once('spawn', () => {
    if (started) child = proc;
  });
  proc.stdout?.on('data', (d) => console.log(`[raven-server] ${d}`.trimEnd()));
  proc.stderr?.on('data', (d) => console.log(`[raven-server] ${d}`.trimEnd()));
  proc.once('exit', (code: number | null) => {
    if (child === proc) child = null;
    if (code) console.error(`[raven] server exited with code ${code}`);
  });
}

export async function startServer(): Promise<void> {
  if (await isListening(SERVER_HOST, SERVER_PORT)) return; // reuse whatever is already running
  tryStart(pythonCandidates(process.platform, process.env));
}

export function stopServer(): void {
  if (child) {
    child.kill();
    child = null;
  }
}
