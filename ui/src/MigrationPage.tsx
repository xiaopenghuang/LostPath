import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, App, Button, Card, Empty, Input, Modal, Select, Spin, Statistic, Tag, Tooltip,
} from 'antd';
import { WarningOutlined } from '@ant-design/icons';
import {
  ACTION_LABEL, BLOCKER_LABEL, checkTargetRoot, DriveInfo, executePlan, fetchDrives,
  fetchOperations, fetchPlan, fetchTargetRoot, fetchTargetRootOverrides, fmtSize,
  Operation, OperationsReport, OP_STATUS_LABEL, OverrideEntry, Plan, PlanReport,
  rollbackOperation, saveTargetRoot, setTargetRootOverride,
  TargetRootCheck, TargetRootInfo,
} from './api';

const ACTION_COLOR: Record<string, string> = {
  redirect: 'cyan',
  cleanup: 'green',
  junction: 'blue',
  none: 'default',
};

/** 取路径末段。用 split 而非 basename：浏览器里没有 path 模块。 */
const baseName = (p: string) => p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p;

/** 被拦下的候选按原因归类。同类原因动辄几十条，逐条列出会把真正能做的事挤出屏幕。 */
function BlockedSummary({ report }: { report: PlanReport }) {
  const counts = report.summary.blocker_counts;
  const entries = Object.entries(counts);
  if (!entries.length) return null;
  return (
    <Card size="small" title={`未纳入 · ${report.summary.blocked} 个目录`} style={{ marginTop: 14 }}>
      <div style={{ fontSize: 12, color: 'var(--tx3)', marginBottom: 10 }}>
        这些目录本工具不动，逐条给出原因。宁可少做一件，不能做错一件。
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {entries.map(([code, n]) => (
          <Tooltip key={code} title={code}>
            <Tag bordered={false} style={{ fontSize: 12, padding: '3px 9px' }}>
              {BLOCKER_LABEL[code] ?? code} · {n}
            </Tag>
          </Tooltip>
        ))}
      </div>
    </Card>
  );
}

/** 操作历史。用户要能随时看见"我做过什么、还能不能撤"。 */
function OperationHistory({
  report,
  onRollback,
}: {
  report: OperationsReport;
  onRollback: (op: Operation) => void;
}) {
  if (!report.operations.length) return null;
  return (
    <Card
      size="small"
      style={{ marginTop: 14 }}
      title={`操作历史 · ${report.summary.total} 条`}
      extra={
        report.summary.recycle_bytes > 0 ? (
          <span style={{ fontSize: 12, color: 'var(--tx3)' }}>
            回收区占用 {fmtSize(report.summary.recycle_bytes)}（回收期内不会真删）
          </span>
        ) : null
      }
    >
      {report.operations.slice(0, 12).map((op) => {
        const st = OP_STATUS_LABEL[op.status] ?? { text: op.status, color: 'default' };
        // 数据已永久删除的不给撤销按钮：点了只会被后端拒绝，白给一次失败
        const canRollback =
          (op.status === 'done' || op.status === 'failed') && !op.purged_at;
        return (
          <div
            key={op.id}
            style={{
              display: 'flex', alignItems: 'center', gap: 10, padding: '9px 11px',
              border: '1px solid var(--line)', borderRadius: 8, marginBottom: 6,
              background: 'var(--bg)',
            }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 12.5, color: 'var(--tx)' }}>
                {ACTION_LABEL[op.action as keyof typeof ACTION_LABEL] ?? op.action}
                {op.size ? ` · ${fmtSize(op.size)}` : ''}
                {op.env_var ? ` · ${op.env_var}` : ''}
              </div>
              <code
                className="lp-mono"
                style={{
                  fontSize: 11, color: 'var(--tx3)', display: 'block',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}
              >
                {op.source_path}
              </code>
              {op.failure && (
                <div style={{ fontSize: 11, color: 'var(--red)' }}>{op.failure}</div>
              )}
            </div>
            <Tag color={op.purged_at ? 'default' : st.color} bordered={false}>
              {op.purged_at ? '已永久删除' : st.text}
            </Tag>
            {canRollback && (
              <Button size="small" onClick={() => onRollback(op)}>撤销</Button>
            )}
          </div>
        );
      })}
    </Card>
  );
}

