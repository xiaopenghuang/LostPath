// 设置页。系统路径与运行事实保持只读，用户规则单独提供可撤销的保留入口。
import { useEffect, useState } from 'react';
import { Alert, App as AntdApp, Button, Card, Select, Spin, Switch, Tag, Typography } from 'antd';
import {
  fetchInspection, fetchRules, fetchSettings, fmtSize, InspectionReport,
  removeIgnorePath, RulesReport, saveInspection, SettingsReport,
} from './api';

const { Paragraph } = Typography;

function Row({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div style={{ display: 'flex', gap: 12, padding: '7px 0', borderBottom: '1px solid var(--line)' }}>
      <div style={{ width: 132, flexShrink: 0, fontSize: 12, color: 'var(--tx3)' }}>{label}</div>
      <div style={{ flex: 1, minWidth: 0, fontSize: 12.5, color: 'var(--tx)' }}>
        {value}
        {hint && <div style={{ fontSize: 11, color: 'var(--tx3)', marginTop: 2 }}>{hint}</div>}
      </div>
    </div>
  );
}

const Mono = ({ children }: { children: React.ReactNode }) => (
  <code className="lp-mono" style={{ fontSize: 11.5, color: 'var(--cyan)', wordBreak: 'break-all' }}>
    {children}
  </code>
);

/**
 * 具体列出扫不进去的目录。
 *
 * 原先只显示"96 个目录拒绝访问"——那个数字既不能让人判断漏了多少空间，也不能判断
 * 值不值得提权重扫。列出路径之后就能看出来：大多是别的用户的目录和系统保护目录，
 * 属于"本来就不该我管"，而不是"我的软件藏在那儿"。
 */
