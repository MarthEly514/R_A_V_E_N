import { app, BrowserWindow, clipboard, ipcMain, session } from 'electron';
import path from 'node:path';
import started from 'electron-squirrel-startup';
import { updateElectronApp } from 'update-electron-app';
import { startServer, stopServer } from './serverProcess';

// Handle creating/removing shortcuts on Windows when installing/uninstalling.
if (started) {
  app.quit();
}

// In-app update, no manual reinstall: checks update.electronjs.org (which
// reads this repo's GitHub Releases) at startup and every 10 minutes, downloads
// silently, then prompts to restart. Only works where Squirrel can replace the
// running app in place -- Windows (MakerSquirrel) and signed macOS builds. Our
// macOS build isn't code-signed and Linux ships .deb/.rpm (OS package manager's
// job, not ours), so this is a deliberate no-op there, not a bug.
if (process.platform === 'win32') {
  updateElectronApp({ repo: 'MarthEly514/R_A_V_E_N' });
}

const createWindow = () => {
  // Create the browser window.
  const mainWindow = new BrowserWindow({
    width: 1180,
    height: 780,
    minWidth: 720,
    minHeight: 520,
    backgroundColor: '#e3e3e3',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  // and load the index.html of the app.
  if (MAIN_WINDOW_VITE_DEV_SERVER_URL) {
    mainWindow.loadURL(MAIN_WINDOW_VITE_DEV_SERVER_URL);
  } else {
    mainWindow.loadFile(
      path.join(__dirname, `../renderer/${MAIN_WINDOW_VITE_NAME}/index.html`),
    );
  }

  // DevTools only while developing.
  if (MAIN_WINDOW_VITE_DEV_SERVER_URL) {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }
};

// This method will be called when Electron has finished
// initialization and is ready to create browser windows.
// Some APIs can only be used after this event occurs.
app.on('ready', () => {
  // Push-to-talk (VoiceStage.tsx) calls getUserMedia({audio:true}) from the
  // renderer. Electron denies permission requests it hasn't been told about,
  // so without this the mic prompt would never resolve at all -- only our
  // own window ever asks, so granting 'media'/clipboard outright is safe here.
  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(permission === 'media' || permission === 'clipboard-read' || permission === 'clipboard-sanitized-write');
  });
  // Copy/paste (MessageBubble.tsx): routed through the MAIN process rather
  // than calling electron's `clipboard` module directly from preload --
  // preload's own copy of `require('electron')` isn't guaranteed to expose
  // every native module the same way the main process's always does (this
  // is what broke it originally: calling it from preload threw "Cannot read
  // properties of undefined" reaching for `clipboard` inside the bridged
  // function, even though the outer bridge object itself looked fine). The
  // main process's `clipboard` import here is never sandboxed/restricted.
  ipcMain.handle('clipboard:writeText', (_event, text: string) => clipboard.writeText(text));
  ipcMain.handle('clipboard:readText', () => clipboard.readText());
  // Settings panel's "App" version line -- app.getVersion() reads the packaged
  // app's own package.json, which is the actual installed desktop build, not
  // just whatever raven_ui/package.json says in source.
  ipcMain.handle('app:getVersion', () => app.getVersion());
  void startServer(); // the UI reconnects on its own, so no need to wait for it
  createWindow();
});

app.on('will-quit', stopServer);

// Quit when all windows are closed, except on macOS. There, it's common
// for applications and their menu bar to stay active until the user quits
// explicitly with Cmd + Q.
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  // On OS X it's common to re-create a window in the app when the
  // dock icon is clicked and there are no other windows open.
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});

// In this file you can include the rest of your app's specific main process
// code. You can also put them in separate files and import them here.
