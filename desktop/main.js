// LostPath 桌面壳：启动引擎（conda 环境）→ 等端口就绪 → 打开暗色窗口
// 机器相关配置集中在顶部 CONFIG，换机器改这里。
const { app, BrowserWindow, shell } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
// https 只用于查新版本（api.github.com）。引擎那边一律走 http 到 127.0.0.1。
const https = require('https');
const path = require('path');

// 引擎有两种形态：
//   打包后 —— 与本文件同级 resources/ 下的 lostpath-engine.exe，目标机器不需要 Python；
//   开发时 —— 优先用 conda 环境直接跑源码，保证改完后启动脚本能立即看到最新代码；
//             只有打包应用才使用冻结 exe。
// 刻意不读环境变量来决定 spawn 什么 —— 让外部值决定被执行的程序会引入程序选择/选项注入。
const CONFIG = {
  // 只有"源码开发模式"才会用到 conda（见 resolveEngine 的第三级退路）。默认取 PATH
  // 里的 conda，装在别处就用 LOSTPATH_CONDA_EXE 指过去——**不写死任何人的安装路径**，
  // 那是只对一台机器成立的配置。
  condaExe: process.env.LOSTPATH_CONDA_EXE || 'conda',
  condaEnv: process.env.LOSTPATH_CONDA_ENV || 'lostpath',
  projectRoot: path.resolve(__dirname, '..'),
  url: 'http://127.0.0.1:8321/',
  bg: '#0d1117',
};

/**
 * 原生窗口控件（最小化/最大化/关闭）的配色，两个主题各一份。
 *
 * 我们把标题栏藏了（`titleBarStyle: 'hidden'`）、只留这三个控件叠在界面右上角，
 * 所以它们的底色必须跟界面同步——否则浅色主题下右上角会留一块深色。
 * 值与 `ui/src/index.css` 的 `--bg` / `--tx2` 对齐。
 */
const TITLEBAR = {
  dark: { color: '#0d1117', symbolColor: '#8b949e', height: 40 },
  light: { color: '#f6f8fa', symbolColor: '#57606a', height: 40 },
};

const fs = require('fs');

/**
 * 壳层日志。写到用户数据目录下的 `logs/desktop.log`。
 *
 * **加它的理由**：桌面壳出问题时没有任何可查的东西。stdout/stderr 在 GUI 子系统里
 * 被吞掉，排查"提权重启后一直显示标准权限"这类问题只能靠反复插临时探针——那既慢
 * 又留不下证据。引擎侧早就有 logs/，壳层没有，属于漏了。
 *
 * 只记关键节点（启动、引擎来源、提权流程各步），不记高频事件，避免日志本身成为负担。
 */
const LOG_FILE = path.join(
  process.env.LOCALAPPDATA || path.join(app.getPath('home'), 'AppData', 'Local'),
  'LostPath', 'logs', 'desktop.log',
);
function log(...parts) {
  const line = `${new Date().toISOString()} ${parts.join(' ')}\n`;
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    // 超过 256 KiB 就重开，日志不该无限长
    try {
      if (fs.statSync(LOG_FILE).size > 262144) fs.writeFileSync(LOG_FILE, '');
    } catch { /* 文件还不存在 */ }
    fs.appendFileSync(LOG_FILE, line);
  } catch { /* 日志写不进去不该影响程序运行 */ }
}

// ── 检查新版本 ────────────────────────────────────────────────────────────
//
// 只做"发现并告知"，不下载、不安装。用户点了就跳浏览器到 Release 页，自己下。
//
// 为什么放在壳层而不是引擎里：**引擎目前零对外网络请求**，是纯本地服务。
// 那条性质值得保住——一个读注册表、扫全盘的东西不出网，用户才好放心。
// 壳层本来就要 shell.openExternal，多一个 https 请求不改变它的性质。
//
// 为什么不用 electron-updater：那是为"自动下载+安装"设计的，而自动安装在本项目
// 有个硬障碍——引擎子进程锁着 resources/lostpath-engine.exe，Windows 不允许覆盖
// 正在运行的可执行文件镜像，装到一半会失败。绕开它要先杀引擎再等它死透
// （taskkill 异步，200~500ms），跟提权交接那个坑同一类。等真要做自动安装时
// 再单独处理，别混在这里。

