// See the Electron documentation for details on how to use preload scripts:
// https://www.electronjs.org/docs/latest/tutorial/process-model#preload-scripts

const { contextBridge, ipcRenderer } = require('electron');

// Routed through the main process (main.ts's ipcMain.handle) rather than
// calling electron's `clipboard` module directly here -- see main.ts's own
// comment on the `clipboard:writeText` handler for why that broke.
contextBridge.exposeInMainWorld('electronClipboard', {
    writeText: (text: string) => ipcRenderer.invoke('clipboard:writeText', text),
    readText: () => ipcRenderer.invoke('clipboard:readText'),
});

contextBridge.exposeInMainWorld('electronApp', {
    getVersion: () => ipcRenderer.invoke('app:getVersion'),
});
