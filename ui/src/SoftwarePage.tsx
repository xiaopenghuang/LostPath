import { useEffect, useMemo, useState } from 'react';
import {
  Alert, App, Button, Card, Checkbox, Empty, Input, List, Modal, Progress, Segmented, Space,
  Table, Tag, Tooltip, Tree, Typography,
} from 'antd';
import {
  ApiOutlined, CheckCircleFilled, HddOutlined, LoadingOutlined, MinusCircleFilled,
  PartitionOutlined, SearchOutlined, SortDescendingOutlined,
} from '@ant-design/icons';
import type { DataNode } from 'antd/es/tree';
import {
  ACTION_LABEL, BLOCKER_LABEL, CAT_COLOR, confirmPortable, entityStatus, fetchBodyTree, fetchPlan,
  fmtSize, LpData, LpNode, Plan, scanPortable, SoftwareEntity, SOURCE_LABEL, ZONE_LABEL,
} from './api';
import EntityGraph from './EntityGraph';
import { Scan, useScan } from './useScan';

const pct = (c?: number | null) => (c == null ? '—' : `${Math.round(c * 100)}%`);

/**
 * 依据软件名称哈希生成稳定色相，让没有真图标的软件之间仍能靠颜色区分。
 *
 * **只返回色相，不返回亮度。** 亮度由 --tile-fg-l 按主题钉死（见 index.css）：
 * hsl 的 L 是数学亮度而非感知亮度，同一个 L 在黄色（h≈60）和蓝紫（h≈240）下
 * 实际明暗差一倍。原先写死 L=60% 时扫过 360 个色相：深色下 160 个不过 4.5:1
 * （最差 h=240 只有 2.62），浅色下 360 个全不过（最差 1.12，基本看不见）。
 * 而首页"占用大户"6 项当前一个真图标都没有，全靠这个回退显示。
 */
function getTileHue(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  return Math.abs(hash) % 360;
}

/** 软件图标：优先真实图标（引擎从 exe 提取的 PNG），加载失败回退首字母。 */
export function AppTile({ e, size }: { e: SoftwareEntity; size: number }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [e.icon]);

  const hue = useMemo(() => getTileHue(e.name || 'App'), [e.name]);
  const initial = (e.name?.[0] || '?').toUpperCase();

  return (
    <div
      style={{
        position: 'relative',
        width: size,
        height: size,
        borderRadius: Math.round(size * 0.28),
        background: 'var(--panel2)',
        border: '1px solid var(--line)',
        boxShadow: 'var(--shadow-sm), inset 0 1px 0 rgba(255,255,255,0.06)',
        display: 'grid',
        placeItems: 'center',
        fontSize: Math.round(size * 0.42),
        fontWeight: 700,
        flexShrink: 0,
        overflow: 'hidden',
        userSelect: 'none',
      }}
    >
      {/* 图标缺失或提取失败时的首字母微质感回退。
          饱和度与亮度走 --tile-fg-s / --tile-fg-l，两个主题各一套；色相是唯一
          随软件名变化的量，所以对比度不会随数据浮动。 */}
      <span
        style={{
          display: 'grid',
          placeItems: 'center',
          width: '100%',
          height: '100%',
          background: `hsla(${hue}, 40%, 50%, 0.12)`,
          color: `hsl(${hue}, var(--tile-fg-s), var(--tile-fg-l))`,
          letterSpacing: -0.5,
        }}
      >
        {initial}
      </span>
      {e.icon && !failed && (
        <img
          src={e.icon}
          alt=""
          onError={() => setFailed(true)}
          style={{
            position: 'absolute',
            inset: 0,
            width: '100%',
            height: '100%',
            objectFit: 'contain',
            padding: Math.round(size * 0.12),
            background: 'var(--panel2)',
          }}
        />
      )}
    </div>
  );
}

