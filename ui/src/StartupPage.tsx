import { useEffect, useMemo, useState } from 'react';
import {
  Alert, App, Button, Card, Empty, Input, Segmented, Spin, Statistic, Table, Tag, Tooltip,
} from 'antd';
import {
  CloudServerOutlined, LoginOutlined, ReloadOutlined, RocketOutlined, SafetyCertificateOutlined,
  ScheduleOutlined, SearchOutlined, StopOutlined, UndoOutlined, WarningOutlined,
} from '@ant-design/icons';
import {
  disableStartup, fetchStartup, refreshStartup, restoreStartup,
  StartupItem, StartupKind, StartupReport,
} from './api';
import { SoftwareGlyph } from './SoftwareShared';

const KIND_LABEL: Record<StartupKind | 'all', string> = {
  all: '全部', startup: '登录启动', service: '系统服务', task: '计划任务',
};

const RISK_LABEL = {
  attention: { text: '需留意', color: 'var(--amber)' },
  normal: { text: '普通', color: 'var(--accent-fg)' },
  system: { text: '系统项', color: 'var(--green)' },
} as const;

function KindIcon({ kind }: { kind: StartupKind }) {
  if (kind === 'startup') return <LoginOutlined />;
  if (kind === 'service') return <CloudServerOutlined />;
  return <ScheduleOutlined />;
}

function Summary({ report }: { report: StartupReport }) {
  const cards = [
    { label: '登录启动', value: report.summary.startup, icon: <LoginOutlined />, color: 'var(--accent-fg)' },
    { label: '系统服务', value: report.summary.services, icon: <CloudServerOutlined />, color: 'var(--cyan)' },
    { label: '计划任务', value: report.summary.tasks, icon: <ScheduleOutlined />, color: 'var(--green)' },
    { label: '需留意', value: report.summary.attention, icon: <WarningOutlined />, color: 'var(--amber)' },
    { label: '已关联软件', value: report.summary.associated, icon: <SafetyCertificateOutlined />, color: 'var(--green)' },
    { label: '可管理', value: report.summary.manageable, icon: <StopOutlined />, color: 'var(--accent-fg)' },
    { label: '已禁用', value: report.summary.disabled, icon: <UndoOutlined />, color: 'var(--green)' },
  ];
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(145px, 1fr))', gap: 12, marginBottom: 14 }}>
      {cards.map((card) => (
        <Card key={card.label} size="small" className="lp-card-elevated">
          <Statistic
            title={<span style={{ color: 'var(--tx2)' }}>{card.label}</span>}
            value={card.value}
            prefix={<span style={{ color: card.color, fontSize: 16 }}>{card.icon}</span>}
            valueStyle={{ color: card.color, fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}
          />
        </Card>
      ))}
    </div>
  );
}

function Details({ item }: { item: StartupItem }) {
  const risk = RISK_LABEL[item.risk];
  return (
    <div style={{ padding: '4px 12px 10px 44px', display: 'grid', gap: 5, fontSize: 12 }}>
      <div style={{ color: 'var(--tx2)' }}>
        <b style={{ color: 'var(--tx)' }}>判定依据：</b>{item.risk_reason}
      </div>
      {item.owner && (
        <div style={{ color: 'var(--tx2)' }}>
          <b style={{ color: 'var(--tx)' }}>关联软件：</b>{item.owner}
          {item.owner_reason && ` · ${item.owner_reason}`}
          {item.owner_confidence != null && ` · ${Math.round(item.owner_confidence * 100)}%`}
        </div>
      )}
      {item.detail && (
        <div style={{ color: 'var(--tx2)' }}>
          <b style={{ color: 'var(--tx)' }}>来源标识：</b><code className="lp-mono">{item.detail}</code>
        </div>
      )}
      {item.task_path && (
        <div style={{ color: 'var(--tx2)' }}>
          <b style={{ color: 'var(--tx)' }}>任务路径：</b><code className="lp-mono">{item.task_path}</code>
        </div>
      )}
      <div style={{ color: 'var(--tx3)', marginTop: 2 }}>
        {item.kind === 'service' && item.start ? `启动类型：${item.start} · ` : ''}
        {item.state ? `当前状态：${item.state} · ` : ''}
        <Tag bordered style={{ color: risk.color, borderColor: risk.color, background: 'transparent' }}>
          {risk.text} · {item.risk_score} 分
        </Tag>
      </div>
      {item.manage && (
        <div style={{ color: item.manage.disabled ? 'var(--green)' : 'var(--tx3)' }}>
          {item.manage.reason}
        </div>
      )}
    </div>
  );
}

