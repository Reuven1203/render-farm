// Electron shell: starts server.py on a free local port and shows its UI in a window.
const { app, BrowserWindow, shell } = require('electron');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');

let py = null;

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); });
    s.on('error', reject);
  });
}

function waitForServer(port, tries = 50) {
  return new Promise((resolve, reject) => {
    const attempt = (n) => {
      const c = net.connect(port, '127.0.0.1');
      c.on('connect', () => { c.destroy(); resolve(); });
      c.on('error', () => (n > 0 ? setTimeout(() => attempt(n - 1), 100) : reject(new Error('server did not start'))));
    };
    attempt(tries);
  });
}

async function createWindow() {
  const port = await freePort();
  py = spawn('python3', ['-u', path.join(__dirname, 'server.py')], {
    cwd: __dirname,
    env: { ...process.env, PORT: String(port), PATH: `/opt/homebrew/bin:/usr/local/bin:${process.env.PATH}` },
    stdio: 'inherit',
  });
  py.on('exit', (code) => { if (code) console.error('server.py exited with', code); });
  await waitForServer(port);

  const win = new BrowserWindow({ width: 860, height: 900, title: 'Render Farm',
    backgroundColor: '#07070b',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true } });
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  win.loadURL(`http://127.0.0.1:${port}`);
}

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
// SIGINT lets server.py cancel in-flight GPU jobs before it exits
app.on('before-quit', () => { if (py) py.kill('SIGINT'); });