function PlanDetail({
  plan,
  onExecute,
  busy,
  onChangeTarget,
  overriddenRoot,
}: {
  plan: Plan;
  onExecute: (p: Plan) => void;
  busy: boolean;
  onChangeTarget?: (p: Plan) => void;
  /** 这一项已单独设过的根；没设过为 null。只用于把按钮文案说准。 */
  overriddenRoot?: string | null;
}) {
  const mech = plan.redirect_mechanism;
  return (
    // lp-sticky-detail：左列有 27 项时高约 1840px，这两块只有约 430px，
    // 不跟随的话"滚下去选、滚回来执行"每次要来回近两屏。见 index.css 同名注释。
    <div className="lp-split lp-sticky-detail" style={{ flex: 1, minWidth: 0 }}>
      <Card size="small" style={{ flex: 1.25, minWidth: 0 }} title="判定依据">
        <div style={{ fontSize: 11, color: 'var(--tx3)', marginBottom: 4 }}>目录</div>
        <code
          className="lp-mono"
          style={{
            display: 'block', background: 'var(--bg)', border: '1px solid var(--line)',
            borderRadius: 6, padding: '8px 10px', fontSize: 12, color: 'var(--cyan)',
            wordBreak: 'break-all',
          }}
        >
          {plan.path}
        </code>

        {/* 子目录级计划必须说清"父目录留着不动"，否则用户会担心整个软件的数据被删 */}
        {plan.parent_path && (
          <Alert
            type="info"
            showIcon={false}
            style={{ marginTop: 8 }}
            message={
              <span style={{ fontSize: 11.5 }}>
                只处理这一个子目录。父目录{' '}
                <code className="lp-mono" style={{ fontSize: 11 }}>
                  {plan.parent_path}
                </code>{' '}
                整块不可动，其余子目录保持原样。
              </span>
            }
          />
        )}

        {plan.action === 'redirect' && plan.target && (
          <>
            <div style={{ textAlign: 'center', color: 'var(--tx3)', margin: '6px 0' }}>↓</div>
            <div style={{ fontSize: 11, color: 'var(--tx3)', marginBottom: 4 }}>
              新位置（改 <code className="lp-mono">{plan.env_var}</code> 指向此处）
            </div>
            <code
              className="lp-mono"
              style={{
                display: 'block', background: 'var(--bg)', border: '1px solid var(--line)',
                borderRadius: 6, padding: '8px 10px', fontSize: 12, color: 'var(--cyan)',
                wordBreak: 'break-all',
              }}
            >
              {plan.target}
            </code>
          </>
        )}

        <div style={{ display: 'flex', gap: 26, marginTop: 14 }}>
          <Statistic
            title={<span style={{ fontSize: 11, color: 'var(--tx3)' }}>可腾出</span>}
            value={fmtSize(plan.reclaimable)}
            valueStyle={{ fontSize: 22, fontWeight: 700, color: 'var(--green)' }}
          />
          <Statistic
            title={<span style={{ fontSize: 11, color: 'var(--tx3)' }}>归因置信度</span>}
            value={`${Math.round(plan.confidence * 100)}%`}
            valueStyle={{ fontSize: 22, fontWeight: 700, color: 'var(--blue2)' }}
          />
          <div>
            <div style={{ fontSize: 11, color: 'var(--tx3)' }}>所属</div>
            <div style={{ fontSize: 13, color: 'var(--tx)', marginTop: 8 }}>
              {plan.owner ?? '未归因'}
            </div>
          </div>
        </div>

        {mech && (
          <div style={{ fontSize: 11.5, color: 'var(--tx2)', marginTop: 12 }}>
            {mech.note}
          </div>
        )}
        {plan.notes.length > 0 && (
          <ul style={{ margin: '10px 0 0', paddingLeft: 18, fontSize: 12, color: 'var(--tx2)' }}>
            {plan.notes.map((n, i) => (
              <li key={i} style={{ marginBottom: 4 }}>{n}</li>
            ))}
          </ul>
        )}
      </Card>

      <Card size="small" style={{ flex: 1, minWidth: 0 }} title="执行计划">
        {plan.steps.map((s) => (
          <div key={s.n} style={{ display: 'flex', gap: 10, marginBottom: 12 }}>
            <div
              style={{
                width: 22, height: 22, borderRadius: '50%', border: '1px solid var(--line2)',
                display: 'grid', placeItems: 'center', fontSize: 11,
                color: 'var(--tx2)', flexShrink: 0,
              }}
            >
              {s.n}
            </div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 12.5, color: 'var(--tx)' }}>{s.title}</div>
              <div style={{ fontSize: 11.5, color: 'var(--tx3)', wordBreak: 'break-all' }}>
                {s.detail}
              </div>
              {s.reversible && s.reversible !== '—' && (
                <div style={{ fontSize: 11, color: 'var(--green)', marginTop: 2 }}>
                  可撤销：{s.reversible}
                </div>
              )}
            </div>
          </div>
        ))}
        <Button type="primary" block loading={busy} onClick={() => onExecute(plan)}>
          执行此计划
        </Button>
        {/* 只对会产生目标位置的动作给这个入口。cleanup 是直接删（进回收区），
            没有"搬到哪"可言，给了按钮只会让人以为它也能改位置。 */}
        {onChangeTarget && (plan.action === 'redirect' || plan.action === 'junction') && (
          <Button
            block
            size="small"
            style={{ marginTop: 8 }}
            onClick={() => onChangeTarget(plan)}
          >
            {overriddenRoot ? '改目标位置（已单独设置）' : '只改这一项的目标位置'}
          </Button>
        )}
        <div style={{ fontSize: 11, color: 'var(--tx3)', marginTop: 8, textAlign: 'center' }}>
          点击后会先让你确认，删除的数据进回收区、30 天内可撤销
        </div>
      </Card>
    </div>
  );
}

