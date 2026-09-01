import { useDeferredValue, useEffect, useMemo, useState } from 'react';
import {
  Alert, App, Button, Card, Empty, Input, Segmented, Spin, Statistic, Table, Tag,
  Tooltip,
} from 'antd';
import {
  CheckCircleOutlined, CodeOutlined, DatabaseOutlined, DeleteOutlined,
  FolderOpenOutlined, ReloadOutlined, RocketOutlined, SafetyCertificateOutlined,
  SearchOutlined, ToolOutlined, WarningOutlined,
} from '@ant-design/icons';
import {
  deepCleanUninstall, fetchUninstall, fetchUninstallAudit, fmtSize, launchUninstall,
  UninstallAuditCandidate, UninstallAuditKind, UninstallAuditReport, UninstallItem,
  UninstallRecent, UninstallReport, verifyUninstall,
} from './api';
import { SoftwareGlyph } from './SoftwareShared';
import HistoryPanel from './HistoryPanel';

type ScopeFilter = 'all' | 'user' | 'system';

const AUDIT_KIND: Record<UninstallAuditKind, { label: string; icon: JSX.Element }> = {
  file: { label: '文件目录', icon: <FolderOpenOutlined /> },
  environment: { label: '环境变量', icon: <CodeOutlined /> },
  registry: { label: '注册表', icon: <DatabaseOutlined /> },
  startup: { label: '启动链路', icon: <RocketOutlined /> },
};