/** 发布页地址。**写成常量**，理由见 maybeNotifyUpdate 里的说明。 */
const RELEASES_URL = 'https://github.com/xiaopenghuang/LostPath/releases/latest';
const UPDATE_API = 'https://api.github.com/repos/xiaopenghuang/LostPath/releases/latest';
const UPDATE_TIMEOUT_MS = 10000;

/**
 * 比较两个语义版本。返回 >0 表示 a 比 b 新，0 相等，<0 更旧，无法解析返回 NaN。
 *
 * **不能按字符串比大小**：那样 `"0.10.0" > "0.9.0"` 为 false（逐字符 '1' < '9'），
 * 于是 0.10.0 会被判成比 0.9.0 旧，用户被反复推送旧版。
 *
 * 解析失败返回 NaN 是刻意的：NaN 参与任何比较都得 false，所以调用方那句
 * `compareVersions(...) > 0` 天然把"字段缺失/垃圾输入"判成没有更新——
 * 宁可漏报，不可误报。
 */
function compareVersions(a, b) {
  const parse = (v) => {
    if (typeof v !== 'string') return null;
    // 剥掉 tag 的 v 前缀（GitHub 上是 v0.1.0，package.json 里是 0.1.0）；
    // 只取三段主版本，-beta.1 这类后缀忽略
    const m = /^v?(\d+)\.(\d+)\.(\d+)/.exec(v.trim());
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
  };
  const pa = parse(a);
  const pb = parse(b);
  if (!pa || !pb) return NaN;
  for (let i = 0; i < 3; i++) {
    if (pa[i] !== pb[i]) return pa[i] - pb[i];
  }
  return 0;
}

/**
 * 拉一次 GitHub 的 latest release。**任何失败都返回 null，不抛、不重试。**
 *
 * 失败是常态而不是异常：断网、公司代理、被墙、匿名频率限制（60 次/小时/IP）
 * 都会走到这里。检查更新失败不该让用户看见任何东西——他打开这个软件是为了
 * 清 C 盘，不是为了知道我们连不上 GitHub。
 */
function fetchLatestRelease() {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };

    let req;
    try {
      req = https.get(UPDATE_API, {
        // GitHub API 不带 User-Agent 直接 403。写明是谁在请求。
        headers: {
          'User-Agent': `LostPath/${app.getVersion()}`,
          'Accept': 'application/vnd.github+json',
        },
        timeout: UPDATE_TIMEOUT_MS,
      }, (res) => {
        if (res.statusCode !== 200) {
          // 403 多半是频率限制，404 是仓库还没有 release。都不值得打扰用户。
          log('update', 'HTTP ' + res.statusCode);
          res.resume();   // 必须消费掉，否则 socket 不释放
          return finish(null);
        }
        // 限制读入量：正常响应几 KB，设上限免得异常响应把内存吃了
        let body = '';
        let tooBig = false;
        res.setEncoding('utf8');
        res.on('data', (c) => {
          if (tooBig) return;
          body += c;
          if (body.length > 512 * 1024) { tooBig = true; req.destroy(); }
        });
        res.on('end', () => {
          if (tooBig) return finish(null);
          try {
            finish(JSON.parse(body));
          } catch {
            finish(null);
          }
        });
        res.on('error', () => finish(null));
      });
    } catch {
      return finish(null);
    }
    req.on('timeout', () => { req.destroy(); finish(null); });
    req.on('error', (e) => { log('update', '请求失败 ' + e.message); finish(null); });
  });
}