export function EvidenceBlock({ node }: { node: LpNode }) {
  const kids = [...(node.children ?? [])].sort((a, b) => (b.size ?? 0) - (a.size ?? 0));
  return (
    <div
      style={{
        padding: '10px 14px',
        background: 'var(--bg)',
        borderLeft: '2px solid rgba(47,129,247,0.55)',
        borderRadius: 6,
      }}
    >
      <div style={{ marginBottom: 6 }}>
        <b>判定：</b>
        {node.why || '—'}（置信度 {pct(node.conf)}）
        {node.family && <Tag style={{ marginLeft: 8 }} bordered={false}>族系 {node.family}</Tag>}
      </div>
      {(node.evidence ?? []).length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <b>证据链：</b>
          {(node.evidence ?? []).map((ev, i) => (
            <div key={i} style={{ margin: '3px 0' }}>
              <Tag color="blue" bordered={false}>{ev.source}</Tag>
              {ev.detail} <Tag bordered={false}>{pct(ev.conf)}</Tag>
            </div>
          ))}
        </div>
      )}
      {node.redirect && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message={`junction 重定向 → ${node.redirect}（体积已去重，不计两次）`}
        />
      )}
      {kids.length > 0 && (
        <>
          <b>构成（子目录 {kids.length} 项）：</b>
          <Table<LpNode>
            size="small"
            rowKey="path"
            pagination={false}
            style={{ marginTop: 4 }}
            dataSource={kids.slice(0, 12)}
            columns={[
              { title: '大小', width: 100, render: (_, k) => <b className="lp-num" style={{ color: 'var(--cyan)' }}>{fmtSize(k.size)}</b> },
              { title: '子目录', render: (_, k) => <code className="lp-mono" style={{ fontSize: 12 }}>{k.path}</code> },
              { title: '说明', width: 240, render: (_, k) => k.role || k.why || '' },
            ]}
            // 没有省略项时**必须返回 undefined 而不是空字符串**：返回 '' 时 antd
            // 照样渲染一个空的 .ant-table-footer 容器，表格下方多出一截空条
            // （浅色主题下它还是纯黑，因为 footerBg 同样吃那个 transparent 派生，
            // 见 theme.tsx）。子目录不足 12 项时这里本来就不该有 footer。
            footer={
              kids.length > 12
                ? () => `其余 ${kids.length - 12} 项更小的子目录未列出`
                : undefined
            }
          />
        </>
      )}
    </div>
  );
}

const treeToAntd = (n: BodyTreeNode): DataNode => ({
  key: n.path,
  title: (
    <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
      <b className="lp-num" style={{ minWidth: 76, color: 'var(--cyan)' }}>{fmtSize(n.size)}</b>
      <span>{n.name}</span>
      <span style={{ color: 'var(--tx3)', fontSize: 12 }}>{n.files.toLocaleString()} 文件</span>
    </span>
  ),
  children: n.children.map(treeToAntd),
});

interface BodyTreeNode {
  name: string;
  path: string;
  size: number;
  files: number;
  children: BodyTreeNode[];
}

