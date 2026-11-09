// Exposes the real on-disk path of a dropped file, so the farm can read the .blend from where it lives
// (relative paths to textures and fonts keep working) instead of from an uploaded copy.
const { contextBridge, webUtils } = require('electron');
contextBridge.exposeInMainWorld('renderFarm', { pathFor: (file) => webUtils.getPathForFile(file) });