/**
 * 查一次新版本，有就弹个原生对话框。没有、或查不了，就什么都不做。
 *
 * **URL 用常量而不是响应里的 `html_url`**：那个字段来自网络，把它直接交给
 * shell.openExternal 等于让远端决定我们打开什么——DNS 被劫持或响应被篡改时
 * 可以变成任意 URL（含 file:// 之类）。发布页地址本来就是固定的，没有任何
 * 理由从网络取。tag 只用于显示，且截断长度。
 */
async function maybeNotifyUpdate() {
  const current = app.getVersion();
  const rel = await fetchLatestRelease();
  if (!rel) return;
  // 草稿和预发布不推给普通用户
  if (rel.draft || rel.prerelease) return;

  const tag = typeof rel.tag_name === 'string' ? rel.tag_name : '';
  if (!(compareVersions(tag, current) > 0)) {
    log('update', `无需更新（远端 ${tag || '?'} / 本地 ${current}）`);
    return;
  }
  log('update', `发现新版 ${tag}，当前 ${current}`);

  if (!win || win.isDestroyed()) return;
  const { dialog } = require('electron');
  // 截断：tag 来自网络，不该让它决定对话框有多大
  const shown = tag.slice(0, 32);
  const r = await dialog.showMessageBox(win, {
    type: 'info',
    title: 'LostPath',
    message: `发现新版本 ${shown}`,
    detail: `当前版本 ${current}。\n\n`
          + '点「去下载」会在浏览器里打开发布页，下载后手动安装即可；'
          + '安装程序会覆盖旧版本，不影响已有的扫描快照与配置。',
    buttons: ['去下载', '以后再说'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  });
  if (r.response === 0) shell.openExternal(RELEASES_URL);
}

/** 窗口图标。打包后在 resources/，开发时在仓库 ico/。都没有就返回 undefined。 */
function iconPath() {
  for (const p of [
    path.join(process.resourcesPath || __dirname, 'LostPath.ico'),
    path.join(CONFIG.projectRoot, 'ico', 'LostPath.ico'),
  ]) {
    if (fs.existsSync(p)) return p;
  }
  return undefined;
}

/** 找引擎。开发壳优先跑源码，打包壳只使用随安装包带来的冻结 exe。 */
function resolveEngine() {
  const packaged = path.join(process.resourcesPath || __dirname, 'lostpath-engine.exe');
  const devExe = path.join(CONFIG.projectRoot, 'dist', 'lostpath-engine.exe');
  if (app.isPackaged && fs.existsSync(packaged)) {
    return [packaged, [], path.dirname(packaged)];
  }

  if (!app.isPackaged) {
    // 开发时 dist/lostpath-engine.exe 是旧快照，优先它会让源码改动静默不生效。
    // 直接跑源码还会读取刚构建的 ui/dist，适合「启动 LostPath.bat」的开发流程。
    return [
      CONFIG.condaExe,
      ['run', '--no-capture-output', '-n', CONFIG.condaEnv, 'python', 'engine/main.py'],
      CONFIG.projectRoot,
    ];
  }

  // 仅保留给未找到打包资源的开发壳或损坏的安装包，方便排障。
  if (fs.existsSync(devExe)) return [devExe, [], CONFIG.projectRoot];
  return [
    CONFIG.condaExe,
    ['run', '--no-capture-output', '-n', CONFIG.condaEnv, 'python', 'engine/main.py'],
    CONFIG.projectRoot,
  ];
}

// 用户数据根：必须与 Python 侧 lostpath/storage/paths.py 指向同一处。
// Electron 默认把 userData 放 %APPDATA%（Roaming），而快照描述的是本机 C 盘事实、
// 不该跟着域账户漫游，所以显式改到 LOCALAPPDATA。不设的话壳层与引擎会分家两地。
const dataRoot = path.join(
  process.env.LOCALAPPDATA || path.join(app.getPath('home'), 'AppData', 'Local'),
  'LostPath',
);
app.setPath('userData', dataRoot);

let engine = null;
let win = null;
/**
 * 这个引擎是不是我起的。
 *
 * 复用已有引擎时为 false（见 probeExisting），退出时**不能去杀它**——它可能属于
 * 另一个正常运行的实例。谁 spawn 的谁负责收。
 */
let ownsEngine = false;

function startEngine() {
  const [exe, args, cwd] = resolveEngine();
  // windowsHide：引擎是 GUI 子系统程序（runw.exe），正常不弹窗；但开发时回退到
  // conda 跑源码那条路走的是控制台程序，不加这个会闪一个黑框。
  // Windows 的 conda 通常是 conda.bat，Node 不带 shell 时会直接报 ENOENT；参数是
  // 本文件固定生成的，开启 shell 只用于这条开发启动路径，不改变打包 exe 的执行方式。
  const condaSource = args[0] === 'run' && args.includes('engine/main.py');
  return spawn(exe, args, {
    cwd, stdio: 'ignore', windowsHide: true,
    shell: process.platform === 'win32' && condaSource,
    // conda.bat 会在 Electron 与 Python 之间插入 cmd/conda 包装层。外层终端被
    // 强制关闭时，包装层可能先死而 Python 变成孤儿。引擎观察真正的桌面壳 PID，
    // 壳没了就自行退出；直接运行引擎时没有这个变量，不受影响。
    env: { ...process.env, LOSTPATH_PARENT_PID: String(process.pid) },
  });
}

/**
 * 已经有引擎在 8321 上服务吗？
 *
 * **不问这一句的代价**：端口被占时 `startEngine()` 起的新引擎会因
 * `[Errno 10048] 端口只能使用一次` 立刻死掉，而 `waitReady` 傻等 60 秒——这段时间
 * 屏幕上**一个窗口都没有**，用户以为"双击图标没反应"。实际撞到过：提权实例被强杀后
 * 它的引擎变成孤儿（提权进程，非提权的 shell 连杀都杀不掉），占着端口不放，
 * 之后每次启动都卡这 60 秒。
 *
 * 已有引擎就直接用，**不去杀它**：它可能是另一个正常运行的实例（比如提权那个），
 * 而引擎本身是只读服务、多个界面连同一个没有问题。谁 spawn 的谁负责收，
 * 所以复用时要记下"引擎不是我起的"（`ownsEngine`），退出时别去杀别人的进程。
 */
function probeExisting(timeoutMs = 1200) {
  return new Promise((resolve) => {
    const req = http.get(`${CONFIG.url}api/data`, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve(false);
    });
  });
}