export default function StartupPage({
  focusEntityId,
  focusEntityName,
  onOpenSoftware,
}: {
  focusEntityId?: string | null;
  focusEntityName?: string | null;
  onOpenSoftware?: (entityId: string) => void;
}) {
  const { message, modal } = App.useApp();
  const [report, setReport] = useState<StartupReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<StartupKind | 'all'>('all');
  const [relatedOnly, setRelatedOnly] = useState(!!focusEntityId);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!focusEntityId) return;
    setRelatedOnly(true);
    setKind('all');
  }, [focusEntityId]);

  const load = () => {
    fetchStartup()
      .then((next) => { setReport(next); setError(next.error ?? null); })
      .catch((e) => setError(e instanceof Error ? e.message : '读取启动项失败'));
  };

  useEffect(() => { load(); }, []);

  useEffect(() => {
    if (report?.state !== 'loading') return undefined;
    const timer = window.setInterval(load, 800);
    return () => window.clearInterval(timer);
  }, [report?.state]);

  const runRefresh = async () => {
    setRefreshing(true);
    try {
      const next = await refreshStartup();
      setReport(next);
      setError(null);
      message.info('已开始重新读取，结果出来后会自动更新');
    } catch (e) {
      message.error(e instanceof Error ? e.message : '启动项刷新失败');
    } finally {
      setRefreshing(false);
    }
  };

  const disableItem = (item: StartupItem) => {
    modal.confirm({
      title: `禁用「${item.name}」的登录启动？`,
      okText: '确认禁用',
      cancelText: '取消',
      content: (
        <div style={{ fontSize: 13, lineHeight: 1.7 }}>
          <p>只会从当前用户的登录启动项中移除，不会删除程序或文件。原值会保存在 LostPath 操作记录里，之后可以恢复。</p>
          {item.exe && <code className="lp-mono" style={{ fontSize: 11.5, wordBreak: 'break-all' }}>{item.exe}</code>}
        </div>
      ),
      onOk: async () => {
        const res = await disableStartup(item.id, false);
        if (res.ok) {
          message.success('已禁用登录启动，可在这里恢复');
          load();
        } else {
          message.error(res.refused ?? '禁用失败');
        }
      },
    });
  };

  const restoreItem = (item: StartupItem) => {
    const operationId = item.manage?.action_id;
    if (!operationId) return;
    modal.confirm({
      title: `恢复「${item.name}」的登录启动？`,
      okText: '确认恢复',
      cancelText: '取消',
      content: '恢复后，Windows 下次登录会再次运行这个程序。',
      onOk: async () => {
        const res = await restoreStartup(operationId);
        if (res.ok) {
          message.success('已恢复登录启动');
          load();
        } else {
          message.error(res.refused ?? '恢复失败');
        }
      },
    });
  };

  const items = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (report?.items ?? []).filter((item) => {
      if (relatedOnly && focusEntityId && item.owner_id !== focusEntityId) return false;
      if (kind !== 'all' && item.kind !== kind) return false;
      if (!q) return true;
      return [item.name, item.exe, item.owner, item.detail, item.task_path, item.risk_reason]
        .some((value) => value?.toLowerCase().includes(q));
    });
  }, [focusEntityId, kind, query, relatedOnly, report?.items]);

  if (!report && !error) {
    return (
      <div style={{ minHeight: 360, display: 'grid', placeItems: 'center' }} role="status">
        <div style={{ textAlign: 'center' }}>
          <Spin />
          <div style={{ color: 'var(--tx2)', fontSize: 12, marginTop: 10 }}>读取系统启动项…</div>
        </div>
      </div>
    );
  }

  return (
    <div className="lp-page" style={{ padding: '22px 26px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 6 }}>
        <div>
          <h1 style={{ margin: 0, color: 'var(--tx)', fontSize: 24, fontWeight: 700 }}>启动管理</h1>
          <div style={{ marginTop: 4, color: 'var(--tx2)', fontSize: 12.5 }}>
            查看登录启动项、自动服务和计划任务，帮助判断哪些程序会随系统运行
          </div>
        </div>
        <Tooltip title="重新读取 Windows 启动项、服务和计划任务">
          <Button
            icon={<ReloadOutlined />}
            loading={refreshing || report?.state === 'loading'}
            onClick={runRefresh}
            style={{ marginLeft: 'auto' }}
          >
            刷新
          </Button>
        </Tooltip>
      </div>

      <Alert
        type="info"
        showIcon
        icon={<SafetyCertificateOutlined />}
        style={{ margin: '14px 0' }}
        message="启动项优化"
        description={
          report
            ? `当前有 ${report.summary.manageable} 项当前用户登录启动可以直接管理，已禁用 ${report.summary.disabled} 项。系统服务和计划任务只做分析，不会被修改。`
            : '登录启动项可以安全禁用并随时恢复，系统服务和计划任务只做分析。'
        }
      />

      {error && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 14 }}
          message="启动项读取失败"
          description={`${error}。请确认 PowerShell 可用后再刷新。`}
        />
      )}

      {report && <Summary report={report} />}

      <Card size="small" className="lp-card-elevated" title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><RocketOutlined style={{ color: 'var(--accent-fg)' }} />系统集成点</span>}>
        {focusEntityId && relatedOnly && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, padding: '8px 10px', border: '1px solid var(--line)', borderRadius: 8, background: 'var(--bg)' }}>
            <span style={{ color: 'var(--tx2)', flex: 1 }}>正在查看 {focusEntityName || '该软件'} 的启动链路</span>
            <Button size="small" onClick={() => setRelatedOnly(false)}>查看全部项目</Button>
          </div>
        )}
        <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
          <Segmented
            value={kind}
            onChange={(value) => setKind(value as StartupKind | 'all')}
            options={(Object.keys(KIND_LABEL) as (StartupKind | 'all')[]).map((key) => ({ label: KIND_LABEL[key], value: key }))}
          />
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索名称、执行文件或关联软件"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ maxWidth: 360, flex: 1, minWidth: 220 }}
          />
          {report?.scanned_at && (
            <span style={{ marginLeft: 'auto', alignSelf: 'center', color: 'var(--tx3)', fontSize: 11 }}>
              读取于 {new Date(report.scanned_at).toLocaleString()}
            </span>
          )}
        </div>

        {report?.state === 'loading' && !report.items.length ? (
          <div style={{ padding: 34, textAlign: 'center' }} role="status">
            <Spin />
            <div style={{ color: 'var(--tx2)', fontSize: 12, marginTop: 10 }}>正在读取 Windows 系统集成点…</div>
          </div>
        ) : items.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的启动项" />
        ) : (
          <Table<StartupItem>
            size="small"
            rowKey="id"
            dataSource={items}
            expandable={{ expandedRowRender: (item) => <Details item={item} /> }}
            pagination={{ pageSize: 30, showSizeChanger: false, showTotal: (total) => `共 ${total} 项` }}
            scroll={{ x: 900 }}
            columns={[
              {
                title: '名称',
                width: 220,
                render: (_, item) => (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                    <span style={{ color: item.risk === 'attention' ? 'var(--amber)' : 'var(--accent-fg)', display: 'flex' }}><KindIcon kind={item.kind} /></span>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={item.name}>{item.name}</span>
                  </div>
                ),
              },
              { title: '来源', dataIndex: 'source', width: 100, render: (value: string) => <Tag bordered={false}>{value}</Tag> },
              {
                title: '状态', width: 110,
                render: (_, item) => item.kind === 'service'
                  ? <span style={{ color: item.state === 'Running' ? 'var(--green)' : 'var(--tx2)' }}>{item.state || '未知'} · {item.start || '未知'}</span>
                  : <span style={{ color: item.manage?.disabled ? 'var(--green)' : 'var(--tx2)' }}>
                      {item.kind === 'startup' ? (item.manage?.disabled ? '已禁用' : '登录时运行') : '系统触发'}
                    </span>,
              },
              {
                title: '关联软件', dataIndex: 'owner', width: 190,
                render: (value: string | null, item) => value ? (
                  <Tooltip title={item.owner_reason ? `${item.owner_reason} · ${Math.round((item.owner_confidence ?? 0) * 100)}%` : undefined}>
                    <Button
                      size="small"
                      type="text"
                      onClick={() => item.owner_id && onOpenSoftware?.(item.owner_id)}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 7, maxWidth: '100%', paddingInline: 4 }}
                    >
                      <SoftwareGlyph name={value} icon={item.owner_icon} size={24} />
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{value}</span>
                    </Button>
                  </Tooltip>
                ) : <span style={{ color: 'var(--tx3)' }}>未关联</span>,
              },
              {
                title: '执行文件', dataIndex: 'exe', ellipsis: true,
                render: (value: string | null) => value ? <code className="lp-mono" style={{ fontSize: 11.5 }} title={value}>{value}</code> : <span style={{ color: 'var(--tx3)' }}>未解析</span>,
              },
              {
                title: '提示', width: 90,
                render: (_, item) => {
                  const r = RISK_LABEL[item.risk];
                  return <Tag bordered style={{ color: r.color, borderColor: r.color, background: 'transparent' }}>{r.text}</Tag>;
                },
              },
              {
                title: '操作', width: 104, fixed: 'right' as const,
                render: (_, item) => {
                  if (item.manage?.can_disable) {
                    return <Button size="small" icon={<StopOutlined />} onClick={() => disableItem(item)}>禁用</Button>;
                  }
                  if (item.manage?.can_restore) {
                    return <Button size="small" icon={<UndoOutlined />} onClick={() => restoreItem(item)}>恢复</Button>;
                  }
                  return <Tooltip title={item.manage?.reason ?? '仅查看'}><span style={{ color: 'var(--tx3)', fontSize: 12 }}>仅查看</span></Tooltip>;
                },
              },
            ]}
          />
        )}
      </Card>
    </div>
  );
}