export default function UninstallPage({
  onOpenSoftware,
}: { onOpenSoftware?: (entityId: string) => void }) {
  const { message, modal } = App.useApp();
  const [report, setReport] = useState<UninstallReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const deferredQuery = useDeferredValue(query);
  const [scope, setScope] = useState<ScopeFilter>('all');
  const [audit, setAudit] = useState<UninstallAuditReport | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [selectedCandidates, setSelectedCandidates] = useState<React.Key[]>([]);

  const showAudit = (next: UninstallAuditReport) => {
    setAudit(next);
    setSelectedCandidates(
      next.candidates.filter((item) => item.recommended && item.can_clean).map((item) => item.id),
    );
  };

  const load = async () => {
    setLoading(true);
    try {
      setReport(await fetchUninstall());
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '软件卸载清单读取失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const uninstall = async (item: UninstallItem) => {
    const preview = await launchUninstall(item.id, true);
    if (!preview.ok) {
      message.error(preview.refused ?? '卸载预演失败');
      return;
    }
    modal.confirm({
      title: `卸载「${item.name}」？`,
      okText: '启动卸载器',
      okButtonProps: { danger: true },
      cancelText: '取消',
      content: (
        <div style={{ lineHeight: 1.7 }}>
          <p>LostPath 将启动软件自己登记的卸载器，不会静默删除程序目录。</p>
          <p style={{ color: 'var(--amber)', marginBottom: 0 }}>软件卸载本身无法自动回滚。完成向导后可回来检测登记和残留。</p>
        </div>
      ),
      onOk: async () => {
        setBusyId(item.id);
        try {
          const result = await launchUninstall(item.id, false);
          if (!result.ok) throw new Error(result.refused ?? '卸载器启动失败');
          message.success('卸载器已启动');
          await load();
        } finally {
          setBusyId(null);
        }
      },
    });
  };

  const verify = async (recent: UninstallRecent) => {
    setBusyId(recent.operation_id);
    try {
      const response = await verifyUninstall(recent.operation_id);
      if (!response.ok || !response.result) {
        message.error(response.refused ?? '复核失败');
        return;
      }
      if (response.catalog) setReport(response.catalog);
      if (response.result.still_installed) {
        message.warning('卸载登记仍然存在，卸载可能尚未完成或已取消');
        return;
      }
      if (response.audit) {
        showAudit(response.audit);
        message.success('已确认卸载，并完成深度残留扫描');
      } else {
        message.warning(response.audit_error ?? '已确认卸载，但没有可用的卸载前关系基线');
      }
    } finally {
      setBusyId(null);
    }
  };

  const openAudit = async (recent: Pick<UninstallRecent, 'operation_id'>) => {
    setAuditLoading(true);
    setBusyId(recent.operation_id);
    try {
      showAudit(await fetchUninstallAudit(recent.operation_id));
    } catch (e) {
      message.error(e instanceof Error ? e.message : '深度残留扫描失败');
    } finally {
      setBusyId(null);
      setAuditLoading(false);
    }
  };

  useEffect(() => {
    if (!audit || audit.startup_state !== 'loading') return undefined;
    const timer = window.setTimeout(() => {
      fetchUninstallAudit(audit.operation_id)
        .then((next) => setAudit(next))
        .catch(() => undefined);
    }, 900);
    return () => window.clearTimeout(timer);
  }, [audit]);

  const cleanAudit = async () => {
    if (!audit || !selectedCandidates.length) return;
    const ids = selectedCandidates.map(String);
    const preview = await deepCleanUninstall(audit.operation_id, ids, true);
    if (!preview.ok || !preview.result) {
      message.error(preview.refused ?? '残留清理预演失败');
      return;
    }
    const previewFailures = preview.result.results.filter((item) => !item.ok);
    if (previewFailures.length) {
      message.error(previewFailures[0].error ?? '部分残留已变化，请重新扫描');
      return;
    }
    const selected = audit.candidates.filter((item) => ids.includes(item.id));
    const fileSize = selected.reduce((sum, item) => (
      item.kind === 'file' ? sum + (item.size ?? 0) : sum
    ), 0);
    modal.confirm({
      title: `清理 ${selected.length} 项卸载残留？`,
      okText: '确认清理',
      okButtonProps: { danger: true },
      cancelText: '取消',
      width: 540,
      content: (
        <div style={{ lineHeight: 1.75 }}>
          <p>目录只会移入 LostPath 回收区，注册表会整键备份，环境变量和登录启动项均可从操作记录撤销。</p>
          <p style={{ color: 'var(--tx2)', marginBottom: 0 }}>
            已选 {selected.length} 项{fileSize > 0 ? ` · 文件约 ${fmtSize(fileSize)}` : ''}。服务和计划任务不会自动修改。
          </p>
        </div>
      ),
      onOk: async () => {
        setAuditLoading(true);
        try {
          const response = await deepCleanUninstall(audit.operation_id, ids, false);
          if (!response.ok || !response.result) throw new Error(response.refused ?? '深度清理失败');
          setAudit(response.result.audit);
          setSelectedCandidates([]);
          if (response.result.failed) {
            const first = response.result.results.find((item) => !item.ok);
            message.warning(`已完成 ${response.result.succeeded} 项，${response.result.failed} 项失败${first?.error ? `：${first.error}` : ''}`);
          } else {
            message.success(`已清理 ${response.result.succeeded} 项，相关操作均已记录`);
          }
          await load();
        } finally {
          setAuditLoading(false);
        }
      },
    });
  };

  const items = useMemo(() => {
    const q = deferredQuery.trim().toLocaleLowerCase();
    return (report?.items ?? []).filter((item) => {
      if (scope !== 'all' && item.scope !== scope) return false;
      return !q || [item.name, item.publisher, item.version, item.location]
        .some((value) => value?.toLocaleLowerCase().includes(q));
    });
  }, [deferredQuery, report?.items, scope]);

  if (!report && loading) {
    return <div style={{ minHeight: 360, display: 'grid', placeItems: 'center' }}><Spin /></div>;
  }

  const summary = report?.summary;
  return (
    <div className="lp-page" style={{ padding: '22px 26px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, color: 'var(--tx)' }}>软件卸载</h1>
          <div style={{ marginTop: 4, color: 'var(--tx2)', fontSize: 12.5 }}>
            原生卸载器、登记复核与残留识别
          </div>
        </div>
        <Tooltip title="重新读取 Windows 软件清单">
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()} style={{ marginLeft: 'auto' }} />
        </Tooltip>
      </div>

      {error && <Card size="small" style={{ marginBottom: 14, color: 'var(--red)' }}>{error}</Card>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginBottom: 14 }}>
        <Card size="small" className="lp-card-elevated"><Statistic title="桌面软件" value={summary?.total ?? 0} prefix={<ToolOutlined style={{ color: 'var(--accent-fg)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="可启动卸载" value={summary?.uninstallable ?? 0} prefix={<CheckCircleOutlined style={{ color: 'var(--green)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="当前用户" value={summary?.user ?? 0} prefix={<SafetyCertificateOutlined style={{ color: 'var(--cyan)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="登记需修复" value={summary?.needs_repair ?? 0} prefix={<WarningOutlined style={{ color: 'var(--amber)' }} />} /></Card>
      </div>

      {audit && (
        <Card
          size="small"
          className="lp-card-elevated"
          style={{ marginBottom: 14 }}
          title={`${audit.name} · 深度卸载报告`}
          extra={(
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={auditLoading}
              onClick={() => void openAudit({ operation_id: audit.operation_id })}
            >
              重新核对
            </Button>
          )}
        >
          <Alert
            type={audit.summary.total ? 'warning' : 'success'}
            showIcon
            message={audit.summary.total
              ? `发现 ${audit.summary.total} 项卸载后关联，${audit.summary.actionable} 项可安全记录后处理`
              : '没有发现仍存在的关联残留'}
            description={(
              <span>
                卸载器已移除环境变量 {audit.changes.environment.removed} 项、注册表登记 {audit.changes.registry.removed} 项、启动链路 {audit.changes.startup.removed} 项。
                {audit.summary.file_size > 0 && ` 当前文件目录约 ${fmtSize(audit.summary.file_size)}。`}
              </span>
            )}
            style={{ marginBottom: 12 }}
          />
          {audit.startup_state === 'loading' && (
            <Alert
              type="info"
              showIcon
              message="服务和计划任务仍在后台读取，报告会自动补全"
              style={{ marginBottom: 12 }}
            />
          )}
          {audit.candidates.length ? (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
                <Button
                  size="small"
                  onClick={() => setSelectedCandidates(
                    audit.candidates.filter((item) => item.recommended && item.can_clean).map((item) => item.id),
                  )}
                >
                  选择推荐项
                </Button>
                <Button size="small" onClick={() => setSelectedCandidates([])}>清空选择</Button>
                <span style={{ color: 'var(--tx3)', fontSize: 11.5 }}>
                  安装目录、配置数据和通用系统项不会默认勾选
                </span>
                <Button
                  type="primary"
                  danger
                  disabled={!selectedCandidates.length}
                  loading={auditLoading}
                  onClick={() => void cleanAudit()}
                  style={{ marginLeft: 'auto' }}
                >
                  清理所选 {selectedCandidates.length} 项
                </Button>
              </div>
              <Table<UninstallAuditCandidate>
                rowKey="id"
                size="small"
                dataSource={audit.candidates}
                pagination={audit.candidates.length > 12
                  ? { pageSize: 12, showSizeChanger: false, showTotal: (total) => `共 ${total} 项` }
                  : false}
                scroll={{ x: 940 }}
                rowSelection={{
                  selectedRowKeys: selectedCandidates,
                  onChange: setSelectedCandidates,
                  getCheckboxProps: (item) => ({
                    disabled: !item.can_clean,
                    title: item.can_clean ? undefined : item.reason,
                  }),
                }}
                columns={[
                  {
                    title: '类型', width: 115,
                    render: (_, item) => (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, color: 'var(--tx2)' }}>
                        <span style={{ color: 'var(--accent-fg)', display: 'flex' }}>{AUDIT_KIND[item.kind].icon}</span>
                        {AUDIT_KIND[item.kind].label}
                      </span>
                    ),
                  },
                  {
                    title: '关联项', width: 235,
                    render: (_, item) => (
                      <div style={{ minWidth: 0 }}>
                        <div style={{ color: 'var(--tx)', fontWeight: 600 }}>{item.name}</div>
                        <div style={{ marginTop: 3 }}>
                          {item.recommended && (
                            <Tag bordered style={{ color: 'var(--green)', borderColor: 'var(--green)', background: 'transparent' }}>推荐清理</Tag>
                          )}
                          {item.can_clean && !item.recommended && (
                            <Tag bordered style={{ color: 'var(--amber)', borderColor: 'var(--amber)', background: 'transparent' }}>需确认</Tag>
                          )}
                          {!item.can_clean && <Tag bordered={false}>只读核对</Tag>}
                          {item.size != null && <span style={{ color: 'var(--tx3)', fontSize: 11 }}>{fmtSize(item.size)}</span>}
                        </div>
                      </div>
                    ),
                  },
                  {
                    title: '位置或来源', ellipsis: true,
                    render: (_, item) => item.path
                      ? <code className="lp-mono" title={item.path}>{item.path}</code>
                      : <span style={{ color: 'var(--tx2)' }}>{item.scope === 'user' ? '当前用户' : item.source || item.scope || '系统登记'}</span>,
                  },
                  {
                    title: '判断依据', width: 285,
                    render: (_, item) => (
                      <div style={{ color: 'var(--tx2)', fontSize: 11.5, lineHeight: 1.55 }}>
                        {item.reason}
                        {item.confidence != null && <span style={{ color: 'var(--tx3)' }}> · {Math.round(item.confidence * 100)}%</span>}
                      </div>
                    ),
                  },
                ]}
              />
            </>
          ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有需要处理的深度残留" />}
        </Card>
      )}

      {!!report?.recent.length && (
        <HistoryPanel
          title="最近卸载"
          items={report.recent}
          itemKey={(item) => item.operation_id}
          style={{ marginBottom: 14 }}
          renderItem={(item) => (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '7px 0', borderBottom: '1px solid var(--line)' }}>
              <span style={{ color: 'var(--tx)', flex: 1 }}>{item.name}</span>
              <Tag bordered={false}>{item.verified_removed ? '已确认卸载' : '卸载器已启动'}</Tag>
              {!item.verified_removed && item.status === 'done' && (
                <Button size="small" loading={busyId === item.operation_id} icon={<SearchOutlined />} onClick={() => void verify(item)}>检测残留</Button>
              )}
              {item.verified_removed && item.baseline_captured && (
                <Button size="small" loading={busyId === item.operation_id} icon={<SearchOutlined />} onClick={() => void openAudit(item)}>
                  深度报告
                </Button>
              )}
            </div>
          )}
        />
      )}

      <Card size="small" className="lp-card-elevated">
        <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
          <Segmented
            value={scope}
            onChange={(value) => setScope(value as ScopeFilter)}
            options={[{ label: '全部', value: 'all' }, { label: '当前用户', value: 'user' }, { label: '系统级', value: 'system' }]}
          />
          <Input.Search
            allowClear
            placeholder="搜索软件、发布商或安装位置"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            style={{ maxWidth: 380, minWidth: 230 }}
          />
        </div>
        {items.length ? (
          <Table<UninstallItem>
            rowKey="id"
            size="small"
            dataSource={items}
            pagination={{ pageSize: 25, showSizeChanger: false, showTotal: (total) => `共 ${total} 项` }}
            scroll={{ x: 980 }}
            columns={[
              {
                title: '软件', dataIndex: 'name', width: 280,
                render: (name: string, item) => (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                    <SoftwareGlyph name={name} icon={item.icon ?? item.entity?.icon} size={34} />
                    <div style={{ minWidth: 0 }}>
                      {item.entity && onOpenSoftware ? (
                        <Button
                          type="link"
                          size="small"
                          onClick={() => onOpenSoftware(item.entity!.entity_id)}
                          style={{ height: 'auto', padding: 0, color: 'var(--tx)', fontWeight: 600 }}
                        >
                          {name}
                        </Button>
                      ) : (
                        <div style={{ color: 'var(--tx)', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</div>
                      )}
                      <div style={{ color: 'var(--tx3)', fontSize: 11.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.publisher || '未知发布商'}</div>
                    </div>
                  </div>
                ),
              },
              { title: '版本', dataIndex: 'version', width: 130, render: (value?: string) => value || '—' },
              { title: '登记大小', dataIndex: 'estimated_size', width: 120, render: (value?: number) => fmtSize(value) },
              {
                title: '安装位置', dataIndex: 'location', ellipsis: true,
                render: (value?: string) => <code className="lp-mono" style={{ color: 'var(--tx3)' }}>{value || '未登记'}</code>,
              },
              {
                title: '操作', width: 120, align: 'right',
                render: (_, item) => item.can_uninstall
                  ? <Button size="small" danger icon={<DeleteOutlined />} loading={busyId === item.id} onClick={() => void uninstall(item)}>卸载</Button>
                  : <Tooltip title={item.reason}><span style={{ color: 'var(--tx3)' }}>不可用</span></Tooltip>,
              },
            ]}
          />
        ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的软件" />}
      </Card>
    </div>
  );
}