const FIELD_LABEL: React.CSSProperties = {
  display: 'block', fontSize: 11.5, color: 'var(--tx3)', marginBottom: 5,
};

/** 一条校验意见。红=不能用，琥珀=能用但可能不是你想要的。 */
function Issue({ tone, text }: { tone: 'error' | 'warn'; text: string }) {
  return (
    <div
      // 错误要让屏幕阅读器读出来：这个框里没有别的地方会告诉用户为什么保存键是灰的
      role={tone === 'error' ? 'alert' : undefined}
      style={{
        display: 'flex', gap: 7, marginTop: 8, fontSize: 12, lineHeight: 1.55,
        // --red / --amber 都是实测过对比度的 token（见 index.css）。**不用 antd 的
        // 预设色**：仓库里已经记过一次 Tag color="orange" 浅色实测只有 3.34:1。
        color: tone === 'error' ? 'var(--red)' : 'var(--amber)',
      }}
    >
      <span aria-hidden style={{ flexShrink: 0, fontWeight: 700 }}>
        {tone === 'error' ? '✕' : '!'}
      </span>
      <span>{text}</span>
    </div>
  );
}

/**
 * 改迁移目标位置。
 *
 * **为什么给盘符下拉而不只给一个输入框**：手打最容易漏掉盘符后的反斜杠，而 `E:` 与
 * `E:\` 在 Windows 上是两回事——前者是"E 盘的当前目录"，实测 join("E:", "x") 得到
 * "E:x"，落点取决于进程工作目录。下拉直接给出合法形状，输入框留给要指定具体子目录的人。
 *
 * **为什么每次输入都问一次后端**：能不能用取决于驱动器类型和写权限，前端判断不了。
 * 而且规则必须落在服务端才拦得住直接打 HTTP 的调用方（见 engine/main.py 的
 * _validated_target_root），前端再实现一遍只会让两份规则各自漂移。
 */
/**
 * 逐项覆盖时要知道改的是哪一条、以及它当前有没有专属位置。
 *
 * `saved` 传的是这一条已存的根（没设过就是 null），用来决定"改回全局位置"那个
 * 按钮能不能按 —— 判据与全局模式一致：看**有没有存过东西**，不看当前是否走全局。
 */
type OverrideScope = { source: string; label: string; saved: string | null };

