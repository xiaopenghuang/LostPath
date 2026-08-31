import { useEffect, useState } from 'react';
import { Alert, App as AntdApp, Card, Skeleton, Tooltip } from 'antd';
import {
  AppstoreOutlined, DashboardOutlined, DeleteOutlined, DesktopOutlined,
  FolderOpenOutlined, MoonOutlined, RadarChartOutlined, SafetyCertificateOutlined,
  SettingOutlined, SunOutlined, SwapOutlined,
} from '@ant-design/icons';
import {
  fetchDrives, fetchData, fetchRecycle, fetchSettings, fmtSize,
  DriveInfo, LpData,
} from './api';
import { isDesktop, ThemeProvider, useTheme } from './theme';
import DashboardPage from './DashboardPage';
import SoftwarePage from './SoftwarePage';
import DiskTreePage from './DiskTreePage';
import MigrationPage from './MigrationPage';
import RecyclePage from './RecyclePage';
import SettingsPage from './SettingsPage';

// 图谱没有独立页：它是"某个软件的关联结构"，脱离具体软件看没有意义，
// 所以只作为软件台账详情页右栏的一块，不占侧栏入口。
type View = 'dashboard' | 'software' | 'disk' | 'migration' | 'recycle' | 'settings';

const NAV: { key: View; label: string; icon: JSX.Element }[] = [
  { key: 'dashboard', label: '仪表盘', icon: <DashboardOutlined /> },
  { key: 'software', label: '软件台账', icon: <AppstoreOutlined /> },
  { key: 'disk', label: 'C 盘全景', icon: <FolderOpenOutlined /> },
  { key: 'migration', label: '迁移中心', icon: <SwapOutlined /> },
  { key: 'recycle', label: '回收站', icon: <DeleteOutlined /> },
];

// 与 NAV 的 label 一致：标题栏回答"我在哪"，应当和侧栏高亮的那一项同名。
// 页面内部的 h1 回答"这页讲什么"，两者不重复（原先标题栏写"仪表盘 / 总览"、
// 页面 h1 写"系统总览"，同一屏上两个近义标题）。
const TITLES: Record<View, string> = {
  dashboard: '仪表盘',
  software: '软件台账',
  disk: 'C 盘全景',
  migration: '迁移中心',
  recycle: '回收站',
  settings: '设置',
};

export default function App() {
  const { theme, toggle } = useTheme();
  return (
    <ThemeProvider theme={theme}>
      <InnerApp theme={theme} toggleTheme={toggle} />
    </ThemeProvider>
  );
}