function DeniedList({ paths, total, elevated }: {
  paths: string[]; total: number; elevated: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!total) return null;
  return (
    <div style={{ padding: '7px 0', borderBottom: '1px solid var(--line)' }}>
      <div style={{ display: 'flex', gap: 12 }}>
        <div style={{ width: 132, flexShrink: 0, fontSize: 12, color: 'var(--tx3)' }}>
          扫不进去的目录
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Button size="small" onClick={() => setOpen((v) => !v)}>
            {open ? '收起' : `查看列表（前 ${Math.min(paths.length, 40)} 项）`}
          </Button>
          {!elevated && (
            <div style={{ fontSize: 11, color: 'var(--tx3)', marginTop: 6, lineHeight: 1.7 }}>
              提升权限的方式：完全退出 LostPath，右键程序图标选择「以管理员身份运行」，
              之后<b>需重新扫描一次</b>，此前不可读的目录才会纳入数据。
              <br />
              本程序<b>不提供一键提权</b>。原因有两层：一是那需要在两个进程之间交接
              服务端口与运行状态，实测存在难以穷尽的时序问题，失败时还会留下标准权限
              无法结束的残留进程；二是「由界面向提权进程下发命令」这种形态，要求用户在
              无法确认将执行何种操作的前提下授予权限。管理员权限仅扩大可读取范围，
              写入操作仍限于三处（保存快照、登记便携软件、执行清理与迁移），
              且均需逐项确认。
            </div>
          )}
          {open && (
            <div
              style={{
                marginTop: 8, maxHeight: 220, overflowY: 'auto',
                background: 'var(--bg)', border: '1px solid var(--line)',
                borderRadius: 6, padding: '6px 9px',
              }}
            >
              {paths.length === 0 ? (
                <span style={{ fontSize: 11.5, color: 'var(--tx3)' }}>
                  这份快照是旧版本扫的，没记下具体路径。重新扫描一次就有了。
                </span>
              ) : (
                paths.map((p) => (
                  <code
                    key={p}
                    className="lp-mono"
                    style={{
                      display: 'block', fontSize: 11, color: 'var(--tx2)',
                      wordBreak: 'break-all', lineHeight: 1.75,
                    }}
                  >
                    {p}
                  </code>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function IgnoredRules({ report, onChanged }: { report: RulesReport | null; onChanged: (next: RulesReport) => void }) {
  const { message } = AntdApp.useApp();
  if (!report) return null;
  return (
    <Card size="small" title={`用户保留规则 · ${report.count} 条`} style={{ marginBottom: 12 }}>
      <div style={{ color: 'var(--tx2)', fontSize: 'var(--fs-sm)', marginBottom: 10 }}>
        被保留的路径不会进入清理或迁移计划，规则只收紧操作，不会删除任何文件。可在 系统盘全景中选中目录后添加。
      </div>
      {!report.ignored_paths.length ? (
        <div style={{ color: 'var(--tx3)', fontSize: 'var(--fs-sm)' }}>还没有手动保留的路径。</div>
      ) : (
        report.ignored_paths.map((rule) => (
          <div key={rule.path} style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '7px 0', borderTop: '1px solid var(--line)' }}>
            <code className="lp-mono" title={rule.path} style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--tx2)', fontSize: 11 }}>
              {rule.path}
            </code>
            <Button
              size="small"
              onClick={async () => {
                try {
                  await removeIgnorePath(rule.path);
                  onChanged({ ...report, ignored_paths: report.ignored_paths.filter((x) => x.path !== rule.path), count: report.count - 1 });
                  message.success('已取消保留，下一次出计划时生效');
                } catch (e) {
                  message.error(e instanceof Error ? e.message : '取消规则失败');
                }
              }}
            >
              取消保留
            </Button>
          </div>
        ))
      )}
    </Card>
  );
}

function InspectionCard({ report, onChanged }: {
  report: InspectionReport | null;
  onChanged: (next: InspectionReport) => void;
}) {
  const { message } = AntdApp.useApp();
  const [saving, setSaving] = useState(false);
  if (!report) return null;
  const scanned = report.last_scanned_at
    ? new Date(report.last_scanned_at).toLocaleString('zh-CN')
    : '还没有扫描记录';
  const update = async (enabled: boolean, interval = report.interval_hours) => {
    setSaving(true);
    try {
      onChanged(await saveInspection(enabled, interval));
      message.success(
        interval !== report.interval_hours
          ? '巡检间隔已保存'
          : enabled ? '自动巡检已开启' : '自动巡检已关闭',
      );
    } catch (e) {
      message.error(e instanceof Error ? e.message : '保存巡检设置失败');
    } finally {
      setSaving(false);
    }
  };
  return (
    <Card size="small" title="自动巡检" style={{ marginBottom: 12 }}>
      <Row
        label="定期扫描"
        value={<Switch checked={report.enabled} loading={saving} onChange={(v) => update(v)} />}
        hint="LostPath 运行期间按间隔触发只读扫描，发现空间增长后可在增长雷达查看。"
      />
      <Row
        label="扫描间隔"
        value={(
          <Select
            size="small"
            value={report.interval_hours}
            disabled={saving}
            style={{ width: 120 }}
            onChange={(v) => update(report.enabled, v)}
            options={[6, 12, 24, 72].map((hours) => ({ value: hours, label: `${hours} 小时` }))}
          />
        )}
        hint={`上次扫描：${scanned}${report.due ? '，已到下一次巡检时间' : ''}`}
      />
    </Card>
  );
}

export default function SettingsPage() {
  const [s, setS] = useState<SettingsReport | null>(null);
  const [rules, setRules] = useState<RulesReport | null>(null);
  const [inspection, setInspection] = useState<InspectionReport | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetchSettings()
      .then(setS)
      .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'));
    fetchRules().then(setRules).catch(() => setRules(null));
    fetchInspection().then(setInspection).catch(() => setInspection(null));
  }, []);

  if (err) return <div style={{ padding: 24 }}><Alert type="error" showIcon message="读取设置失败" description={err} /></div>;
  // tip 写在 Spin 上不渲染（antd 5 只在 nest / fullscreen 模式下认），说明另起一行
  if (!s)
    return (
      <div style={{ padding: 64, textAlign: 'center' }} role="status" aria-busy="true">
        <Spin />
        <div style={{ marginTop: 12, fontSize: 'var(--fs-md)', color: 'var(--tx2)' }}>
          读取运行状态…
        </div>
      </div>
    );

  const scanned = s.snapshot.scanned_at
    ? new Date(s.snapshot.scanned_at).toLocaleString('zh-CN')
    : null;

  // 与 App.tsx 侧栏那个 staleSnapshot 同一判据：进程已提权、而数据是标准权限下采的。
  // **不能用 denied_count > 0 判** —— 管理员下它也恒为真（系统保护目录始终不可读）。
  const staleData = s.engine.elevated && s.snapshot.elevated === false;

  return (
    <div className="lp-page" style={{ padding: '20px 26px', maxWidth: 900 }}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 14 }}
        message="系统设置只读，用户规则可调整"
        description="数据位置由环境变量 LOSTPATH_DATA_DIR 决定，改完重启引擎生效。用户保留规则只会阻止计划，不会直接操作文件。"
      />

      <Card size="small" title="数据位置" style={{ marginBottom: 12 }}>
        <Row
          label="数据根目录"
          value={<Mono>{s.paths.data_root}</Mono>}
          hint={
            s.paths.override_active
              ? `当前由环境变量 ${s.paths.override_var ?? 'LOSTPATH_DATA_DIR'} 覆盖`
              : '默认位置（%LOCALAPPDATA%\\LostPath）。在程序目录之外，正常更新不会影响；卸载前仍需先处理回收区'
          }
        />
        <Row label="最新快照" value={<Mono>{s.paths.latest_snapshot}</Mono>} />
        <Row label="图标缓存" value={<Mono>{s.paths.icons_dir}</Mono>} />
        <Row label="便携软件登记" value={<Mono>{s.paths.portable_config}</Mono>} />
        <Row label="用户规则" value={<Mono>{s.paths.rules_config ?? '—'}</Mono>} />
        <Row label="巡检配置" value={<Mono>{s.paths.inspection_config ?? '—'}</Mono>} />
      </Card>

      <Card size="small" title="上次扫描" style={{ marginBottom: 12 }}>
        {!s.snapshot.present ? (
          <Alert
            type="warning"
            showIcon={false}
            message="还没有扫描过"
            description="仪表盘或软件台账里点「重新扫描 系统盘」，之后痕迹归因才有数据。"
          />
        ) : (
          <>
            <Row label="扫描时间" value={scanned ?? '—'} />
            <Row label="机器名" value={s.snapshot.machine ?? '—'} />
            <Row
              label="覆盖范围"
              value={
                <>
                  {(s.snapshot.total_dirs ?? 0).toLocaleString()} 个目录 ·{' '}
                  {(s.snapshot.total_files ?? 0).toLocaleString()} 个文件 ·{' '}
                  {fmtSize(s.snapshot.total_bytes)}
                </>
              }
              hint={s.snapshot.elapsed_sec != null ? `耗时 ${s.snapshot.elapsed_sec} 秒` : undefined}
            />
            {/* 权限有两个，**必须分开显示**：一个是此刻进程的，一个是产出这份数据时
                的。原先这里只显示前者却用来解释后者的数字，于是以管理员扫完之后仍
                写着"非管理员进不去的目录"——把原因说错了。 */}
            <Row
              label="当前进程权限"
              value={
                s.engine.elevated
                  ? <Tag color="green" bordered={false}>管理员</Tag>
                  : <Tag color="orange" bordered={false}>标准用户</Tag>
              }
              hint={
                s.engine.elevated
                  ? '可读取范围已最大化'
                  : '部分目录不可读取，见下方「如何提升权限」'
              }
            />
            <Row
              label="采集时权限"
              value={
                s.snapshot.elevated == null
                  ? <Tag bordered={false}>未记录（旧版本快照）</Tag>
                  : s.snapshot.elevated
                    ? <Tag color="green" bordered={false}>管理员</Tag>
                    : <Tag color="orange" bordered={false}>标准用户</Tag>
              }
              hint={
                staleData
                  ? '当前已是管理员，但这份数据是标准权限下采集的 —— 重新扫描后不可读目录将纳入统计'
                  : '决定了这份数据的完整程度，与上方的当前权限是两件事'
              }
            />
            <Row
              label="不可读目录"
              value={`${(s.snapshot.denied_count ?? 0).toLocaleString()} 项`}
              hint={
                s.snapshot.elevated
                  ? '采集时已是管理员，余下的属于系统保护目录或被独占占用，提升权限亦无法读取'
                  : '这些目录的体积不计入任何统计 —— 「其他已用」中有一部分即为它们'
              }
            />
            {/* elevated 传的是**采集时**的权限：这一段解释的是"这些目录为什么读不到"，
                那取决于扫的时候有没有权限，而不是此刻。旧快照未记录时按 false 处理
                （给出提权引导），宁可多提示一次也不隐瞒可能的盲区。 */}
            <DeniedList paths={s.snapshot.denied_sample ?? []}
                        total={s.snapshot.denied_count ?? 0}
                        elevated={s.snapshot.elevated === true} />
            <Row
              label="重解析点"
              value={`${(s.snapshot.reparse_count ?? 0).toLocaleString()} 处`}
              hint="junction / 符号链接，扫描时不递归进去，避免同一份数据重复计量"
            />
          </>
        )}
      </Card>

      <Card size="small" title="回收与撤销" style={{ marginBottom: 12 }}>
        <Row
          label="可恢复期"
          value={`${s.recycle.recoverable_days} 天`}
          hint="清理的数据先搬进回收区，这段时间内可一键还原"
        />
        <Row
          label="到期处理"
          value={
            s.recycle.auto_purge === 'startup'
              ? '每次启动时自动永久删除已过期的数据'
              : '不自动清理，需到回收站手动腾空'
          }
          hint={
            s.recycle.auto_purge === 'startup'
              ? '只删已过可恢复期的；回收期内的一个都不动。删了哪些记在 logs/purge.log 里。想提前腾空可到回收站点名删除'
              : undefined
          }
        />
        <Row
          label="回收区位置"
          value={<Mono>{s.recycle.recycle_root}</Mono>}
          hint="在 LostPath 的数据目录内，所以卸载 LostPath 会连带删除它——第三方卸载工具（Geek、Revo 等）做残留深扫时尤其如此。卸载前请先把回收站里还想留的数据还原回原位"
        />
      </Card>

      <InspectionCard report={inspection} onChanged={setInspection} />

      <IgnoredRules report={rules} onChanged={setRules} />

      <Card size="small" title="引擎">
        <Row label="监听地址" value={<Mono>{s.engine.bind}</Mono>} hint="只绑回环，外部网络访问不到" />
        <Row
          label="扫描根"
          value={<Mono>{s.engine.scan_root}</Mono>}
          hint="不接受入参（避免路径注入），但也不写死 C —— 从 %SystemDrive% 取，系统装哪个盘就扫哪个"
        />
        <Row label="Python" value={s.engine.python} />
      </Card>

      <Paragraph style={{ fontSize: 11.5, color: 'var(--tx3)', marginTop: 14 }}>
        系统集成功能只修改当前用户范围，均需逐项确认并先写恢复记录；服务、计划任务和机器级设置保持只读。
      </Paragraph>
    </div>
  );
}