function TargetRootModal({
  info, onClose, onSaved, scope,
}: {
  info: TargetRootInfo;
  onClose: () => void;
  onSaved: () => void;
  /** 传了就是"只改这一条"，不传是改全局。两种模式共用同一套校验与输入控件。 */
  scope?: OverrideScope;
}) {
  const { message, modal } = App.useApp();
  // 逐项模式下先显示这一条已存的根，没设过就拿全局的当起点 —— 用户多数是想在
  // 全局位置的基础上挪一挪，而不是从空白开始打字。
  const [text, setText] = useState(
    scope ? (scope.saved ?? info.effective) : (info.saved ?? info.effective),
  );
  const [drives, setDrives] = useState<DriveInfo[]>([]);
  const [check, setCheck] = useState<TargetRootCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetchDrives().then(setDrives).catch(() => undefined);
  }, []);

  // 输入时问后端校验。debounce 400ms 而不是每次 onChange 就发：校验里含一次**真实的
  // 写入探测**（在目标最近的已存在祖先里建再删一个临时文件），不该跟着按键频率跑。
  useEffect(() => {
    const v = text.trim();
    if (!v) {
      setCheck(null);
      setChecking(false);
      return;
    }
    let alive = true;
    setChecking(true);
    const timer = setTimeout(() => {
      checkTargetRoot(v)
        .then((r) => { if (alive) { setCheck(r); setChecking(false); } })
        .catch(() => { if (alive) setChecking(false); });
    }, 400);
    // alive 标志防的是"请求还在飞、用户已经关掉弹窗"：那时 setState 会警告，
    // 更麻烦的是旧请求的结果会盖掉新输入的校验结论。
    return () => { alive = false; clearTimeout(timer); };
  }, [text]);

  const doSave = async (path: string | null) => {
    setSaving(true);
    try {
      if (scope) {
        const res = await setTargetRootOverride(scope.source, path);
        if (res.ok) {
          // 提示里报**后端算出的完整目标**，不是用户填的根。界面自己拼镜像后缀
          // 就等于把 planner 的规则复制过来，两份实现必然漂移，症状是"提示里说搬到
          // 这儿、实际搬到别处"而两边都看起来对。
          message.success(path
            ? `这一项将搬到 ${res.target ?? res.normalized}`
            : '已改回全局位置');
          onSaved();
        } else {
          message.error(res.errors?.[0]?.message ?? res.error ?? '保存失败');
        }
        return;
      }
      const res = await saveTargetRoot(path);
      if (res.ok) {
        message.success(path ? `目标位置已改为 ${res.normalized}` : '已恢复为自动选择');
        onSaved();
      } else {
        message.error(res.errors[0]?.message ?? '保存失败');
      }
    } finally {
      setSaving(false);
    }
  };

  const sysWarn = check?.warnings.find((w) => w.code === 'same_as_system_drive');

  const submit = () => {
    // 系统盘要二次确认：技术上完全能用，但用户来这一页就是为了腾系统盘的空间，
    // 默默照做等于让他白忙一场。**这是"警告"而不是"拒绝"的意义所在**——决定权在他。
    if (sysWarn) {
      modal.confirm({
        title: '这个位置腾不出空间，确定要用？',
        okText: '我知道，就用它',
        cancelText: '换一个',
        content: <div style={{ fontSize: 13 }}>{sysWarn.message}</div>,
        onOk: () => doSave(text.trim()),
      });
      return;
    }
    doSave(text.trim());
  };

  return (
    <Modal
      open
      title={scope ? `只改这一项：${scope.label}` : '迁移目标位置'}
      onCancel={onClose}
      width={580}
      footer={[
        <Button
          key="auto"
          onClick={() => doSave(null)}
          loading={saving}
          // 判据是"有没有存过东西"，不是"当前是否走自动"。存了一个**失效**的值时
          // （盘拔了），后端的 source 会是 auto——用 source 判就把这个按钮禁掉了，
          // 而那时用户恰恰最需要它：不清掉那个坏值，那条黄色警告会一直挂着，
          // 他只能改成另一个合法路径，没法回到"就用自动挑的"。
          disabled={scope ? !scope.saved : !info.saved}
        >
          {scope ? '改回全局位置' : '恢复自动选择'}
        </Button>,
        <Button key="cancel" onClick={onClose}>取消</Button>,
        <Button
          key="ok"
          type="primary"
          loading={saving}
          // 校验没过 / 还在核对时不给按。禁用理由已经用 role="alert" 说在下面了，
          // 不是让用户对着一个灰按钮猜。
          disabled={checking || !check?.ok}
          onClick={submit}
        >
          保存并重新核算
        </Button>,
      ]}
    >
      <div style={{ fontSize: 12.5, color: 'var(--tx2)', lineHeight: 1.6, marginBottom: 16 }}>
        {scope ? (
          <>
            只有这一项会用下面这个位置，其余项仍按全局设置。填的是<b>根目录</b>——
            原来的目录层级会照原样接在它后面，所以新位置一眼能看出东西本来在哪。
            <div style={{ marginTop: 6, color: 'var(--tx3)', fontSize: 11.5 }}>
              例：根填 <code className="lp-mono">G:\1</code>，而这一项在{' '}
              <code className="lp-mono">C:\Users\你\AppData\Local\某目录</code>，
              就会搬到 <code className="lp-mono">G:\1\AppData\Local\某目录</code>。
            </div>
          </>
        ) : (
          <>
            重定向类操作会把环境变量指到这里，搬迁类操作会把数据复制到这里。原来的
            目录层级会照原样接在它后面，各软件不会混在一起。
          </>
        )}
      </div>

      <div style={{ marginBottom: 14 }}>
        <label htmlFor="lp-target-drive" style={FIELD_LABEL}>
          选个盘（会填到下面的路径里）
        </label>
        <Select
          id="lp-target-drive"
          style={{ width: '100%' }}
          placeholder="从本机固定磁盘里挑一个"
          // 刻意不受控（选完回到 placeholder）：真值只有下面那个路径框一个。用户手改
          // 过路径之后，下拉里显示的盘符很可能已经和路径不是一回事，那种"两个控件各说
          // 一套"比看不见自己刚选了什么更糟。
          value={undefined}
          onChange={(letter: string) => setText(`${letter}\\LostPathStore`)}
          options={drives.map((d) => ({
            value: d.letter,
            label: `${d.letter}   剩余 ${fmtSize(d.free)} / 共 ${fmtSize(d.total)}`
              // 标出系统盘：不标的话用户看见"C: 剩余 20 GB"很可能直接选它，
              // 要等到按保存才被警告，而那时选择已经做完了。
              + (d.letter.toUpperCase() === (info.system_drive || '').toUpperCase()
                ? '   ·   系统盘，腾不出空间' : ''),
          }))}
        />
      </div>

      <div>
        <label htmlFor="lp-target-path" style={FIELD_LABEL}>完整路径</label>
        <Input
          id="lp-target-path"
          className="lp-mono"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onPressEnter={() => { if (!checking && check?.ok) submit(); }}
          placeholder="例如 E:\LostPathStore"
          status={check && !check.ok ? 'error' : undefined}
        />
      </div>

      {checking && (
        <div style={{ fontSize: 12, color: 'var(--tx3)', marginTop: 8 }}>正在核对…</div>
      )}
      {!checking && check?.errors.map((e) => (
        <Issue key={e.code} tone="error" text={e.message} />
      ))}
      {!checking && check?.warnings.map((w) => (
        <Issue key={w.code} tone="warn" text={w.message} />
      ))}

      {!checking && check?.ok && check.normalized && (
        <div
          style={{
            marginTop: 12, padding: '9px 11px', background: 'var(--bg)',
            border: '1px solid var(--line)', borderRadius: 6,
          }}
        >
          <div style={{ fontSize: 11, color: 'var(--tx3)', marginBottom: 3 }}>
            各项会落在
          </div>
          {/* 不再写 `<根>\<软件名>`：目标叶子早已改成**按源路径镜像**，写软件名是
              在骗用户。而且这里刻意只给形状不给具体路径——真实路径由后端算
              （planner.mirror_suffix），前端拼一遍必然与后端漂移。 */}
          <code className="lp-mono" style={{ fontSize: 11.5, color: 'var(--cyan)',
            wordBreak: 'break-all' }}>
            {check.normalized}\&lt;原来的目录层级&gt;
          </code>
          <div style={{ fontSize: 11, color: 'var(--tx3)', marginTop: 5 }}>
            例：<code className="lp-mono">…\AppData\Local\某目录</code> 会落到{' '}
            <code className="lp-mono">{check.normalized}\AppData\Local\某目录</code>
          </div>
        </div>
      )}

      <div style={{ fontSize: 11.5, color: 'var(--tx3)', marginTop: 16 }}>
        当前生效：<code className="lp-mono">{info.effective}</code>
        {info.source === 'custom' ? '（你指定的）' : '（自动挑的）'}
      </div>
    </Modal>
  );
}