function BodyCard({ e }: { e: SoftwareEntity }) {
  // 见 theme.tsx：静态 message 读全局主题，跟不上 ConfigProvider 切换
  const { message } = App.useApp();
  const [tree, setTree] = useState<BodyTreeNode | null>(null);
  const [loading, setLoading] = useState(false);
  const loadTree = async () => {
    if (!e.location) return;
    setLoading(true);
    try {
      setTree(await fetchBodyTree(e.location));
    } catch {
      message.error('本体树扫描失败');
    } finally {
      setLoading(false);
    }
  };
  return (
    <Card
      size="small"
      style={{ marginBottom: 14 }}
      title={
        <Space size={8} wrap>
          <HddOutlined style={{ color: 'var(--accent-fg)' }} />
          <span>"本体"安装位置</span>
        </Space>
      }
      extra={
        e.location_exists && (
          <Button size="small" loading={loading} onClick={loadTree}>
            加载文件树
          </Button>
        )
      }
    >
      {!e.location ? (
        <Alert
          type="info"
          showIcon={false}
          message="本体位置未定位：注册表没有 InstallLocation，且 UninstallString / DisplayIcon 无法解析出有效目录"
        />
      ) : (
        <>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              background: 'var(--bg)',
              border: '1px solid var(--line)',
              borderRadius: 8,
              padding: '9px 13px',
              flexWrap: 'wrap',
            }}
          >
            <code className="lp-mono" style={{ fontSize: 12.5, color: 'var(--cyan)', flex: 1 }}>
              {e.location}
            </code>
            {e.location_exists ? (
              <Tag color="green" bordered={false}>✓ 已验证存在</Tag>
            ) : (
              <Tag color="red" bordered={false}>目录不存在</Tag>
            )}
            {e.location_basis && <Tag bordered={false}>{e.location_basis}</Tag>}
          </div>
          <div style={{ marginTop: 10, display: 'flex', gap: 34 }}>
            <div>
              <div style={{ fontSize: 11, color: 'var(--tx3)' }}>登记大小</div>
              <b className="lp-num" style={{ fontSize: 16 }}>{fmtSize(e.estimated_size)}</b>
            </div>
            <div>
              <div style={{ fontSize: 11, color: 'var(--tx3)' }}>聚合碎片</div>
              <b className="lp-num" style={{ fontSize: 16 }}>{e.fragments.length}</b>
            </div>
            {e.exe_path && (
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 11, color: 'var(--tx3)' }}>主程序</div>
                <code className="lp-mono" style={{ fontSize: 11.5 }}>{e.exe_path}</code>
              </div>
            )}
          </div>
          {tree && (
            <Tree
              blockNode
              showLine
              selectable={false}
              defaultExpandedKeys={[tree.path]}
              treeData={[treeToAntd(tree)]}
              style={{ marginTop: 10 }}
            />
          )}
          {e.fragments.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <Tag color="purple" bordered={false}>已聚合碎片 {e.fragments.length} 项</Tag>
              <span style={{ color: 'var(--tx3)', fontSize: 12 }}>
                {e.fragments.slice(0, 8).join('、')}
                {e.fragments.length > 8 ? ' …' : ''}
              </span>
            </div>
          )}
        </>
      )}
    </Card>
  );
}

/**
 * 每条痕迹前的可处理性标记。原先这里是个永久 disabled 的复选框，提示"清理动作在
 * M4 交付"——批量选择从来没做，那个框只是让人以为能勾。改成如实标记：能不能处理、
 * 不能的话为什么，鼠标悬停看理由。
 */
function TraceActionMark({ plan, loading }: { plan?: Plan; loading: boolean }) {
  if (loading) {
    return <LoadingOutlined style={{ color: 'var(--tx3)', fontSize: 15 }} />;
  }
  if (!plan) {
    return (
      <Tooltip title="计划器未把这条列为候选（通常是体积过小，低于 50 MiB 不值得动）">
        <MinusCircleFilled style={{ color: 'var(--line2)', fontSize: 15 }} />
      </Tooltip>
    );
  }
  if (plan.executable) {
    return (
      <Tooltip title={`可处理：${ACTION_LABEL[plan.action]} · 预计腾出 ${fmtSize(plan.reclaimable)}`}>
        <CheckCircleFilled style={{ color: 'var(--green)', fontSize: 15 }} />
      </Tooltip>
    );
  }
  return (
    <Tooltip
      title={
        <>
          暂不可处理：
          {plan.blockers.map((b) => (
            <div key={b.code}>· {b.reason}</div>
          ))}
        </>
      }
    >
      <MinusCircleFilled style={{ color: 'var(--amber)', fontSize: 15 }} />
    </Tooltip>
  );
}

/**
 * 可处理性汇总。一律以 /api/plan 为准——详情页自己按 cat 猜过一版，结果页面说
 * "建议清理"而没有任何地方能清理，因为真正掌管动作的计划器还会查磁盘实况
 * （占用中、置信度、环境变量冲突），两套判断对不上。
 */