/**
 * 确保有一个能用的引擎，**可反复调用**。
 *
 * `probeExisting()` 只探一次就不再复查，这在提权切换时会咬人，而且咬得很难看：
 *
 *   旧实例 UAC 通过 → killEngine()（taskkill 是异步的，要 200~500ms）→ 900ms 后退出
 *   提权实例启动 → 探到旧引擎**还没死透** → 判定"复用"，自己不起引擎
 *                → 旧引擎随后被杀
 *                → 提权实例手里一个引擎都没有，且永远不会再去起
 *                → 页面加载失败，兜底逻辑把空窗口显示出来 = **黑屏，只有个窗口框**
 *
 * 所以判据不能是"启动那一刻探到了什么"，而要是"现在能不能用"。这个函数在启动时
 * 调一次，页面加载失败时再调一次（见 did-fail-load）——引擎无论因为什么原因不在了
 * （被杀、崩了、端口交接没接上），都能自己接上。
 *
 * `reuseOnly` 为 true 时只复用不新起：给"探到别人的引擎"那条路用，避免我们抢着
 * 起第二个去撞端口。
 */
async function ensureEngine(readyTimeoutMs = 20000) {
  if (await probeExisting()) {
    // 别人的引擎（或我们自己之前起的、仍活着的那个）。不接管所有权：
    // 不是我 spawn 的就不能在退出时杀它。
    if (!engine) ownsEngine = false;
    return true;
  }
  // 我们自己之前起过一个但现在探不到 —— 它死了，句柄没用了，重起一个。
  engine = startEngine();
  ownsEngine = true;
  try {
    await waitReady(readyTimeoutMs);
    return true;
  } catch {
    return false;
  }
}