export default function MigrationPage({ onRefresh }: { onRefresh?: () => void }) {
  // 见 theme.tsx：静态 message/Modal 读全局主题，跟不上 ConfigProvider 切换。
  // 这一页的确认弹窗描述的是要对磁盘做什么，样式错了比别处更不该。
  const { message, modal } = App.useApp();
  const [report, setReport] = useState<PlanReport | null>(null);
  const [ops, setOps] = useState<OperationsReport | null>(null);
  const [err, setErr] = useState('');
  const [sel, setSel] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [targetInfo, setTargetInfo] = useState<TargetRootInfo | null>(null);
  const [targetOpen, setTargetOpen] = useState(false);
  const [overrides, setOverrides] = useState<OverrideEntry[]>([]);
  // 非 null 时弹窗处于"只改这一项"模式
  const [scope, setScope] = useState<OverrideScope | null>(null);

  const reload = useCallback(() => {
    fetchPlan()
      .then(setReport)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
    fetchOperations().then(setOps).catch(() => undefined);
    // 目标位置单独取而不是从 plan 里读：plan 只给出**这份计划用了哪个根**，
    // 说不出它是自动挑的还是用户设的、也说不出用户设的那个是否已经失效。
    fetchTargetRoot().then(setTargetInfo).catch(() => undefined);
    fetchTargetRootOverrides().then(setOverrides).catch(() => undefined);
  }, []);

  /** 这个源路径有没有单独设过根。键在后端存的是小写，这里比较也要转。 */
  const overrideOf = useCallback(
    (path: string) =>
      overrides.find((o) => o.source === path.toLowerCase()) ?? null,
    [overrides],
  );

  useEffect(reload, [reload]);

  /** 执行前二次确认。把"将发生什么"和"怎么撤"讲清楚，用户才有能力判断。 */
  const confirmExecute = (plan: Plan) => {
    modal.confirm({
      title: `确认执行：${ACTION_LABEL[plan.action]}`,
      width: 560,
      okText: '确认执行',
      cancelText: '取消',
      content: (
        <div style={{ fontSize: 13 }}>
          <p style={{ marginTop: 8 }}>将对下面这个目录动手：</p>
          <code className="lp-mono" style={{ fontSize: 11.5, wordBreak: 'break-all' }}>
            {plan.path}
          </code>
          <ul style={{ paddingLeft: 18, marginTop: 10 }}>
            {plan.steps.map((s) => (
              <li key={s.n} style={{ marginBottom: 3 }}>{s.title}：{s.detail}</li>
            ))}
          </ul>
          <p style={{ color: 'var(--tx2)' }}>
            数据不会被直接删除，而是移入回收区，<b>30 天内都可以撤销</b>。
            {plan.env_var && ' 环境变量的原值也会记下来，撤销时一并还原。'}
          </p>
          {plan.action === 'redirect' && (
            <p style={{ color: 'var(--tx2)' }}>
              注意：环境变量只对新启动的程序生效，已经开着的相关程序需要重启。
            </p>
          )}
          {plan.action === 'junction' && (
            <>
              <p style={{ color: 'var(--tx2)' }}>
                这是<b>搬迁而非删除</b>：数据一个字节都不会少，只是换到{' '}
                <code className="lp-mono" style={{ fontSize: 11.5 }}>{plan.target}</code>
                ，原位留一个链接，软件仍按老路径访问。
              </p>
              <p style={{ color: 'var(--red)' }}>
                请先关掉「{plan.owner ?? '该软件'}」再执行。跨盘复制
                {plan.size >= 2 * 1024 ** 3 ? '这么大的目录会花几分钟' : '需要一点时间'}
                ，期间该软件正在写文件会导致复制到一半的数据不一致。
              </p>
              <p style={{ color: 'var(--tx2)' }}>
                少数软件不认这种链接（尤其带自校验或反作弊的）。真出问题就来这里回滚，
                原始数据整份放回原位。
              </p>
            </>
          )}
        </div>
      ),
      onOk: async () => {
        setBusy(true);
        try {
          const res = await executePlan(plan.path, false, report?.target_root ?? undefined);
          if (res.ok) {
            // 报实测值而不是计划里的 reclaimable：后者逐文件累加，硬链接会被重复计数
            // （实测 uv 缓存虚高 5 倍）。数据现在还在回收区，空间要清空它才真正释放，
            // 所以措辞是"可释放"而非"已腾出"。
            const f = res.op?.freed;
            const real = f ? fmtSize(f.freeable) : fmtSize(plan.reclaimable);
            const gap = f && f.logical > f.freeable * 1.1
              ? `（目录逻辑体积 ${fmtSize(f.logical)}，其中 ${f.linked_files} 个文件是硬链接，
                 与别处共用同一份内容，故实际可释放较少）`.replace(/\s+/g, '')
              : '';
            message.success(
              `已移入回收区，清空后可释放 ${real}${gap}；可在操作历史里撤销`);
            reload();
            onRefresh?.();
          } else {
            message.error(res.refused ?? res.error ?? '执行失败');
            reload();
          }
        } finally {
          setBusy(false);
        }
      },
    });
  };

  const confirmRollback = (op: Operation) => {
    modal.confirm({
      title: '撤销这次操作？',
      okText: '撤销',
      cancelText: '取消',
      content: (
        <div style={{ fontSize: 13 }}>
          <p>会把数据从回收区移回原位置
            {op.env_var && `，并把 ${op.env_var} 还原成操作前的值`}。</p>
          <code className="lp-mono" style={{ fontSize: 11.5, wordBreak: 'break-all' }}>
            {op.source_path}
          </code>
        </div>
      ),
      onOk: async () => {
        const res = await rollbackOperation(op.id);
        if (res.ok) {
          message.success('已撤销，数据回到原位置');
          reload();
          onRefresh?.();
        } else {
          message.error(res.refused ?? '撤销失败');
        }
      },
    });
  };

  const actionable = useMemo(
    () => (report?.plans ?? []).filter((p) => p.executable)
      .sort((a, b) => b.reclaimable - a.reclaimable),
    [report],
  );
  const current = actionable.find((p) => p.path === sel) ?? actionable[0];

  if (err) {
    return (
      <div style={{ padding: '22px 26px' }}>
        <Alert type="error" showIcon message="计划加载失败" description={err} />
      </div>
    );
  }
  if (!report) {
    // tip 写在 Spin 上不渲染，见 RecyclePage 同处注释
    return (
      <div style={{ padding: 60, textAlign: 'center' }} role="status" aria-busy="true">
        <Spin />
        <div style={{ marginTop: 12, fontSize: 'var(--fs-md)', color: 'var(--tx2)' }}>
          正在核算计划…（要对每个候选查磁盘实况，比读快照慢）
        </div>
      </div>
    );
  }
  if (report.hint) {
    return (
      <div style={{ padding: '22px 26px' }}>
        <Card className="lp-page">
          <Empty description={report.hint} />
        </Card>
      </div>
    );
  }

  const s = report.summary;

  return (
    <div className="lp-page" style={{ padding: '22px 26px' }}>
      <Alert
        type="info"
        showIcon
        icon={<WarningOutlined />}
        style={{ marginBottom: 14 }}
        message="所有操作执行前均需确认，删除的数据 30 天内可撤销"
        description={
          <span style={{ fontSize: 12.5 }}>
            所有判断都基于本机实扫结果，每条都附判定依据。数据不会被直接删除，而是先移入
            回收区并写下回滚记录；改环境变量时原值也会记下来。左侧未纳入的目录逐条给了
            不处理的原因。
          </span>
        }
      />

      <Card size="small" style={{ marginBottom: 14 }}>
        <div style={{ display: 'flex', gap: 44, flexWrap: 'wrap' }}>
          <Statistic
            title={<span style={{ fontSize: 11.5, color: 'var(--tx3)' }}>可腾出 C 盘空间</span>}
            value={fmtSize(s.reclaimable)}
            valueStyle={{ fontSize: 27, fontWeight: 700, color: 'var(--green)' }}
          />
          <Statistic
            title={<span style={{ fontSize: 11.5, color: 'var(--tx3)' }}>可执行条目</span>}
            value={s.executable}
            suffix={<span style={{ fontSize: 13, color: 'var(--tx3)' }}>/ {s.total_candidates} 处痕迹</span>}
            valueStyle={{ fontSize: 27, fontWeight: 700, color: 'var(--blue2)' }}
          />
          {Object.entries(s.by_action).map(([act, v]) => (
            <div key={act}>
              <div style={{ fontSize: 11.5, color: 'var(--tx3)' }}>
                {ACTION_LABEL[act as keyof typeof ACTION_LABEL] ?? act}
              </div>
              <div style={{ fontSize: 20, fontWeight: 600, color: 'var(--tx)', marginTop: 6 }}>
                {v.count} 项 · {fmtSize(v.reclaimable)}
              </div>
            </div>
          ))}
        </div>
        {report.target_root && (
          <div style={{ fontSize: 11.5, color: 'var(--tx3)', marginTop: 12,
            display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}>
            <span>目标位置</span>
            <code className="lp-mono">{report.target_root}</code>
            <span>
              {targetInfo?.source === 'custom'
                ? '（你指定的）'
                : '（自动挑了非系统盘里可用空间最大的一个）'}
            </span>
            {targetInfo && (
              <Button
                size="small"
                // 显式清 scope：这个入口改的是全局，不能带着上一次逐项的 scope 进去
                onClick={() => { setScope(null); setTargetOpen(true); }}
                style={{ fontSize: 11.5, height: 22, padding: '0 9px' }}
              >
                更改
              </Button>
            )}
          </div>
        )}
        {/* 设过但现在用不了：盘拔了、盘符变了、目录被别的程序删了。**必须显式说出来**
            ——后端此时已静默回落到自动挑的盘（出计划是只读操作，那里弹错误没有用户
            能采取的动作），不说的话用户会以为数据仍会去他设的位置。 */}
        {targetInfo?.saved_invalid && (
          <Alert
            type="warning"
            showIcon
            // 图标颜色显式给 --amber。**antd 预设的 warning 图标色实测只有 1.83:1**
            // （#faad14 对 #fffbe6 底），非文本元素要求 ≥3:1。仓库里已经因为
            // Tag color="orange"（浅色 3.34）栽过一次，这是同一族问题的第二例，
            // 而 tsc 与 vite build 对它照样全绿——只能靠浏览器取值。
            icon={<WarningOutlined style={{ color: 'var(--amber)' }} />}
            style={{ marginTop: 10 }}
            message={
              <span style={{ fontSize: 12.5 }}>
                你设的目标位置{' '}
                <code className="lp-mono" style={{ fontSize: 11.5 }}>
                  {targetInfo.saved}
                </code>{' '}
                现在用不了，已暂时改用 <code className="lp-mono" style={{ fontSize: 11.5 }}>
                  {targetInfo.auto}
                </code>
              </span>
            }
            description={
              <span style={{ fontSize: 12 }}>
                {targetInfo.errors[0]?.message}
              </span>
            }
            action={
              <Button
                size="small"
                onClick={() => { setScope(null); setTargetOpen(true); }}
              >
                重新设置
              </Button>
            }
          />
        )}
      </Card>

      {actionable.length === 0 ? (
        <Card>
          <Empty description="当前没有可安全处理的目录" />
        </Card>
      ) : (
        <div className="lp-split">
          <Card
            size="small"
            // 窄窗下 lp-split 折成上下，这时不该再固定 372 宽
            style={{ width: 372, flexShrink: 0, maxWidth: '100%' }}
            title={`可处理 · ${actionable.length} 项`}
          >
            {actionable.map((p) => {
              const active = p.path === current?.path;
              return (
                // button 而非 div：见 SoftwarePage 台账行同处改动。这一列决定
                // 右侧显示哪条计划的判定依据，键盘到不了就只能看第一条。
                <button
                  key={p.path}
                  type="button"
                  onClick={() => setSel(p.path)}
                  aria-current={active ? 'true' : undefined}
                  className="lp-item"
                  style={{
                    alignItems: 'center', gap: 10,
                    padding: '10px 12px', borderRadius: 8,
                    marginBottom: 6,
                    border: active ? '1px solid rgba(47,129,247,0.5)' : '1px solid var(--line)',
                    background: active ? 'rgba(47,129,247,0.1)' : 'var(--bg)',
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--tx)',
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {/* 子目录级条目要标出来：不然"WebStorage"孤零零列在这里，
                          用户不知道它是 Code 底下的一块，会以为要删整个软件的数据 */}
                      {p.parent_path && (
                        <span style={{ color: 'var(--tx3)', fontWeight: 400 }}>
                          ↳{' '}
                        </span>
                      )}
                      {p.name}
                    </div>
                    <code
                      className="lp-mono"
                      style={{ fontSize: 11, color: 'var(--tx3)', display: 'block',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                    >
                      {p.parent_path
                        ? `${p.owner ?? '未归因'} · ${baseName(p.parent_path)} 的子目录`
                        : (p.owner ?? '未归因')}
                    </code>
                  </div>
                  <b className="lp-num" style={{ color: 'var(--green)' }}>{fmtSize(p.reclaimable)}</b>
                  <Tag color={ACTION_COLOR[p.action]} bordered={false}>
                    {ACTION_LABEL[p.action]}
                  </Tag>
                </button>
              );
            })}
          </Card>
          {current && (
            <PlanDetail
              plan={current}
              onExecute={confirmExecute}
              busy={busy}
              overriddenRoot={overrideOf(current.path)?.root ?? null}
              onChangeTarget={(p) => {
                setScope({
                  source: p.path,
                  label: p.name ?? baseName(p.path),
                  saved: overrideOf(p.path)?.root ?? null,
                });
                setTargetOpen(true);
              }}
            />
          )}
        </div>
      )}

      {ops && <OperationHistory report={ops} onRollback={confirmRollback} />}
      <BlockedSummary report={report} />

      {/* 条件渲染而不是 open={targetOpen}：每次打开都是新实例，输入框与校验状态跟着
          重置，不会把上次输错的路径连同那条红字一起端出来。 */}
      {targetOpen && targetInfo && (
        <TargetRootModal
          info={targetInfo}
          scope={scope ?? undefined}
          // scope 必须跟着弹窗一起清掉。留着的话下次点全局那个"更改"按钮会
          // 带着上一次的 scope 进来，于是用户以为在改全局、实际只改了某一项。
          onClose={() => { setTargetOpen(false); setScope(null); }}
          onSaved={() => { setTargetOpen(false); setScope(null); reload(); }}
        />
      )}
    </div>
  );
}
