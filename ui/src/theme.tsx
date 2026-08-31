import { App as AntdApp, ConfigProvider, theme as antdTheme } from 'antd';
import { useEffect, useState } from 'react';

export type ThemeName = 'dark' | 'light';

/**
 * Electron 壳暴露的窄桥（见 `desktop/preload.js`）。
 * 浏览器里跑 dev server 时它是 undefined —— 所有用处都要判空，别假设自己在桌面端。
 */
declare global {
  interface Window {
    lostpath?: {
      isDesktop: boolean;
      setTitleBarTheme: (name: ThemeName) => void;
    };
  }
}

/** 是否跑在 Electron 壳里。界面据此决定要不要给原生窗口控件留位。 */
export const isDesktop = () => !!window.lostpath?.isDesktop;


export function useTheme() {
  const [theme, setTheme] = useState<ThemeName>(() => {
    const saved = localStorage.getItem('lp-theme');
    return saved === 'light' ? 'light' : 'dark';
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('lp-theme', theme);
    // 桌面端还要把原生窗口控件的配色一起换掉，否则浅色主题下右上角
    // 最小化/关闭那一块仍是深色。浏览器里 lostpath 不存在，跳过。
    window.lostpath?.setTitleBarTheme(theme);
  }, [theme]);
  return {
    theme,
    toggle: () => setTheme((t) => (t === 'dark' ? 'light' : 'dark')),
  };
}

const shared = {
  borderRadius: 8,
  fontFamily:
    "'Segoe UI', 'Microsoft YaHei UI', 'PingFang SC', system-ui, sans-serif",
};

const DARK_TOKENS = {
  ...shared,
  algorithm: antdTheme.darkAlgorithm,
  token: {
    colorPrimary: '#2f81f7',
    colorInfo: '#4493f8',
    colorBgBase: '#0d1117',
    colorBgContainer: '#161b22',
    colorBgElevated: '#1c2128',
    colorBorder: 'rgba(240,246,252,0.12)',
    colorBorderSecondary: 'rgba(240,246,252,0.08)',
  },
  components: {
    Card: { colorBgContainer: '#161b22' },
    Table: {
      colorBgContainer: 'transparent',
      headerBg: 'rgba(240,246,252,0.04)',
      // rowHoverBg 必须显式给，**不能让 antd 从 colorBgContainer 推导**。见
      // LIGHT_TOKENS 同处那段长注释：`transparent` 会让推导结果变成纯黑。
      // 深色下纯黑不显眼（底色本就近黑），所以这个 bug 一直只在浅色下暴露。
      rowHoverBg: '#21262d',
      bodySortBg: '#1c2128',
      footerBg: '#1c2128',
    },
    Tree: { colorBgContainer: 'transparent' },
  },
};

const LIGHT_TOKENS = {
  ...shared,
  algorithm: antdTheme.defaultAlgorithm,
  token: {
    colorPrimary: '#0969da',
    colorInfo: '#218bff',
    colorBgBase: '#f6f8fa',
    colorBgContainer: '#ffffff',
    colorBgElevated: '#ffffff',
    colorBorder: 'rgba(31,35,40,0.15)',
    colorBorderSecondary: 'rgba(31,35,40,0.1)',
  },
  components: {
    Card: { colorBgContainer: '#ffffff' },
    Table: {
      colorBgContainer: 'transparent',
      headerBg: '#f6f8fa',
      /*
       * `rowHoverBg` 与 `bodySortBg` 必须显式给。
       *
       * **不给的话浅色主题下表格行一悬停就整行变纯黑，字全看不见。** antd 5 的
       * 算法（`table/style/index.js:194`）是：
       *
       *     colorFillAlterSolid = new FastColor(colorFillAlter)
       *         .onBackground(colorBgContainer).toHexString()
       *
       * `colorFillAlter` 是个半透明灰，把它合成到我们设的 `transparent`
       * （= `rgba(0,0,0,0)`）上，再 `toHexString()` **丢掉 alpha** —— 结果就是
       * `#000000`。`rowHoverBg` / `headerBg` / `bodySortBg` 三个都由它派生，
       * headerBg 我们本来就显式给了，所以只有悬停和排序底色中招。
       *
       * `colorBgContainer: 'transparent'` 本身是刻意的（表格要透出卡片底色，
       * 否则详情页里一层白叠一层白），所以不能靠改它来修，只能把派生值钉死。
       *
       * 取值与 CSS 变量同口径：浅色 `--panel2`，深色 `--line`。文字对比度实测
       * 13.55（浅）/ 12.88（深），与卡片底也有可辨差异（1.17 / 1.14）。
       */
      rowHoverBg: '#eaeef2',
      bodySortBg: '#f0f3f6',
      // footerBg 同样吃那个派生（`table/style/index.js:217`）。上一轮只钉了
      // rowHoverBg / bodySortBg，漏了它，于是有 footer 的表格下方还是一条纯黑。
      footerBg: '#f6f8fa',
    },
    Tree: { colorBgContainer: 'transparent' },
  },
};

/**
 * 主题容器。
 *
 * **`AntdApp` 那一层不是可选的装饰。** antd 5 的静态方法（`message.success`、
 * `Modal.confirm` 等，本项目用了 23 处）走的是独立于 React 树的渲染路径，
 * 拿不到 `ConfigProvider` 的 context——antd 自己在 config-provider 里就 warn
 * 过这件事：`Static function can not consume context like dynamic theme.
 * Please use 'App' component instead.`
 *
 * 后果是浅色主题下所有 toast 与确认弹窗仍按深色算法渲染。而回收站那个
 * 「永久删除，无法恢复」正是 `Modal.confirm`——全工具唯一不可撤销的操作，
 * 偏偏是弹窗样式最容易出错的那类。
 *
 * `AntdApp` 在树内挂一组 context 版实例（message / notification / modal）与它们的
 * holder。**但只包一层是不够的**：从 'antd' 直接 import 的 `message` 走的是
 * `globalConfig().getTheme()`，那是个全局值，跟 React 树无关，包多少层都读不到。
 * 真正生效要求调用方改用 `App.useApp()` 取实例——所以四个用到弹窗的文件
 * （SoftwarePage / MigrationPage / RecyclePage / useScan）都改了取值来源，
 * 调用写法不变（`message.success(...)` 仍是 `message.success(...)`，只是
 * `message` 来自 hook；`Modal.confirm` 变成 `modal.confirm`）。
 */
export function ThemeProvider({ theme, children }: { theme: ThemeName; children: React.ReactNode }) {
  return (
    <ConfigProvider theme={theme === 'dark' ? DARK_TOKENS : LIGHT_TOKENS}>
      <AntdApp
        // 铺满，否则 AntdApp 的 div 会截断 100vh 布局
        style={{ height: '100%' }}
        // message 默认堆在顶部中央，那里正是面包屑那一行；下移让它不压住标题
        message={{ top: 64 }}
      >
        {children}
      </AntdApp>
    </ConfigProvider>
  );
}
