import { Alert, Button, Card, Progress, Tooltip } from 'antd';
import { RadarChartOutlined, RocketOutlined } from '@ant-design/icons';
import { fmtSize, DriveInfo, LpData, SoftwareEntity } from './api';
import { AppTile } from './SoftwarePage';
import { Scan, useScan } from './useScan';

/**
 * 扫描状态与操作在 ./useScan：按钮要待在标题行里、进度卡片要在标题行下面，
 * 同一份状态渲染到两处不相邻的位置，组件包不住，所以是 hook。软件台账详情页
 * 也用同一个 hook 发起扫描。
 */
function ScanButtons({
  scan,
}: {
  scan: Scan;
}) {
  const { status, starting, busy, begin, askCancel } = scan;
  return (
    <div style={{ marginLeft: 'auto', display: 'flex', gap: 10, alignItems: 'center' }}>
      {busy && (
        <Button danger onClick={askCancel} disabled={status?.cancel_requested} style={{ borderRadius: 'var(--radius-md)' }}>
          {status?.cancel_requested ? '正在取消…' : '取消'}
        </Button>
      )}
      <Tooltip title={busy ? '扫描进行中' : '递归扫描 C 盘并重新归因，全程只读，覆盖前自动归档上一份快照'}>
        <Button
          type="primary"
          size="large"
          className="lp-scan-btn"
          icon={<RadarChartOutlined style={{ fontSize: 18 }} />}
          loading={starting || busy}
          onClick={begin}
          style={{
            height: 42,
            padding: '0 20px',
            borderRadius: 'var(--radius-md)',
            fontSize: 'var(--fs-md)',
          }}
        >
          {busy ? '全盘扫描中…' : '发起深度扫描'}
        </Button>
      </Tooltip>
    </div>
  );
}

function ScanPanels({ scan, elevated }: { scan: Scan; elevated: boolean }) {
  const { status, busy } = scan;
  const r = status?.result;
  return (
    <>
      {busy && (
        <Card size="small" style={{ marginBottom: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
            <span style={{ fontSize: 13, color: 'var(--tx)' }}>{status?.phase_label ?? '准备中'}</span>
            <span style={{ fontSize: 11.5, color: 'var(--tx3)', marginLeft: 'auto' }}>
              已用 {status?.elapsed_sec ?? 0}s
            </span>
          </div>
          <Progress percent={status?.percent ?? 0} status="active" showInfo={false} />
          <code
            className="lp-mono"
            style={{
              fontSize: 11, color: 'var(--tx3)', display: 'block', marginTop: 6,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
            }}
          >
            {status?.detail || '\u00a0'}
          </code>
        </Card>
      )}

      {status?.state === 'failed' && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 14 }}
          message="扫描失败"
          description={`${status.error ?? '未知错误'}（详情见用户目录 logs/scan.log；现有快照未被改动）`}
        />
      )}

      {status?.state === 'cancelled' && (
        <Alert type="info" showIcon style={{ marginBottom: 14 }} message="扫描已取消，现有快照未改动" />
      )}

      {status?.state === 'done' && r && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 14 }}
          message={`扫描完成 · ${r.scanned_files.toLocaleString()} 个文件 / ${r.scanned_dirs.toLocaleString()} 个目录，耗时 ${r.scan_elapsed_sec}s`}
          description={
            <div style={{ fontSize: 12 }}>
              <div>
                归因出 {r.entries} 处足迹 / {fmtSize(r.total_size)}，其中未归因 {fmtSize(r.unknown_size)}；
                台账 {r.registry_apps} 条注册表记录 + {r.appx} 个商店应用 + {r.shortcuts} 个快捷方式。
              </div>
              {r.denied_count > 0 && (
                <div style={{ marginTop: 4, color: 'var(--tx2)' }}>
                  {/* 原先无条件写"（非管理员盲区）"——以管理员扫完它照样这么说，
                      而那时剩下的恰恰是**提权也读不到**的那些（系统保护目录、
                      正被独占打开的文件）。把原因说错比不说更糟：用户会以为
                      再提一次权就能解决。 */}
                  {r.denied_count} 个目录不可读取，其体积未计入统计。
                  {elevated
                    ? '当前已是管理员，余下的属于系统保护目录或正被独占占用，提权也无法读取。'
                    : '以管理员身份运行可减少此类目录。'}
                </div>
              )}
              {r.index_warnings.length > 0 && (
                <div style={{ marginTop: 4, color: 'var(--red)' }}>
                  索引告警：{r.index_warnings.join('；')}
                </div>
              )}
            </div>
          }
        />
      )}
    </>
  );
}