/**
 * 引擎起不来时显示的错误页。**内联在这里，不从引擎取。**
 *
 * 这一页存在的唯一理由：**界面本身是引擎提供的**（引擎用 StaticFiles 把 ui/dist
 * 挂在 /）。所以引擎不在时，连"无法连接本地服务"那个提示都渲染不出来——用户看到的
 * 是一个纯黑的空窗口，只有个框。实际发生过：提权切换时引擎在交接中被杀，
 * 用户报"提权重启怎么黑屏了，没东西显示了，只有一个软件框"。
 *
 * 用 data: URL 而不是打包一个 html 文件：这一页不能有任何外部依赖，否则它自己
 * 也可能加载失败，那就还是黑屏。文案给出可执行的下一步，不只说"出错了"。
 */
function showFallback() {
  if (!win || win.isDestroyed()) return Promise.resolve();
  const html = `<!doctype html>
<!-- 标题刻意与正常界面不同：用户从任务栏就能看出这不是正常状态，
     而不是打开窗口才发现。也让自动化验证能区分两者。 -->
<html lang="zh-CN"><head><meta charset="utf-8"><title>LostPath — 服务未启动</title>
<style>
  :root { color-scheme: dark }
  body { margin:0; height:100vh; display:grid; place-items:center;
         background:#0d1117; color:#e6edf3;
         font:14px/1.75 "Segoe UI","Microsoft YaHei UI",system-ui,sans-serif }
  .box { max-width:560px; padding:28px 32px; border:1px solid #21262d;
         border-radius:12px; background:#161b22 }
  h1 { margin:0 0 12px; font-size:17px; font-weight:600 }
  p  { margin:0 0 10px; color:#8b949e }
  ol { margin:0; padding-left:20px; color:#8b949e }
  li { margin-bottom:6px }
  code { font-family:"Cascadia Code",Consolas,monospace; font-size:12.5px; color:#7adcf0 }
</style></head><body><div class="box">
  <h1>本地服务未能启动</h1>
  <p>界面需要 LostPath 本地服务（端口 8321）提供数据，当前无法连接。</p>
  <ol>
    <li>检查 8321 端口是否被其他程序占用，或存在残留的
        <code>lostpath-engine.exe</code> 进程</li>
    <li>若刚执行过以管理员权限重启，残留进程需在管理员权限下才能结束</li>
    <li>完全退出本程序后重新启动</li>
  </ol>
</div></body></html>`;
  return win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
}

function waitReady(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const poll = () => {
      const req = http.get(`${CONFIG.url}api/data`, (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve();
        retry();
      });
      req.on('error', retry);
      function retry() {
        if (Date.now() > deadline) {
          return reject(new Error(
            '引擎启动超时（常见原因：8321 端口已被别的实例占着，'
            + '或引擎进程启动即退出）',
          ));
        }
        setTimeout(poll, 600);
      }
    };
    poll();
  });
}