function ActionabilityCard({
  doable, blocked, doableSize, loading, err, hasTraces, onGotoMigration,
}: {
  doable: { t: LpNode; p?: Plan }[];
  blocked: { t: LpNode; p?: Plan }[];
  doableSize: number;
  loading: boolean;
  err: string | null;
  hasTraces: boolean;
  onGotoMigration: () => void;
}) {
  if (!hasTraces) return null;
  if (loading) {
    return <Card size="small"><Space><LoadingOutlined />正在核算可处理性…</Space></Card>;
  }
  if (err) {
    return <Alert type="warning" showIcon message="可处理性算不出来" description={err} />;
  }

  // 拦阻按原因归并：同一个理由重复列 N 遍没有信息量
  const byCode = new Map<string, { reason: string; n: number; size: number }>();
  for (const { p } of blocked) {
    for (const b of p!.blockers) {
      const cur = byCode.get(b.code) ?? { reason: b.reason, n: 0, size: 0 };
      cur.n += 1;
      cur.size += p!.size ?? 0;
      byCode.set(b.code, cur);
    }
  }

  return (
    <>
      {doable.length > 0 && (
        <Alert
          type="success"
          showIcon
          message={`${doable.length} 处可处理，预计腾出 ${fmtSize(doableSize)}`}
          description={
            <>
              {doable.map(({ t, p }) => (
                <div key={t.path} style={{ fontSize: 11.5 }}>
                  · {t.role || t.name}：{ACTION_LABEL[p!.action]}（{fmtSize(p!.reclaimable)}）
                </div>
              ))}
              <Button size="small" type="primary" style={{ marginTop: 8 }} onClick={onGotoMigration}>
                去迁移中心处理
              </Button>
            </>
          }
        />
      )}
      {byCode.size > 0 && (
        <Alert
          type="info"
          showIcon={false}
          message={`${blocked.length} 处暂不可处理`}
          description={
            <div style={{ fontSize: 11.5, lineHeight: 1.75 }}>
              {[...byCode.entries()].map(([code, v]) => (
                <div key={code}>
                  <Tag bordered={false} style={{ marginRight: 4 }}>
                    {BLOCKER_LABEL[code] ?? code}
                  </Tag>
                  {v.n} 处 · {v.reason}
                </div>
              ))}
            </div>
          }
        />
      )}
      {doable.length === 0 && byCode.size === 0 && (
        <Alert
          type="info"
          showIcon={false}
          message="没有进入候选的痕迹"
          description="计划器只收 50 MiB 以上的目录，这些痕迹都在阈值以下。"
        />
      )}
    </>
  );
}