function InnerApp({ theme, toggleTheme }: { theme: 'dark' | 'light'; toggleTheme: () => void }) {
  // 从 App context 取，不用静态的 —— 静态方法读全局主题，见 theme.tsx
  const { modal, message } = AntdApp.useApp();
  const [data, setData] = useState<LpData | null>(null);
  const [drives, setDrives] = useState<DriveInfo[]>([]);
  const [err, setErr] = useState('');
  const [view, setView] = useState<View>('dashboard');
  const [ownerId, setOwnerId] = useState<string | null>(null);

  // 回收区占用挂在侧栏徽标上：清理后"空间没真腾出来"这件事得一眼看见，
  // 否则用户会以为工具在骗他。
  const [recycleBytes, setRecycleBytes] = useState(0);

  /**
   * 提权状态与盲区目录数，供侧栏底部那块显示。
   *
   * **原先那行字是硬编码的"非管理员 · 有盲区"。** 以管理员身份运行时它照样
   * 这么说——看起来在报告运行状态，实际什么都没读，属于本项目 MEMORY 里记的
   * "假测试"同一族：一个永远输出同样结论的东西，不具备任何判别力。
   * `/api/settings` 一直有真的 `engine.elevated` 与 `snapshot.denied_count`。
   *
   * null 表示还没读到，此时不显示任何结论——宁可留空，也不先说一句待会儿
   * 可能要被推翻的话。
   */
  const [env, setEnv] = useState<{
    /** 当前进程是否提权（此刻的事实） */
    elevated: boolean;
    /** 快照里记的不可读目录数 */
    denied: number;
    /**
     * 产出当前快照时是否提权。**与 elevated 是两件事。**
     *
     * null = 旧快照没记（当"不知道"处理，不当作 false，否则老快照会被误报成
     * "需要重扫"）。判"该不该重扫"只能用这两者的差：进程现在提权了、而快照是
     * 非提权时扫的 —— 那才是真的该重扫。
     *
     * **不能用 `denied > 0` 判**：以管理员扫完照样有读不到的目录（系统保护目录、
     * 正被独占打开的文件，实测提权后仍有 17 个），那样会永远提示"待重扫"，
     * 用户扫多少次都甩不掉。
     */
    snapElevated: boolean | null;
  } | null>(null);

  const refresh = () => {
    fetchData()
      .then(setData)
      .catch((e) => setErr(e?.message ?? String(e)));
    fetchDrives()
      .then(setDrives)
      .catch(() => setDrives([]));
    fetchRecycle()
      .then((r) => setRecycleBytes(r.summary.total_size))
      .catch(() => setRecycleBytes(0));
    fetchSettings()
      .then((s) => setEnv({
        elevated: s.engine.elevated,
        denied: s.snapshot.denied_count ?? 0,
        snapElevated: s.snapshot.elevated ?? null,
      }))
      .catch(() => setEnv(null));
  };

  useEffect(() => {
    refresh();
  }, []);

  /**
   * 提权说明。**只讲怎么做，不代做。**
   *
   * 曾经这里是"点一下自动提权"（`Start-Process -Verb RunAs` 重启自己），已撤掉 ——
   * 见 `desktop/main.js` 里那段说明：功能本身通，但两个进程之间交接端口、单实例锁、
   * 引擎进程的时序问题改了四轮都没穷尽，最糟的一次留下了标准权限终端杀不掉的
   * 提权残留，把一次失败变成了持续故障。
   *
   * 右键「以管理员身份运行」没有交接问题：旧实例由用户自己关闭，端口、锁、引擎
   * 全部干净释放。多两步操作换掉一整类时序缺陷。
   */
  const showElevateHelp = () => {
    modal.info({
      title: '如何以管理员权限运行',
      okText: '知道了',
      width: 520,
      content: (
        <div style={{ fontSize: 'var(--fs-md)', lineHeight: 1.75 }}>
          <ol style={{ paddingLeft: 20, margin: '8px 0 10px' }}>
            <li>完全退出 LostPath</li>
            <li>右键点击 LostPath 图标，选择「以管理员身份运行」</li>
            <li>在 Windows 的 UAC 提示中确认</li>
            <li>启动后<b>重新扫描一次</b>，此前不可读的目录才会纳入数据</li>
          </ol>
          <p style={{ color: 'var(--tx2)', margin: '0 0 8px' }}>
            管理员权限仅影响<b>可读取的目录范围</b>。当前有 {env?.denied ?? 0} 项目录
            因权限不足无法读取，其体积不计入任何统计（归入「其他已用」）。
          </p>
          <p style={{ color: 'var(--tx2)', margin: '0 0 8px' }}>
            扫描过程全程只读。提升权限不会增加任何写入操作 —— 写入仅限三处：
            保存快照、登记便携软件、执行清理与迁移，且均需逐项确认。
          </p>
          <p style={{ color: 'var(--tx3)', margin: 0, fontSize: 'var(--fs-sm)' }}>
            本程序不提供「一键提权」：那需要在两个进程间交接服务端口与运行状态，
            实测存在难以穷尽的时序问题，且失败时会留下标准权限无法结束的残留进程。
          </p>
        </div>
      ),
    });
  };

  /**
   * 当前快照是否需要重扫。
   *
   * 判据是**权限差**而非不可读目录数：进程现在有管理员权限，而这份快照是以标准
   * 权限扫的 —— 那些当时读不到的目录现在能读了，但数据里还没有。
   *
   * `snapElevated === null`（旧快照未记录该字段）时**不提示**：宁可漏提一次，
   * 也不对着一份无从判断的快照断言它过期。
   */
  const staleSnapshot = !!env && env.elevated && env.snapElevated === false;

  const openOwner = (name?: string | null) => {
    if (!name || !data) return false;
    const ent = data.software.find(
      (g) => g.name === name || g.traces?.some((t) => t.owner === name),
    );
    setOwnerId(ent ? ent.id : null);
    setView('software');
    return true;
  };

  if (err)
    return (
      <div style={{ padding: 48 }}>
        <Alert
          type="error"
          showIcon
          message="无法连接 LostPath 本地服务"
          description={`${err} —— 请先启动引擎：conda run -n lostpath python engine/main.py`}
        />
      </div>
    );
  if (!data)
    return (
      // 首屏用骨架而非转圈：`Spin tip` 在非嵌套模式下 antd 5 根本不渲染那行字
      // （源码里就 warn 了这件事），所以此前这里是一个无说明的转圈。骨架还能
      // 预留版位，数据到位时不跳版（Skill 的 content-jumping 一条）。
      // aria-busy + role=status 让屏幕阅读器知道"在加载"，而不是"页面空的"。
      <div style={{ padding: '22px 26px' }} role="status" aria-busy="true">
        <Skeleton active title={{ width: 180 }} paragraph={{ rows: 1, width: ['46%'] }} />
        <div style={{ display: 'flex', gap: 14, margin: '18px 0' }}>
          <Card size="small" style={{ flex: 1 }}>
            <Skeleton active title={false} paragraph={{ rows: 3 }} />
          </Card>
          <Card size="small" style={{ flex: 1.6 }}>
            <Skeleton active title={false} paragraph={{ rows: 3 }} />
          </Card>
        </div>
        <Card size="small">
          <Skeleton active title={false} paragraph={{ rows: 4 }} />
        </Card>
      </div>
    );

  return (
    <div style={{ height: '100vh', display: 'flex', background: 'var(--bg)' }}>
      {/* 侧边栏 */}
      <aside
        className="lp-sidebar"
        style={{
          width: 228,
          flexShrink: 0,
          // className 是给 index.css 里的 .lp-sidebar 用的：侧栏是全应用唯一以
          // --deep 为底的面，而浅色下 --deep 比 --bg 更**亮**，几个前景色压上去
          // 会掉线（--amber 3.81、--tx3 4.10、--green 3.97）。与其逐处改颜色，
          // 不如在这一个容器里把这些令牌整体覆盖掉——以后往侧栏加东西就自动安全。
          background: 'var(--deep)',
          borderRight: '1px solid var(--line)',
          display: 'flex',
          flexDirection: 'column',
          padding: '16px 12px',
        }}
      >
        {/* 品牌标识区。标题栏藏了之后左上角这块也要能拖窗口，否则窗口只有右半边
            顶栏能拖。下面的导航按钮是独立元素，不受这个 drag 影响。 */}
        <div
          style={{
            display: 'flex', gap: 10, alignItems: 'center', padding: '2px 8px 18px',
            WebkitAppRegion: 'drag',
          } as React.CSSProperties}
        >
          {/* 软件自己的 logo（ico/LostPath.png，与窗口图标、安装包图标同一份）。
              aria-hidden：品牌标记不承载信息，旁边的 "LostPath" 文字才是可访问名。
              加载失败回退到内置图标——图标缺失不该让品牌区变成一个空洞。 */}
          <div className="lp-logo" aria-hidden="true">
            <img
              src="/logo.png"
              alt=""
              onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
            />
            <RadarChartOutlined className="lp-logo-fallback" />
          </div>
          <div style={{ minWidth: 0 }}>
            {/* 品牌字：渐变描字 + 收紧字距。渐变方向与 logo 一致，两者看着是一套。
                字重 700 配 -0.3px 字距——无衬线体在大字重下默认字距偏松，收一点
                才像刻意排过版而不是"把字号调大了"。 */}
            <div className="lp-brand">LostPath</div>
            <div className="lp-brand-sub">Trace Manager</div>
          </div>
        </div>

        {NAV.map((n) => {
          const active = view === n.key;
          return (
            // 用 button + aria-current 而非 div：原先图标与文字并排、文字是裸文本
            // 节点，导航项没有可访问名——屏幕阅读器读不出来，键盘也无法聚焦切换。
            <button
              key={n.key}
              type="button"
              onClick={() => setView(n.key as View)}
              aria-current={active ? 'page' : undefined}
              className={`lp-nav ${active ? 'lp-nav-active' : ''}`}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                width: '100%',
                textAlign: 'left',
                font: 'inherit',
                padding: '9px 12px',
                borderRadius: 'var(--radius-md)',
                marginBottom: 4,
                cursor: 'pointer',
                fontSize: 'var(--fs-md)',
                background: 'transparent',
                color: active ? undefined : 'var(--tx2)',
                border: '1px solid transparent',
              }}
            >
              <span style={{ fontSize: 16, display: 'flex', alignItems: 'center' }}>{n.icon}</span>
              <span>{n.label}</span>
              {n.key === 'recycle' && recycleBytes > 0 && (
                <span
                  style={{
                    marginLeft: 'auto',
                    fontSize: 'var(--fs-xs)',
                    padding: '1px 6px',
                    borderRadius: 'var(--radius-full)',
                    // 底用 color-mix 从当前主题的 --red 现算，而不是写死一个 rgba：
                    // 原先写死的是深色版的红，浅色主题下底色和字色会一起偏，
                    // 结果对比度掉到 3.35。字色走 --danger-fg，见 index.css。
                    background: 'color-mix(in srgb, var(--red) 15%, transparent)',
                    color: 'var(--danger-fg)',
                    fontWeight: 600,
                  }}
                >
                  {fmtSize(recycleBytes)}
                </span>
              )}
            </button>
          );
        })}

        <div style={{ flex: 1 }} />

        {/* 主题与设置这两个按钮**刻意不挂 Tooltip**：它们已经把状态写在标签上
            （「主题：浅色」「设置」），悬停再弹一个框只是复述，还会遮住旁边的项。
            Tooltip 该用在"图标不自明"或"有额外信息"的地方——底部那块权限状态就
            属于后者（要说清盲区是什么），所以它保留。 */}
          <button
            type="button"
            onClick={toggleTheme}
            className="lp-nav"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              width: '100%',
              textAlign: 'left',
              font: 'inherit',
              background: 'transparent',
              border: '1px solid transparent',
              padding: '8px 12px',
              borderRadius: 'var(--radius-md)',
              fontSize: 'var(--fs-md)',
              color: 'var(--tx2)',
              cursor: 'pointer',
              marginBottom: 4,
            }}
          >
            {/* 原先这里是 🌙 / ☀️ 两个 emoji。emoji 的字形由系统字体决定，
                跨机器不一致、受不到 fontSize/color 控制，也不随主题变色；
                项目已装 @ant-design/icons，没有理由用它当结构性图标。 */}
            {theme === 'dark' ? <MoonOutlined /> : <SunOutlined />}
            <span>主题：{theme === 'dark' ? '深色' : '浅色'}</span>
          </button>
          <button
            type="button"
            onClick={() => setView('settings')}
            aria-current={view === 'settings' ? 'page' : undefined}
            className={`lp-nav ${view === 'settings' ? 'lp-nav-active' : ''}`}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              width: '100%',
              textAlign: 'left',
              font: 'inherit',
              background: 'transparent',
              padding: '8px 12px',
              borderRadius: 'var(--radius-md)',
              fontSize: 'var(--fs-md)',
              color: view === 'settings' ? undefined : 'var(--tx2)',
              cursor: 'pointer',
              border: '1px solid transparent',
            }}
          >
            <SettingOutlined />
            <span>设置</span>
          </button>
        {/*
          这块要同时说清两件**来源不同**的事，别混成一句：
            env.elevated —— 当前进程有没有管理员权限（实时）
            env.denied   —— 快照里记的读不到的目录数（**扫描当时**的事实）

          刚提权完的那一刻两者必然打架：权限已经是管理员，而快照还是非管理员时
          扫的，96 这个数字一个字节没变。原先这里写"以管理员身份运行，盲区已最小化
          （仍有 96 个系统保护目录读不到）"——把旧快照的遗留说成提权后的实况，
          用户会以为已经解决了，其实统计里那部分体积还是缺的。
        */}
        <Tooltip
          title={
            env == null
              ? '正在读取运行状态'
              : staleSnapshot
                ? '已获得管理员权限，但当前数据仍是标准权限下采集的。重新扫描后，此前不可读的目录将计入统计。'
                : env.elevated
                  ? (env.denied
                    ? `以管理员权限运行。仍有 ${env.denied} 项不可读，属于系统保护目录或被独占占用，提升权限亦无法访问。`
                    : '以管理员权限运行，全部目录均可读取。')
                  : `以标准权限运行，${env.denied} 项目录不可读取，其体积不计入统计。可在下方提升权限。`
          }
        >
          <div
            style={{
              marginTop: 10,
              padding: '9px 12px',
              borderRadius: 8,
              background: 'var(--panel)',
              border: '1px solid var(--line)',
              display: 'flex',
              gap: 9,
              alignItems: 'center',
            }}
          >
            <div
              style={{
                width: 26,
                height: 26,
                borderRadius: '50%',
                background: 'var(--line)',
                display: 'grid',
                placeItems: 'center',
                fontSize: 13,
                color: env?.elevated ? 'var(--green)' : 'var(--tx2)',
              }}
            >
              {/* 原先是 🖥️ emoji，理由同主题按钮那处 */}
              {env?.elevated ? <SafetyCertificateOutlined /> : <DesktopOutlined />}
            </div>
            <div style={{ lineHeight: 1.3, minWidth: 0 }}>
              <div style={{ fontSize: 'var(--fs-sm)', color: 'var(--tx)' }}>本机扫描</div>
              <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--tx3)' }}>
                {env == null
                  ? '读取中'
                  : env.elevated
                    ? (staleSnapshot ? '管理员 · 数据待更新' : '管理员权限')
                    : `标准权限 · ${env.denied} 项不可读`}
              </div>
            </div>
          </div>
        </Tooltip>

        {/* 标准权限且确实有不可读目录时，给一个**说明**入口（不是代做）。
            只在有不可读目录时出现 —— 没有盲区的机器上摆一个提权入口属于无谓地
            劝人提权。文案是「如何提升权限」而非「提升权限」：点它不会发生任何事，
            只会告诉你怎么做，这一点必须从标签上就看得出来。 */}
        {env && !env.elevated && env.denied > 0 && (
          <button
            type="button"
            className="lp-nav"
            onClick={showElevateHelp}
            style={{
              marginTop: 6, width: '100%', display: 'flex', alignItems: 'center',
              gap: 8, justifyContent: 'center', font: 'inherit',
              // 用 --accent-fg 而非 --blue2：这颗按钮在侧栏（底是 --deep），
              // 浅色下 --blue2 压上去只有 2.65。11px 的字尤其经不起。
              fontSize: 'var(--fs-xs)', color: 'var(--accent-fg)',
              background: 'transparent', border: '1px dashed var(--line2)',
              borderRadius: 8, padding: '7px 10px', cursor: 'pointer',
            }}
          >
            <SafetyCertificateOutlined />
            <span>如何提升权限</span>
          </button>
        )}

        {/* 已获得管理员权限、但数据仍是标准权限下采集的。
            **提升权限不会改变任何已有数据**，须重新扫描才会纳入此前不可读的目录。
            判据是 staleSnapshot（权限差），不是 denied > 0 —— 后者在管理员下
            也恒为真（系统保护目录始终不可读），会导致提示永不消失。 */}
        {staleSnapshot && (
          <button
            type="button"
            className="lp-nav"
            onClick={() => setView('dashboard')}
            style={{
              marginTop: 6, width: '100%', display: 'flex', alignItems: 'center',
              gap: 8, justifyContent: 'center', font: 'inherit',
              fontSize: 'var(--fs-xs)', color: 'var(--amber)',
              background: 'transparent', border: '1px dashed var(--amber)',
              borderRadius: 8, padding: '7px 10px', cursor: 'pointer',
              textAlign: 'left', lineHeight: 1.5,
            }}
          >
            <RadarChartOutlined style={{ flexShrink: 0 }} />
            <span>重新扫描以更新数据</span>
          </button>
        )}
      </aside>

      {/* 主区 */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        {/*
          顶栏 = 窗口标题栏。
          --------------------------------------------------------------
          原先这里是「LostPath / 仪表盘 / 总览」的面包屑加一句「数据版本 · 本机扫描
          快照 × 实时台账」，三处问题：

          ① **面包屑名不副实**。面包屑的作用是在层级里定位并逐级回退，而这里是平级
             的五个视图、「LostPath」那一级也点不动。只有一层的地方不需要面包屑
             （Skill 的 breadcrumb-web 一条：3 层以上才用）。
          ② **「数据版本」那句是常量**，任何页面任何时刻都显示同一行字，不随数据变化，
             占着整条右侧却不携带信息。真正会变的"上次扫描时间"在仪表盘标题下面。
          ③ **品牌标识重复**：原生标题栏显示一次图标+LostPath，侧栏顶部又一次。

          现在：藏掉原生标题栏（见 desktop/main.js 的 titleBarStyle），这一条就是
          标题栏——整条可拖动窗口，只留当前页面名，右侧给原生窗口控件留位。
          侧栏那份成为唯一的品牌标识。
        */}
        <div
          style={{
            height: 40,
            flexShrink: 0,
            borderBottom: '1px solid var(--line)',
            display: 'flex',
            alignItems: 'center',
            padding: '0 22px',
            gap: 10,
            // 整条作为窗口拖动区。里面的可交互元素要单独声明 no-drag，
            // 否则点不动——目前这一条里没有可交互元素。
            WebkitAppRegion: 'drag',
            // 桌面端右侧留出最小化/最大化/关闭三个原生控件的宽度（Windows 上
            // 约 138px，留 146 有余量）。浏览器里没有它们，不留。
            paddingRight: isDesktop() ? 146 : 22,
          } as React.CSSProperties}
        >
          <span style={{
            color: 'var(--tx)', fontSize: 'var(--fs-lg)', fontWeight: 600,
          }}>
            {TITLES[view]}
          </span>
        </div>
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          {view === 'dashboard' && (
            <DashboardPage
              data={data}
              drives={drives}
              elevated={!!env?.elevated}
              onOpenEntity={(id) => {
                setOwnerId(id);
                setView('software');
              }}
              onGoto={setView}
              onRefresh={refresh}
            />
          )}
          {view === 'software' && (
            <SoftwarePage
              data={data}
              owner={ownerId}
              onSelect={setOwnerId}
              onRefresh={refresh}
              theme={theme}
              onGotoMigration={() => setView('migration')}
            />
          )}
          {view === 'disk' && <DiskTreePage data={data} onOpenOwner={openOwner} />}
          {/* 计划由 /api/plan 只读算出，不复用 data —— 拦阻判定要查磁盘实况 */}
          {view === 'migration' && <MigrationPage onRefresh={refresh} />}
          {view === 'recycle' && <RecyclePage onRefresh={refresh} />}
          {view === 'settings' && <SettingsPage />}
        </div>
      </div>
    </div>
  );
}