function SegmentedDriveBar({
  drive,
  segments,
}: {
  drive: DriveInfo;
  segments: { label: string; color: string; size: number }[];
}) {
  const used = drive.total - drive.free;
  const segs = segments.map((s) => ({ ...s, size: Math.max(0, Math.min(s.size, used)) }));
  // 空闲当成正式一段画出来，而不是"track 剩下的部分"。原先它靠 --panel2 露底，
  // 与「其他已用」的 --line2 几乎一个灰，整条看不出分段。
  const all = [...segs.filter((s) => s.size > 0), {
    label: '空闲', color: 'var(--seg-free)', size: drive.free,
  }];
  const pctOf = (n: number) => (drive.total > 0 ? (n / drive.total) * 100 : 0);
  const freePct = Math.round(pctOf(drive.free));
  return (
    <div style={{ padding: '2px 0' }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
        fontSize: 'var(--fs-md)', marginBottom: 8, gap: 8,
      }}>
        <b style={{ color: 'var(--tx)', display: 'flex', alignItems: 'center', gap: 6 }}>
          <span
            className="lp-mono"
            style={{
              padding: '1px 5px',
              borderRadius: 'var(--radius-xs)',
              background: 'var(--panel2)',
              border: '1px solid var(--line2)',
              color: 'var(--accent-fg)',
              fontSize: 'var(--fs-sm)',
            }}
          >
            {drive.letter}
          </span>
          本地磁盘
        </b>
        <span className="lp-num" style={{ color: 'var(--tx2)', fontSize: 'var(--fs-sm)' }}>
          已用 {fmtSize(used)} / {fmtSize(drive.total)}
          {/* 剩余百分比比绝对值更能说明"还够不够用" */}
          <span
            style={{
              fontWeight: 600,
              color: freePct <= 10 ? 'var(--red)' : 'var(--tx2)',
              marginLeft: 8,
            }}
          >
            · 剩 {freePct}%
          </span>
        </span>
      </div>
      <div
        role="img"
        aria-label={`${drive.letter} 共 ${fmtSize(drive.total)}，${
          all.map((s) => `${s.label} ${fmtSize(s.size)}`).join('，')}`}
        style={{
          display: 'flex',
          height: 12,
          borderRadius: 'var(--radius-sm)',
          overflow: 'hidden',
          background: 'var(--line)',
          boxShadow: 'inset 0 1px 2px rgba(0,0,0,0.15)',
        }}
      >
        {all.map((s, i) => (
          // 用 antd Tooltip 而不是原生 title：原生的是系统样式黑框、延迟约 1 秒、
          // 位置不可控。多出来的信息是百分比（图例只给绝对值），所以值得加。
          // 但不加 cursor: pointer——这几段点了没有任何反应，指针形状是最强的
          // "这里可点"信号，用它换一个 tooltip 提示不划算。
          <Tooltip key={i} title={`${s.label}: ${fmtSize(s.size)} (${pctOf(s.size).toFixed(1)}%)`}>
            <div
              style={{
                width: `${pctOf(s.size)}%`,
                background: s.color,
                // 段与段之间留一道卡片底色的缝：即使两段颜色接近（或用户是色盲），
                // 边界仍然看得见——不让颜色单独承载"这里分段了"这个信息。
                boxShadow: i > 0 ? '-1px 0 0 0 var(--panel)' : undefined,
                transition: `all var(--dur-base) var(--ease-out)`,
              }}
            />
          </Tooltip>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 14, marginTop: 8, flexWrap: 'wrap' }}>
        {all.map((s, i) => (
          <span
            key={i}
            style={{
              fontSize: 'var(--fs-xs)',
              color: 'var(--tx2)',
              display: 'flex',
              gap: 5,
              alignItems: 'center',
            }}
          >
            <span style={{
              width: 8,
              height: 8,
              borderRadius: 'var(--radius-xs)',
              background: s.color,
              display: 'inline-block',
              flexShrink: 0,
              boxShadow: 'var(--shadow-sm)',
            }} />
            {s.label} <span className="lp-num" style={{ fontWeight: 500 }}>{fmtSize(s.size)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

export default function DashboardPage({
  data,
  drives,
  onOpenEntity,
  onGoto,
  onRefresh,
  elevated,
}: {
  data: LpData;
  drives: DriveInfo[];
  onOpenEntity: (id: string) => void;
  onGoto: (v: 'software' | 'disk' | 'migration' | 'dashboard') => void;
  onRefresh: () => void;
  /** 当前进程是否提权。扫描结果里"为什么有目录读不到"的说法取决于它 */
  elevated: boolean;
}) {
  const scan = useScan(onRefresh);
  const s = data.summary;
  // 快照时间戳后端给的是 UTC ISO，按本地时区显示
  const scannedAt = data.snapshot?.scanned_at
    ? new Date(data.snapshot.scanned_at).toLocaleString()
    : null;
  const offenders: SoftwareEntity[] = [...data.software]
    .filter((x) => (x.traces?.length ?? 0) > 0)
    .sort((a, b) => (b.traces_size ?? 0) - (a.traces_size ?? 0))
    .slice(0, 6);

  const cDrive = drives.find((d) => d.letter === 'C:');
  const cSegments = cDrive
    ? [
        { label: '其他已用', color: 'var(--seg-other)', size: cDrive.total - cDrive.free - s.total_size },
        { label: '软件痕迹（已归因）', color: 'var(--seg-known)', size: s.total_size - s.unknown_size },
        { label: '未归因痕迹', color: 'var(--seg-unknown)', size: s.unknown_size },
      ]
    : [];

  return (
    <div className="lp-page" style={{ padding: '24px 28px' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 20,
          padding: '16px 20px',
          borderRadius: 'var(--radius-lg)',
          background: 'linear-gradient(135deg, var(--panel) 0%, var(--panel2) 100%)',
          border: '1px solid var(--line)',
          boxShadow: 'var(--shadow-card), var(--inset-highlight)',
        }}
      >
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 700, color: 'var(--tx)', margin: 0, letterSpacing: -0.5 }}>
            系统总览
          </h1>
          {/* 这里原先挂了一个写死的"实时就绪"徽标。删掉的理由：它无条件显示，
              既不反映引擎连通状态也不反映快照新鲜度——而它右边紧挨着的就是
              "上次扫描 X"，两者可以同时显示"就绪"和一个几天前的时间。
              在一个会删文件的工具里，让状态指示器说谎比没有状态指示器更糟。 */}
          <div style={{ color: 'var(--tx2)', fontSize: 'var(--fs-sm)', marginTop: 4 }}>
            台账实时 · C 盘足迹来自本机扫描快照
            {scannedAt && ` · 上次扫描 ${scannedAt}`}
          </div>
        </div>
        <ScanButtons scan={scan} />
      </div>

      <ScanPanels scan={scan} elevated={elevated} />

      {/* 旧快照的体积没排除硬链接。**必须显式说**：不然界面会拿虚高数倍的数字算"能腾出
          多少"，而用户是照着那个数按下执行的——uv 那次就是这样，说能腾 1.63 GB，实际
          只有 0.31 GB。放在扫描面板下方，紧邻"重新扫描"按钮，因为解决办法就是重扫。 */}
      {data.snapshot?.sizes_inflated && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 14 }}
          message="这份快照的体积偏大，重新扫描一次会更准"
          description={
            data.snapshot.sizes_reason
            ?? '旧版本扫的快照没有排除硬链接，uv / pnpm 这类共用内容的缓存会被高报数倍。'
          }
        />
      )}

      <div style={{ display: 'flex', gap: 14, marginBottom: 14 }}>
        <Card size="small" className="lp-card-elevated" style={{ flex: 1 }}>
          <div style={{ fontSize: 'var(--fs-md)', fontWeight: 600, color: 'var(--tx)', marginBottom: 12 }}>
            扫描摘要
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div
              style={{
                padding: '10px 12px',
                borderRadius: 'var(--radius-md)',
                background: 'var(--panel2)',
                border: '1px solid var(--line2)',
              }}
            >
              {/* 26px 落在"大文本"门槛（24px 加粗）之上，但底是 --panel2，
                  浅色下 --blue2 在这个底上只有 2.90，连 3.0 都不到。用 --accent-fg。 */}
              <div className="lp-num" style={{ fontSize: 26, fontWeight: 700, color: 'var(--accent-fg)', lineHeight: 1.15 }}>
                {s.entities}
              </div>
              <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--tx2)', marginTop: 4 }}>
                软件实体
              </div>
              {/* 用 --tx2 而非 --tx3：这两个内层小盒的底是 --panel2，浅色下 --tx3 压在
                  它上面实测 4.4999，差 0.0001 不过线（调色板标注的 4.50 是四舍五入
                  后的样子，看着像过了）。10.5px 的字更不该踩这条线。 */}
              <div style={{ fontSize: 10.5, color: 'var(--tx2)', marginTop: 2 }}>
                {s.registry_raw} 条注册表聚合
              </div>
            </div>
            <div
              style={{
                padding: '10px 12px',
                borderRadius: 'var(--radius-md)',
                background: 'var(--panel2)',
                border: '1px solid var(--line2)',
              }}
            >
              <div className="lp-num" style={{ fontSize: 26, fontWeight: 700, color: 'var(--tx)', lineHeight: 1.15 }}>
                {s.located}
              </div>
              <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--tx2)', marginTop: 4 }}>
                本体已定位
              </div>
              <div style={{ fontSize: 10.5, color: 'var(--tx2)', marginTop: 2 }}>
                覆盖率 {s.entities > 0 ? Math.round((s.located / s.entities) * 100) : 0}%
              </div>
            </div>
          </div>
          <Button
            type="primary"
            ghost
            size="middle"
            icon={<RocketOutlined />}
            style={{ marginTop: 14, width: '100%', borderRadius: 'var(--radius-md)' }}
            onClick={() => onGoto('software')}
          >
            进入软件台账
          </Button>
        </Card>

        <Card size="small" className="lp-card-elevated" style={{ flex: 1.6 }}>
          <div style={{ fontSize: 'var(--fs-md)', fontWeight: 600, color: 'var(--tx)', marginBottom: 12 }}>
            存储拓扑
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {drives.map((d) =>
              d.letter === 'C:' && cDrive ? (
                <SegmentedDriveBar key={d.letter} drive={d} segments={cSegments} />
              ) : (
                <SegmentedDriveBar
                  key={d.letter}
                  drive={d}
                  segments={[{ label: '已用', color: 'var(--seg-other)', size: d.total - d.free }]}
                />
              ),
            )}
            {drives.length === 0 && <Alert type="info" showIcon={false} message="磁盘信息加载中…" />}
          </div>
        </Card>
      </div>

      <Card size="small" className="lp-card-elevated">
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 12 }}>
          <div>
            <span style={{ fontSize: 'var(--fs-md)', fontWeight: 600, color: 'var(--tx)' }}>
              占用大户 · C 盘痕迹 Top 6
            </span>
            <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--tx3)', marginLeft: 8 }}>
              优先清理或迁移这些应用可最快释放 C 盘
            </span>
          </div>
          <Button type="link" size="small" style={{ marginLeft: 'auto' }} onClick={() => onGoto('software')}>
            查看全部 →
          </Button>
        </div>
        {offenders.map((e) => {
          const maxTraceSize = offenders[0]?.traces_size ?? 1;
          const ratio = Math.min(100, Math.max(8, Math.round(((e.traces_size ?? 0) / maxTraceSize) * 100)));
          return (
            // button 而非 div：见 SoftwarePage 台账行同处改动。这 6 行是仪表盘上
            // 唯一的深入入口，键盘到不了就等于首页没有出路。
            <button
              key={e.id}
              type="button"
              onClick={() => onOpenEntity(e.id)}
              className="lp-offender-item"
            >
              <AppTile e={e} size={36} />
              <div style={{ flex: 1, minWidth: 0, zIndex: 1 }}>
                <div style={{ fontSize: 'var(--fs-md)', fontWeight: 600, color: 'var(--tx)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {e.name}
                </div>
                <code className="lp-mono" style={{ fontSize: 'var(--fs-xs)', color: 'var(--tx3)', display: 'block', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', marginTop: 2 }}>
                  {e.location ?? '本体未定位'}
                </code>
              </div>

              {/* 视觉占比微进度条（让占用大小一目了然） */}
              <div
                style={{
                  position: 'absolute',
                  right: 0,
                  top: 0,
                  bottom: 0,
                  width: `${ratio * 0.45}%`,
                  background:
                    'linear-gradient(90deg, transparent, color-mix(in srgb, var(--red) 8%, transparent))',
                  pointerEvents: 'none',
                  borderTopRightRadius: 'var(--radius-md)',
                  borderBottomRightRadius: 'var(--radius-md)',
                }}
              />

              <div style={{ textAlign: 'right', flexShrink: 0, zIndex: 1 }}>
                <div className="lp-num" style={{ color: 'var(--red)', fontWeight: 700, fontSize: 'var(--fs-title)' }}>
                  {fmtSize(e.traces_size)}
                </div>
                <div style={{ fontSize: 'var(--fs-xs)', color: 'var(--tx3)', marginTop: 2 }}>{e.traces?.length ?? 0} 处痕迹</div>
              </div>
            </button>
          );
        })}
      </Card>
    </div>
  );
}