function EntityDetail({ e, onBack, plans, planErr, theme, scan, onGotoMigration }: {
  e: SoftwareEntity;
  onBack: () => void;
  /** path → 计划。null 表示还在算 */
  plans: Map<string, Plan> | null;
  planErr: string | null;
  theme: 'dark' | 'light';
  scan: Scan;
  onGotoMigration: () => void;
}) {
  const traces = [...(e.traces ?? [])].sort((a, b) => (b.size ?? 0) - (a.size ?? 0));
  const st = entityStatus(e, plans);

  // 可处理性一律以计划器为准，不再自己按 cat 猜。原先这里用 cat==='可再生缓存'
  // 判"建议清理"，而计划器还会查磁盘实况（占用中、置信度、环境变量冲突），
  // 两套判断各说一套，就出现了"页面说建议清理，却没有任何地方能清理"。
  const mine = traces
    .map((t) => ({ t, p: plans?.get(t.path) }))
    .filter((x) => x.p);
  const doable = mine.filter((x) => x.p!.executable);
  const blocked = mine.filter((x) => !x.p!.executable);
  const doableSize = doable.reduce((s, x) => s + (x.p!.reclaimable ?? 0), 0);

  return (
    <div className="lp-page" style={{ padding: '20px 26px' }}>
      <Button type="text" size="small" onClick={onBack} style={{ marginBottom: 10, color: 'var(--tx3)' }}>
        ← 返回软件台账
      </Button>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 16 }}>
        <AppTile e={e} size={46} />
        <div style={{ flex: 1 }}>
          <Space size={8} align="baseline">
            <span style={{ fontSize: 22, fontWeight: 700, color: 'var(--tx)' }}>{e.name}</span>
            {e.version && <Tag bordered={false}>v{e.version}</Tag>}
            <Tag bordered={false}>{SOURCE_LABEL[e.source]}</Tag>
          </Space>
          <div style={{ fontSize: 12, color: 'var(--tx3)' }}>
            {e.publisher ?? '未知发布商'} · 深度扫描结果与依赖分析
          </div>
        </div>
        <Tooltip title="引擎只做全盘扫描：痕迹归因依赖全局视图，单独重扫一个软件得不出归属">
          <Button
            icon={<SearchOutlined />}
            loading={scan.starting || scan.busy}
            onClick={scan.askBegin}
          >
            {scan.busy ? '扫描中…' : '重新扫描 C 盘'}
          </Button>
        </Tooltip>
      </div>

      {/* lp-split：1280 以下折成上下两段，否则右栏被挤到读不了（见 index.css） */}
      <div className="lp-split">
        <div style={{ flex: 1.9, minWidth: 0 }}>
          <BodyCard e={e} />
          <Card size="small" title={`C 盘痕迹（${traces.length} 处 · ${fmtSize(e.traces_size ?? 0)}）`}>
            {traces.length === 0 ? (
              <Alert type="success" showIcon={false} message="C 盘没有归因到该软件的显著足迹" />
            ) : (
              traces.map((t) => {
                const ev = (t.evidence ?? [])[0];
                return (
                  <div key={t.path} style={{ marginBottom: 8 }}>
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 12,
                        background: 'var(--bg)',
                        border: '1px solid var(--line)',
                        borderRadius: 8,
                        padding: '9px 13px',
                      }}
                    >
                      <TraceActionMark plan={plans?.get(t.path)} loading={plans === null && !planErr} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                          <b style={{ fontSize: 12.5, color: 'var(--tx)' }}>
                            {t.role || t.name}
                          </b>
                          <Tag color={CAT_COLOR[t.cat ?? '未定性']} bordered={false}>
                            {t.cat ?? '未定性'}
                          </Tag>
                          {t.zone && (
                            <span style={{ fontSize: 11, color: 'var(--tx3)' }}>
                              {ZONE_LABEL[t.zone] ?? t.zone}
                            </span>
                          )}
                        </div>
                        <code className="lp-mono" style={{ fontSize: 11.5, color: 'var(--tx2)' }}>
                          {t.path}
                        </code>
                        {ev && (
                          <div style={{ fontSize: 11, color: 'var(--tx3)', marginTop: 3 }}>
                            证据：<Tag color="blue" bordered={false} style={{ margin: 0 }}>{ev.source}</Tag>
                            {ev.detail} · {pct(ev.conf)}
                          </div>
                        )}
                      </div>
                      <b className="lp-num" style={{ color: 'var(--red)', fontSize: 14 }}>{fmtSize(t.size)}</b>
                    </div>
                    <div style={{ margin: '4px 0 0 36px' }}>
                      <EvidenceBlock node={t} />
                    </div>
                  </div>
                );
              })
            )}
          </Card>
        </div>

        {/* 右栏：图谱 + 可处理性 */}
        <div style={{ flex: 1.1, display: 'flex', flexDirection: 'column', gap: 12, minWidth: 300 }}>
          <Card
            size="small"
            title={<Space size={8}><PartitionOutlined style={{ color: 'var(--accent-fg)' }} />关联图谱</Space>}
            styles={{ body: { padding: 6 } }}
          >
            <EntityGraph entity={e} theme={theme} height={320} />
            <div style={{
              fontSize: 'var(--fs-xs)', color: 'var(--tx3)', padding: '4px 6px 2px',
              lineHeight: 1.6,
            }}>
              中心 = 本软件 · 内环 = 本体 / C 盘痕迹 · 外环 = 子目录
              <br />
              圆与线宽 ∝ 占用 · 颜色 = 定性 · 悬停高亮 · 可拖拽缩放
            </div>
          </Card>

          <ActionabilityCard
            doable={doable}
            blocked={blocked}
            doableSize={doableSize}
            loading={plans === null && !planErr}
            err={planErr}
            hasTraces={traces.length > 0}
            onGotoMigration={onGotoMigration}
          />

          <Card size="small">
            <div style={{ fontSize: 11.5, color: 'var(--tx3)', lineHeight: 1.7 }}>
              状态：<Tag color={st.color} bordered={false}>{st.label}</Tag>
              <br />
              置信度 = 多条证据加权的结果；展开任一痕迹可查看完整证据链。
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

