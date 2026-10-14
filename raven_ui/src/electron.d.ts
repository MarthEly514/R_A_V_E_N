// The bridge preload.ts exposes via contextBridge.exposeInMainWorld --
// see preload.ts and main.ts's ipcMain.handle('clipboard:...') handlers.
export {};

declare global {
  interface Window {
    electronClipboard: {
      writeText: (text: string) => Promise<void>;
      readText: () => Promise<string>;
    };
    electronApp: {
      getVersion: () => Promise<string>;
    };
  }
}
