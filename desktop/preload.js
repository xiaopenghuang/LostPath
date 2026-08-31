// 渲染进程与主进程之间的窄桥。
//
// 只暴露两件事，且都不接受任意值：
//   isDesktop —— 界面据此决定是否给窗口控件留位（浏览器里跑 dev server 时没有它们）；
//   setTitleBarTheme('dark'|'light') —— 切主题时让原生窗口控件跟着变色。
//
// 刻意不暴露 ipcRenderer 本身，也不做通用 invoke 转发：那等于把主进程的全部通道
// 交给页面，一旦界面侧被注入内容就能越权。参数在主进程侧还会再校验一次。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('lostpath', {
  isDesktop: true,
  setTitleBarTheme: (name) => {
    if (name !== 'dark' && name !== 'light') return;
    ipcRenderer.send('lp:titlebar-theme', name);
  },
});

// 曾经这里还有一个 relaunchElevated（以管理员身份重启自己）。**已撤掉**，
// 理由记在 main.js 里那段"不提供点一下自动提权"的注释：功能本身通，但两个进程
// 之间交接端口/锁/引擎的时序问题改了四轮都没穷尽，而右键「以管理员身份运行」
// 完全没有这类问题。界面改为只识别权限并给出引导。