function killEngine() {
  // 不是我起的就不动它。`engine` 为 null 时本来也会 return，但把判据写出来
  // 更清楚：这是一条**故意不做的事**，不是恰好没做。
  if (!ownsEngine || !engine) return;
  // 先摘所有权，避免 before-quit / window-all-closed / SIGINT 连续到达时重复
  // taskkill 同一个进程树。保存局部句柄供下面完成本次清理。
  const ownedEngine = engine;
  ownsEngine = false;
  engine = null;
  try {
    if (process.platform === 'win32') {
      // /T 连子进程（conda run 下的 python）一起杀
      spawn('taskkill', ['/pid', String(ownedEngine.pid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      ownedEngine.kill();
    }
  } catch {
    /* 忽略 */
  }
}

// 从启动批处理按 Ctrl+C、终端被关闭，或外层任务发送终止信号时，Electron 不一定
// 走 window-all-closed。没有这两条，开发壳退了而 conda 下的 Python 仍占着 8321，
// 下次启动会复用那个旧引擎，表现成“代码改了但界面没变化”。
let signalShutdown = false;
function shutdownFromSignal() {
  if (signalShutdown) return;
  signalShutdown = true;
  killEngine();
  app.quit();
  // taskkill 是异步进程。留一个短兜底窗口，让它有机会结束整个 conda 子进程树。
  setTimeout(() => app.exit(0), 700);
}
process.on('SIGINT', shutdownFromSignal);
process.on('SIGTERM', shutdownFromSignal);

/**
 * 抢单实例锁，**拿不到时重试几次再放弃**。
 *
 * 原先是"拿不到就立刻 app.quit()"，于是**关掉窗口后马上再开就点不动** —— 上一个
 * 实例的 Chromium 锁还没释放完，双击图标什么都不发生。用户看到的是"点了没反应"，
 * 分不清是启动失败还是已经开着。实测撞到过。
 *
 * （这个重试当初也是为提权重启加的兜底，那条路已撤掉；但"关掉再开"这个场景本身
 * 就需要它，所以留着。）
 *
 * 重试总共约 1.5 秒（6 × 250ms），比"上一个实例正在退出"这个窗口期长；真有另一个
 * 实例在正常运行时，第一次 requestSingleInstanceLock 就会触发它的 second-instance
 * 事件把它的窗口带到前台，那才是该走的路径 —— 所以重试不会让"聚焦已有窗口"变慢，
 * 它只在锁处于"正在释放"这个中间态时起作用。
 */
function acquireLock(tries = 6) {
  if (app.requestSingleInstanceLock()) return Promise.resolve(true);
  if (tries <= 0) return Promise.resolve(false);
  return new Promise((resolve) => {
    setTimeout(() => resolve(acquireLock(tries - 1)), 250);
  });
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  // 同步这一次没拿到：可能是真有实例在跑（上面那次调用已经通知它了），也可能是
  // 锁正在释放。异步重试一轮，成了就继续启动，仍不成才退。
  acquireLock().then((ok) => {
    if (!ok) app.quit();
    else start();
  });
} else {
  start();
}

function start() {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    // 记下这个实例究竟以什么权限在跑。排查"提权重启后仍是标准权限"必需。
    log('--- instance start ---',
        'packaged=' + app.isPackaged,
        'execPath=' + process.execPath,
        'argv=' + JSON.stringify(process.argv.slice(1)));
    // 已有引擎就复用，否则起一个。**失败也继续开窗**：界面自己会显示"无法连接
    // 本地服务"，那比让用户对着空屏幕等着有用；而且下面 did-fail-load 那条会
    // 再试一次 ensureEngine 并重载，多数情况能自愈。
    //
    // 超时 20s 而非 60s：60 秒的空白屏跟"点了没反应"没有区别。引擎正常启动
    // 约 10~13 秒（PyInstaller onefile 要先解压到临时目录）。
    const engineOk = await ensureEngine();
    log('ensureEngine', 'ok=' + engineOk, 'ownsEngine=' + ownsEngine,
        'enginePid=' + (engine ? engine.pid : 'none'));
    win = new BrowserWindow({
      width: 1520,
      height: 960,
      minWidth: 1080,
      minHeight: 700,
      backgroundColor: CONFIG.bg,
      autoHideMenuBar: true,
      title: 'LostPath',
      // 打包后图标由 electron-builder 嵌进 exe，这里显式给一份是为了开发时窗口
      // 也不用默认的 Electron 图标。找不到就交给系统默认，不因为图标缺失就崩。
      icon: iconPath(),
      show: false,
      // 藏掉原生标题栏，只保留右上角三个窗口控件叠在界面上。
      //
      // 为什么：原生标题栏会显示一次图标+「LostPath」，而界面侧栏顶部又有一次
      // 图标+「LostPath」——同一块区域上下两行重复的品牌标识。藏掉标题栏之后，
      // 界面自己的顶栏就是标题栏（拖动区由 CSS 的 -webkit-app-region: drag 指定），
      // 侧栏那份成为唯一的品牌标识。
      //
      // 用 hidden + titleBarOverlay 而不是 frame: false：后者要自己实现最小化/
      // 最大化/关闭三个按钮与它们的悬停反馈、双击标题栏最大化、贴边分屏等一堆
      // 系统行为，自造一份只会更差。
      titleBarStyle: 'hidden',
      titleBarOverlay: TITLEBAR.dark,
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        preload: path.join(__dirname, 'preload.js'),
      },
    });

    // 切主题时同步窗口控件配色。渲染进程只能送 'dark' / 'light' 两个字面量
    // （preload 已挡），这里再校验一次并且只认自己那个窗口发来的消息。
    const { dialog, ipcMain } = require('electron');

    /**
     * **不提供"点一下自动提权"。** 这是撤掉一次实现之后的决定，理由记在这里，
     * 免得下次有人再试一遍。
     *
     * 曾经实现过：`Start-Process -Verb RunAs` 重启自己。功能本身通（UAC 能弹、
     * 提权实例能起来），但它要求在两个进程之间交接三样东西 —— 8321 端口、
     * Chromium 单实例锁、引擎进程 —— 而交接窗口只有一两秒，改了四轮每轮都冒出
     * 新的时序问题：
     *
     *   · 提权实例复用了尚未死透的**标准权限**引擎 → 界面报"标准权限"
     *   · 旧实例先释放锁又被提权实例抢先 → 两个实例都退出，屏幕上什么都不剩
     *   · 提权实例复用引擎之后旧引擎才死 → 它手里没有引擎 → **黑屏，只有窗口框**
     *   · 提权实例被强杀后留下**提权的**残留进程，占着锁与端口，而标准权限的
     *     终端连杀都杀不掉 —— 用户此后每次启动都被挡在抢锁阶段
     *
     * 最后一条尤其糟：它把一次失败变成了持续故障，只能让用户开管理员终端清场。
     *
     * 而右键「以管理员身份运行」没有任何交接问题：旧实例是用户自己关的，端口、锁、
     * 引擎全部干净释放，之后启动的实例是唯一实例。多两步操作换掉一整类时序缺陷，
     * 这笔交易是划算的。所以界面只做两件事：**识别**当前权限、**引导**怎么提权。
     */

    ipcMain.on('lp:titlebar-theme', (event, name) => {
      if (!win || win.isDestroyed()) return;
      if (event.sender !== win.webContents) return;
      const conf = TITLEBAR[name];
      if (!conf) return;
      try {
        win.setTitleBarOverlay(conf);
        win.setBackgroundColor(conf.color);
      } catch {
        // 某些 Windows 版本上 setTitleBarOverlay 可能不可用，配色不同步
        // 不值得让窗口崩掉
      }
    });

    ipcMain.handle('lp:pick-executable', async (event) => {
      if (!win || win.isDestroyed() || event.sender !== win.webContents) return null;
      const result = await dialog.showOpenDialog(win, {
        title: '选择右键菜单要启动的程序',
        properties: ['openFile'],
        filters: [
          { name: 'Windows 程序', extensions: ['exe'] },
          { name: '所有文件', extensions: ['*'] },
        ],
      });
      return result.canceled ? null : (result.filePaths[0] || null);
    });

    /**
     * 显示窗口。**不能只吊在 `ready-to-show` 上。**
     *
     * 原先就一行 `win.once('ready-to-show', () => win.show())`，配 `show: false`。
     * 那个事件要等首帧渲染完，而首帧依赖 `loadURL` 拿到页面——引擎没起来时页面
     * 加载失败，事件不触发，于是**窗口一直存在但永远不可见**。表现就是"双击图标
     * 什么都没发生"，而任务管理器里进程好好地在跑（实际撞到过，排查时才发现
     * 窗口对象确实在、`isVisible()` 是 false）。
     *
     * 现在三条路任一到达就显示，`showOnce` 保证只显示一次：
     *   ready-to-show   正常路径，有内容了才显示，不闪白
     *   did-fail-load   加载失败也要显示——界面自己会渲染连接错误提示
     *   2.5s 兜底       上面两个都没来也得让用户看见窗口，宁可先白一下
     */
    let shown = false;
    const showOnce = () => {
      if (shown || !win || win.isDestroyed()) return;
      shown = true;
      win.show();
      // 窗口可见之后再查新版本。**挂在这里而不是 whenReady 开头**：对话框要有父窗口
      // 才会正确居中并模态化，而且不该跟引擎启动、首屏加载抢那几秒。
      // 延迟 5 秒是为了让用户先看到界面 —— 一打开就弹窗像流氓软件。
      // 整段包在 catch 里：查更新失败绝不能影响主流程。
      setTimeout(() => {
        maybeNotifyUpdate().catch((e) => log('update', '检查失败 ' + e.message));
      }, 5000);
    };
    win.once('ready-to-show', showOnce);
    setTimeout(showOnce, 2500);

    /**
     * 页面加载失败时的处理。
     *
     * 起因：用户报"提权重启怎么黑屏了，没东西显示了，只有一个软件框"。**界面本身是
     * 引擎提供的**（引擎把 ui/dist 挂在 /），引擎不在时连"无法连接本地服务"那个提示
     * 都渲染不出来，加上 2.5 秒兜底把空窗口显示出来，就是一个纯黑的框。空窗口比
     * 不显示更糟：不显示还能理解成"在启动"，空窗口看着像程序坏了。
     *
     * 顺序是**先给用户看东西，再后台自愈**，不是反过来。
     *
     * 反过来试过，实测很糟：`did-fail-load` 里先 `await ensureEngine()`（等引擎最长
     * 20 秒），期间窗口还是空的——而启动路径上已经等过一次 20 秒了，加起来用户对着
     * 空窗口坐 40 秒。那和黑屏没区别，只是黑得更久。
     *
     * 现在：立刻显示错误页（用户马上知道发生了什么、下一步做什么），然后后台试着
     * 把引擎拉起来；成功了再切回真界面。自愈成功是锦上添花，不是显示内容的前提。
     */
    let healing = false;
    win.webContents.on('did-fail-load', async (_e, code) => {
      // -3 是 ERR_ABORTED，通常是我们自己 reload 打断了上一次加载，不是故障
      if (code === -3) return;
      if (healing || !win || win.isDestroyed()) return;
      healing = true;

      // ① 先让窗口有内容，别让用户对着黑框等
      await showFallback();
      showOnce();

      // ② 再后台自愈。超时给 8 秒而非 20：这条路是"本该有引擎却没有"（交接没接上、
      //    引擎崩了），那种情况引擎起得快；20 秒是留给冷启动的，不该在这里再等一遍。
      if (!(await ensureEngine(8000))) {
        healing = false;
        return;
      }
      if (!win || win.isDestroyed()) return;
      try {
        await win.loadURL(CONFIG.url);
      } catch {
        /* 仍不行就留在错误页上，它已经说清了怎么办 */
      }
      healing = false;
    });
    // 外部链接走系统浏览器，应用内不跳出去
    win.webContents.setWindowOpenHandler(({ url }) => {
      shell.openExternal(url);
      return { action: 'deny' };
    });
    // loadURL 失败会 reject。**必须接住**：裸 await 抛出去会让整个 whenReady 链
    // 静默失败，窗口停在初始空白状态。接住之后交给 did-fail-load 那条去自愈
    // （它对同一次失败也会触发），接不上则显示自带的错误页。
    try {
      await win.loadURL(CONFIG.url);
    } catch {
      if (!healing) {
        await showFallback();
        showOnce();
      }
    }
  });

  app.on('window-all-closed', () => {
    killEngine();
    app.quit();
  });
}
