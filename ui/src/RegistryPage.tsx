import { useDeferredValue, useEffect, useMemo, useState } from 'react';
import { App, Button, Card, Empty, Input, Segmented, Spin, Statistic, Table, Tag, Tooltip } from 'antd';
import {
  CheckCircleOutlined, ClearOutlined, ReloadOutlined, SafetyCertificateOutlined,
  UndoOutlined, WarningOutlined,
} from '@ant-design/icons';
import {
  cleanupRegistryEntry, fetchRegistryHealth, RegistryHealthItem, RegistryHealthReport,
  RegistryHealthStatus, restoreRegistryEntry,
} from './api';
import { SoftwareGlyph } from './SoftwareShared';
import HistoryPanel from './HistoryPanel';

const STATUS: Record<RegistryHealthStatus, { label: string; color: string }> = {
  healthy: { label: '正常', color: 'var(--green)' },
  location_missing: { label: '目录缺失', color: 'var(--amber)' },
  uninstaller_missing: { label: '卸载器缺失', color: 'var(--amber)' },
  orphaned: { label: '失效登记', color: 'var(--red)' },
  incomplete: { label: '信息不足', color: 'var(--tx3)' },
};

type HealthFilter = 'all' | 'attention' | 'orphaned' | 'healthy';

export default function RegistryPage({
  focusEntityId,
  focusEntityName,
  onOpenSoftware,
}: {
  focusEntityId?: string | null;
  focusEntityName?: string | null;
  onOpenSoftware?: (entityId: string) => void;
}) {
  const { message, modal } = App.useApp();
  const [report, setReport] = useState<RegistryHealthReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const deferredQuery = useDeferredValue(query);
  const [filter, setFilter] = useState<HealthFilter>('attention');
  const [relatedOnly, setRelatedOnly] = useState(!!focusEntityId);

  useEffect(() => {
    if (!focusEntityId) return;
    setRelatedOnly(true);
    setFilter('all');
  }, [focusEntityId]);

  const load = async () => {
    setLoading(true);
    try {
      setReport(await fetchRegistryHealth());
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '注册表读取失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const clean = async (item: RegistryHealthItem) => {
    const preview = await cleanupRegistryEntry(item.id, true);
    if (!preview.ok) {
      message.error(preview.refused ?? '预演失败');
      return;
    }
    modal.confirm({
      title: `移除「${item.name}」的失效登记？`,
      okText: '确认移除',
      cancelText: '取消',
      content: '只移除当前用户的软件卸载登记，不删除软件文件。完整注册表键会先备份，可从本页恢复。',
      onOk: async () => {
        const result = await cleanupRegistryEntry(item.id, false);
        if (!result.ok) throw new Error(result.refused ?? '清理失败');
        message.success('失效登记已移除');
        await load();
      },
    });
  };

  const restore = async (operationId: string) => {
    const result = await restoreRegistryEntry(operationId);
    if (!result.ok) {
      message.error(result.refused ?? '恢复失败');
      return;
    }
    message.success('卸载登记已恢复');
    await load();
  };

  const items = useMemo(() => {
    const q = deferredQuery.trim().toLocaleLowerCase();
    return (report?.items ?? []).filter((item) => {
      if (relatedOnly && focusEntityId && item.entity?.entity_id !== focusEntityId) return false;
      if (filter === 'healthy' && item.status !== 'healthy') return false;
      if (filter === 'orphaned' && item.status !== 'orphaned') return false;
      if (filter === 'attention' && item.status === 'healthy') return false;
      return !q || [item.name, item.publisher, item.location, item.reason]
        .some((value) => value?.toLocaleLowerCase().includes(q));
    });
  }, [deferredQuery, filter, focusEntityId, relatedOnly, report?.items]);

  if (!report && loading) {
    return <div style={{ minHeight: 360, display: 'grid', placeItems: 'center' }}><Spin /></div>;
  }

  const summary = report?.summary;
  return (
    <div className="lp-page" style={{ padding: '22px 26px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, color: 'var(--tx)' }}>注册表巡检</h1>
          <div style={{ marginTop: 4, color: 'var(--tx2)', fontSize: 12.5 }}>
            卸载登记健康度与可恢复清理
          </div>
        </div>
        <Tooltip title="重新读取 Windows 卸载登记">
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()} style={{ marginLeft: 'auto' }} />
        </Tooltip>
      </div>

      {error && <Card size="small" style={{ marginBottom: 14, color: 'var(--red)' }}>{error}</Card>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginBottom: 14 }}>
        <Card size="small" className="lp-card-elevated"><Statistic title="登记总数" value={summary?.total ?? 0} prefix={<SafetyCertificateOutlined style={{ color: 'var(--accent-fg)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="已关联软件" value={summary?.associated ?? 0} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="状态正常" value={summary?.healthy ?? 0} prefix={<CheckCircleOutlined style={{ color: 'var(--green)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="需要留意" value={summary?.attention ?? 0} prefix={<WarningOutlined style={{ color: 'var(--amber)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="可清理失效项" value={summary?.manageable ?? 0} prefix={<ClearOutlined style={{ color: 'var(--red)' }} />} /></Card>
      </div>

      <Card size="small" className="lp-card-elevated">
        {focusEntityId && relatedOnly && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, padding: '8px 10px', border: '1px solid var(--line)', borderRadius: 8, background: 'var(--bg)' }}>
            <span style={{ color: 'var(--tx2)', flex: 1 }}>正在查看 {focusEntityName || '该软件'} 的注册表登记</span>
            <Button size="small" onClick={() => setRelatedOnly(false)}>查看全部登记</Button>
          </div>
        )}
        <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
          <Segmented
            value={filter}
            onChange={(value) => setFilter(value as HealthFilter)}
            options={[
              { label: '需留意', value: 'attention' },
              { label: '失效登记', value: 'orphaned' },
              { label: '正常', value: 'healthy' },
              { label: '全部', value: 'all' },
            ]}
          />
          <Input.Search
            allowClear
            placeholder="搜索软件、发布商或路径"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            style={{ maxWidth: 360, minWidth: 220 }}
          />
        </div>
        {items.length ? (
          <Table<RegistryHealthItem>
            rowKey="id"
            size="small"
            dataSource={items}
            pagination={{ pageSize: 25, showSizeChanger: false, showTotal: (total) => `共 ${total} 项` }}
            scroll={{ x: 900 }}
            expandable={{
              expandedRowRender: (item) => (
                <div style={{ padding: '4px 14px', display: 'grid', gap: 5, color: 'var(--tx2)', fontSize: 12 }}>
                  <div>{item.reason}</div>
                  <div>
                    <b style={{ color: 'var(--tx)' }}>登记路径：</b>
                    <code className="lp-mono" style={{ color: 'var(--tx3)' }}>{item.registry_path}</code>
                  </div>
                  {item.entity && (
                    <div>
                      <b style={{ color: 'var(--tx)' }}>关联依据：</b>
                      {item.entity.reason} · {Math.round(item.entity.confidence * 100)}%
                    </div>
                  )}
                  {item.location && <code className="lp-mono" style={{ color: 'var(--tx3)' }}>{item.location}</code>}
                </div>
              ),
            }}
            columns={[
              {
                title: '软件', dataIndex: 'name', width: 270,
                render: (name: string, item) => (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                    <SoftwareGlyph name={name} icon={item.entity?.icon} size={32} />
                    <div style={{ minWidth: 0 }}>
                      {item.entity && onOpenSoftware ? (
                        <Button type="link" size="small" onClick={() => onOpenSoftware(item.entity!.entity_id)} style={{ height: 'auto', padding: 0, color: 'var(--tx)', fontWeight: 600 }}>
                          {name}
                        </Button>
                      ) : <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--tx)', fontWeight: 600 }}>{name}</div>}
                      <div style={{ color: 'var(--tx3)', fontSize: 11.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.publisher || '未知发布商'}</div>
                    </div>
                  </div>
                ),
              },
              {
                title: '状态', width: 125,
                render: (_, item) => {
                  const status = STATUS[item.status];
                  return <Tag bordered style={{ color: status.color, borderColor: status.color, background: 'transparent' }}>{status.label}</Tag>;
                },
              },
              { title: '版本', dataIndex: 'version', width: 120, render: (value?: string) => value || '—' },
              { title: '范围', width: 100, render: (_, item) => <Tag bordered={false}>{item.scope === 'user' ? '当前用户' : '系统级'}</Tag> },
              {
                title: '操作', width: 110, align: 'right',
                render: (_, item) => item.can_clean
                  ? <Button size="small" danger icon={<ClearOutlined />} onClick={() => void clean(item)}>移除登记</Button>
                  : <span style={{ color: 'var(--tx3)' }}>只读</span>,
              },
            ]}
          />
        ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选下没有登记" />}
      </Card>

      {!!report?.removed.length && (
        <HistoryPanel
          title="已移除登记"
          items={report.removed}
          itemKey={(item) => item.operation_id}
          style={{ marginTop: 14 }}
          renderItem={(item) => (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 0', borderBottom: '1px solid var(--line)' }}>
              <span style={{ color: 'var(--tx)', flex: 1 }}>{item.name}</span>
              <Button size="small" icon={<UndoOutlined />} onClick={() => void restore(item.operation_id)}>恢复登记</Button>
            </div>
          )}
        />
      )}
    </div>
  );
}