function PortableModal({ open, onClose, onConfirmed }: {
  open: boolean; onClose: () => void; onConfirmed: () => void;
}) {
  const { message } = App.useApp();
  const [path, setPath] = useState('');
  const [scanned, setScanned] = useState(false);
  const [cands, setCands] = useState<PortableCandidate[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const doScan = async () => {
    if (!path.trim()) return;
    setBusy(true);
    try {
      const r = await scanPortable(path.trim());
      setCands(r);
      setScanned(true);
      setSelected([]);
      if (r.length === 0) message.info('该目录下没有发现 exe（深度 2 层）');
    } catch {
      message.error('扫描失败');
    } finally {
      setBusy(false);
    }
  };
  const doConfirm = async () => {
    setBusy(true);
    try {
      await confirmPortable(cands.filter((c) => selected.includes(c.dir)));
      message.success(`已入库 ${selected.length} 个便携软件`);
      setCands([]);
      setScanned(false);
      setSelected([]);
      onConfirmed();
      onClose();
    } catch {
      message.error('入库失败');
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={<Space><ApiOutlined style={{ color: 'var(--accent-fg)' }} /> 便携软件发现</Space>}
      open={open}
      onCancel={onClose}
      width={760}
      footer={[
        <Button key="cancel" onClick={onClose}>关闭</Button>,
        <Button key="ok" type="primary" disabled={selected.length === 0} loading={busy} onClick={doConfirm}>
          确认入库（{selected.length}）
        </Button>,
      ]}
    >
      <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
        <Input
          placeholder="输入要扫描的目录，例如 G:\ 或 G:\Softwares（深度 2 层，只读）"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          onPressEnter={doScan}
        />
        <Button type="primary" loading={busy} onClick={doScan}>扫描</Button>
      </Space.Compact>
      {scanned && (
        <Table<PortableCandidate>
          size="small"
          rowKey="dir"
          loading={busy}
          dataSource={cands}
          pagination={cands.length > 10 ? { pageSize: 10 } : false}
          rowSelection={{ selectedRowKeys: selected, onChange: (k) => setSelected(k as string[]) }}
          columns={[
            {
              title: '候选软件',
              render: (_, c) => (
                <div>
                  <b>{c.name}</b>
                  {c.exe_count > 1 && <span style={{ color: 'var(--tx3)', fontSize: 12 }}>（{c.exe_count} 个 exe）</span>}
                </div>
              ),
            },
            { title: '位置', ellipsis: true, render: (_, c) => <code className="lp-mono" style={{ fontSize: 12 }}>{c.exe}</code> },
            { title: '大小', width: 90, render: (_, c) => <b className="lp-num">{fmtSize(c.size)}</b> },
          ]}
        />
      )}
    </Modal>
  );
}

interface PortableCandidate {
  name: string;
  dir: string;
  exe: string;
  exe_count: number;
  size: number;
}

const maxTrace = (data: LpData) =>
  Math.max(1, ...data.software.map((g) => g.traces_size ?? 0));

export default function SoftwarePage({
  data,
  owner,
  onSelect,
  onRefresh,
  theme,
  onGotoMigration,
}: {
  data: LpData;
  owner: string | null;
  onSelect: (id: string | null) => void;
  onRefresh: () => void;
  theme: 'dark' | 'light';
  onGotoMigration: () => void;
}) {
  const [q, setQ] = useState('');
  const scan = useScan(onRefresh);
  /**
   * 排序方式。
   *
   * 原先这里是个 `<Button>排序：痕迹大小</Button>`，**没有 onClick** —— 长得
   * 像个能点的控件，点了什么都不发生。项目上一轮才刚把"永久 disabled 的复选框"
   * 那类假控件改掉，这是同一族的遗留：宁可不放这个按钮，也不该放一个假的。
   *
   * 三档而非两档：按名字找是最常见的第二诉求（你记得软件叫什么，但不记得它占
   * 多少），而"可处理项优先"直接对应本工具的目的——先看能动手的那些。
   */
  const [sort, setSort] = useState<'traces' | 'name' | 'doable'>('traces');

  // 计划在这一层取：/api/plan 会对全部候选查磁盘实况，每开一个详情页重算一次太浪费。
  // 详情页渲染在本组件内部，所以这一份覆盖所有详情页的打开。
  const [plans, setPlans] = useState<Map<string, Plan> | null>(null);
  const [planErr, setPlanErr] = useState<string | null>(null);
  useEffect(() => {
    let dropped = false;
    setPlans(null);
    setPlanErr(null);
    fetchPlan()
      .then((r) => {
        if (dropped) return;
        setPlans(new Map(r.plans.map((p) => [p.path, p])));
      })
      .catch((err) => {
        if (!dropped) setPlanErr(err instanceof Error ? err.message : '读取计划失败');
      });
    return () => {
      dropped = true;
    };
  }, [data]);
  const [modalOpen, setModalOpen] = useState(false);
  const barMax = useMemo(() => maxTrace(data), [data]);
  const groups = useMemo(() => {
    const kw = q.trim().toLowerCase();
    const hit = !kw
      ? data.software
      : data.software.filter(
        (g) =>
          g.name.toLowerCase().includes(kw) ||
          (g.publisher ?? '').toLowerCase().includes(kw) ||
          (g.traces ?? []).some((t) => t.path.toLowerCase().includes(kw)) ||
          (g.location ?? '').toLowerCase().includes(kw),
      );
    // 复制再排：data.software 是上层持有的数组，原地 sort 会改到别的页面看到的顺序
    const out = [...hit];
    if (sort === 'name') {
      // localeCompare 带 'zh'：默认字典序会把中文名按码点排，"腾讯"落到 Z 之后。
      //
      // unresolved 一律沉底：255 个实体里有 65 个（全是 appx）的 name 是
      // `@{...?ms-resource://...}` —— Windows 的间接资源引用，得用
      // SHLoadIndirectString 才能解析成真名，后端目前没做。按痕迹排序时它们
      // 因为 traces=0 自然沉在底部，从没露过脸；按名称排序会把这 65 条整块
      // 顶到最前，第一屏全是乱码。这里不假装解析成功，只是不让没名字的东西
      // 占住最显眼的位置。
      const nameless = (g: SoftwareEntity) => (/^@\{|ms-resource:/.test(g.name) ? 1 : 0);
      out.sort((a, b) => nameless(a) - nameless(b)
        || a.name.localeCompare(b.name, 'zh'));
    } else if (sort === 'doable') {
      // 可处理的排前面，同为可处理的再按痕迹大小。plans 未就绪时这一档退化成
      // 按痕迹排序——不假装知道还没算出来的事。
      const score = (g: SoftwareEntity) =>
        entityStatus(g, plans).key === 'doable' ? 1 : 0;
      out.sort((a, b) => score(b) - score(a)
        || (b.traces_size ?? 0) - (a.traces_size ?? 0));
    } else {
      out.sort((a, b) => (b.traces_size ?? 0) - (a.traces_size ?? 0));
    }
    return out;
  }, [data, q, sort, plans]);

  if (owner) {
    const e = data.software.find((g) => g.id === owner);
    if (e)
      return (
        <EntityDetail
          e={e}
          onBack={() => onSelect(null)}
          plans={plans}
          planErr={planErr}
          theme={theme}
          scan={scan}
          onGotoMigration={onGotoMigration}
        />
      );
  }
  const current = groups.find((g) => g.id === owner);

  return (
    <div className="lp-page" style={{ padding: '20px 26px' }}>
      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <Input
          prefix={<SearchOutlined style={{ color: 'var(--tx3)' }} />}
          placeholder={`搜索软件 / 发布商 / 路径…（共 ${data.software.length} 个实体）`}
          allowClear
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ maxWidth: 420 }}
        />
        <Segmented<'traces' | 'name' | 'doable'>
          value={sort}
          onChange={setSort}
          options={[
            { label: '痕迹大小', value: 'traces', icon: <SortDescendingOutlined /> },
            { label: '名称', value: 'name' },
            { label: '可处理优先', value: 'doable' },
          ]}
        />
        <Tooltip title="全盘可处理项由计划器统一算，逐个软件点进去看不全">
          <Button style={{ marginLeft: 'auto' }} onClick={onGotoMigration}>
            全盘可处理项
          </Button>
        </Tooltip>
        <Button type="primary" onClick={() => setModalOpen(true)}>+ 便携软件</Button>
      </div>

      {groups.length === 0 && <Empty description="没有匹配的软件" />}
      {groups.map((g) => {
        const st = entityStatus(g, plans);
        const barPct = Math.round(((g.traces_size ?? 0) / barMax) * 100);
        const active = g.id === current?.id;
        return (
          // button 而非 div：原先是 `div onClick`，a11y 树里只有 StaticText，
          // 键盘 Tab 停不下来——255 行一行都打不开。aria-current 让屏幕阅读器
          // 报出"当前选中的是哪一个"，与侧栏导航同口径。
          <button
            key={g.id}
            type="button"
            onClick={() => onSelect(g.id)}
            aria-current={active ? 'true' : undefined}
            className="lp-item"
            style={{
              alignItems: 'center',
              gap: 14,
              padding: '11px 16px',
              borderRadius: 10,
              border: active ? '1px solid rgba(47,129,247,0.55)' : '1px solid var(--line)',
              background: active ? 'rgba(47,129,247,0.07)' : 'var(--panel)',
              marginBottom: 8,
            }}
          >
            <AppTile e={g} size={36} />
            <div style={{ width: 300, minWidth: 0 }}>
              <div style={{ display: 'flex', gap: 6, alignItems: 'baseline' }}>
                <Typography.Text strong ellipsis style={{ maxWidth: 220, color: 'var(--tx)' }}>
                  {g.name}
                </Typography.Text>
                {g.version && <span style={{ fontSize: 11, color: 'var(--tx3)' }}>v{g.version}</span>}
              </div>
              <code
                className="lp-mono"
                style={{
                  fontSize: 11,
                  color: 'var(--tx3)',
                  display: 'block',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
              >
                {g.location ?? '本体未定位'}
              </code>
            </div>
            <div style={{ width: 220 }}>
              <Progress percent={barPct} showInfo={false} size={['100%', 4]} strokeColor="var(--blue)" />
              <div style={{ fontSize: 10.5, color: 'var(--tx3)', marginTop: -2 }}>
                C 盘痕迹 {fmtSize(g.traces_size)}
                {(g.traces?.length ?? 0) > 0 && ` · ${g.traces!.length} 处`}
                {g.fragments.length > 0 && ` · ${g.fragments.length} 碎片`}
              </div>
            </div>
            <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
              {g.location && (
                <Tag bordered={false} style={{ fontFamily: 'Cascadia Code, Consolas, monospace' }}>
                  {g.location[0]}:
                </Tag>
              )}
              <Tag color={st.color} bordered={false} style={{ minWidth: 74, textAlign: 'center' }}>
                {st.label}
              </Tag>
            </div>
          </button>
        );
      })}

      <PortableModal open={modalOpen} onClose={() => setModalOpen(false)} onConfirmed={onRefresh} />
    </div>
  );
}
