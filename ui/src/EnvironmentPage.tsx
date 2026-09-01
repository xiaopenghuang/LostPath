import { useDeferredValue, useEffect, useMemo, useState } from 'react';
import {
  App, Button, Card, Empty, Input, Modal, Segmented, Spin, Statistic, Table, Tag, Tooltip,
} from 'antd';
import {
  DeleteOutlined, EditOutlined, EyeInvisibleOutlined, PlusOutlined, ReloadOutlined,
  SafetyCertificateOutlined, UndoOutlined, UserOutlined,
} from '@ant-design/icons';
import {
  deleteEnvironmentValue, EnvironmentItem, EnvironmentReport, fetchEnvironment,
  restoreEnvironmentValue, setEnvironmentValue,
} from './api';
import { SoftwareGlyph } from './SoftwareShared';
import HistoryPanel from './HistoryPanel';

type ScopeFilter = 'all' | 'user' | 'machine';

export default function EnvironmentPage({
  focusEntityId,
  focusEntityName,
  onOpenSoftware,
}: {
  focusEntityId?: string | null;
  focusEntityName?: string | null;
  onOpenSoftware?: (entityId: string) => void;
}) {
  const { message, modal } = App.useApp();
  const [report, setReport] = useState<EnvironmentReport | null>(null);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const deferredQuery = useDeferredValue(query);
  const [scope, setScope] = useState<ScopeFilter>('all');
  const [relatedOnly, setRelatedOnly] = useState(!!focusEntityId);
  const [loading, setLoading] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<EnvironmentItem | null>(null);
  const [draftName, setDraftName] = useState('');
  const [draftValue, setDraftValue] = useState('');

  useEffect(() => {
    if (!focusEntityId) return;
    setRelatedOnly(true);
    setScope('all');
  }, [focusEntityId]);

  const load = async () => {
    setLoading(true);
    try {
      setReport(await fetchEnvironment());
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '环境变量读取失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const openCreate = () => {
    setEditing(null);
    setDraftName('');
    setDraftValue('');
    setEditorOpen(true);
  };

  const openEdit = (item: EnvironmentItem) => {
    setEditing(item);
    setDraftName(item.name);
    setDraftValue(item.value ?? '');
    setEditorOpen(true);
  };

  const saveDraft = async () => {
    const name = draftName.trim();
    if (!name) {
      message.error('请输入变量名');
      return;
    }
    const preview = await setEnvironmentValue(
      name, draftValue, editing?.fingerprint ?? null, true,
    );
    if (!preview.ok) {
      message.error(preview.refused ?? '预演失败');
      return;
    }
    modal.confirm({
      title: editing ? `保存 ${name} 的新值？` : `新增环境变量 ${name}？`,
      okText: '确认保存',
      cancelText: '取消',
      content: editing?.masked
        ? '原值属于敏感信息，界面未读取；保存后将直接替换。'
        : '修改只影响当前用户，新启动的程序会读取新值。',
      onOk: async () => {
        const result = await setEnvironmentValue(
          name, draftValue, editing?.fingerprint ?? null, false,
        );
        if (!result.ok) throw new Error(result.refused ?? '保存失败');
        setEditorOpen(false);
        message.success('环境变量已保存');
        await load();
      },
    });
  };

  const remove = async (item: EnvironmentItem) => {
    const preview = await deleteEnvironmentValue(item.name, item.fingerprint, true);
    if (!preview.ok) {
      message.error(preview.refused ?? '预演失败');
      return;
    }
    modal.confirm({
      title: `删除环境变量 ${item.name}？`,
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      content: '原值会写入本地操作记录，可从本页最近变更中撤销。',
      onOk: async () => {
        const result = await deleteEnvironmentValue(item.name, item.fingerprint, false);
        if (!result.ok) throw new Error(result.refused ?? '删除失败');
        message.success('环境变量已删除');
        await load();
      },
    });
  };

  const restore = async (operationId: string) => {
    const result = await restoreEnvironmentValue(operationId);
    if (!result.ok) {
      message.error(result.refused ?? '撤销失败');
      return;
    }
    message.success('已恢复到修改前');
    await load();
  };

  const items = useMemo(() => {
    const q = deferredQuery.trim().toLocaleLowerCase();
    return (report?.items ?? []).filter((item) => {
      if (relatedOnly && focusEntityId
        && !item.relations.some((relation) => relation.entity_id === focusEntityId)) return false;
      if (scope !== 'all' && item.scope !== scope) return false;
      return !q || item.name.toLocaleLowerCase().includes(q)
        || (!item.masked && item.preview.toLocaleLowerCase().includes(q))
        || item.relations.some((relation) => relation.name.toLocaleLowerCase().includes(q));
    });
  }, [deferredQuery, focusEntityId, relatedOnly, report?.items, scope]);

  if (!report && loading) {
    return <div style={{ minHeight: 360, display: 'grid', placeItems: 'center' }}><Spin /></div>;
  }

  const summary = report?.summary;
  return (
    <div className="lp-page" style={{ padding: '22px 26px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, color: 'var(--tx)' }}>环境变量</h1>
          <div style={{ marginTop: 4, color: 'var(--tx2)', fontSize: 12.5 }}>
            当前用户可编辑，系统级变量保持只读
          </div>
        </div>
        <Tooltip title="重新读取注册表中的环境变量">
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()} style={{ marginLeft: 'auto' }} />
        </Tooltip>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增变量</Button>
      </div>

      {error && <Card size="small" style={{ marginBottom: 14, color: 'var(--red)' }}>{error}</Card>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginBottom: 14 }}>
        <Card size="small" className="lp-card-elevated"><Statistic title="当前用户" value={summary?.user ?? 0} prefix={<UserOutlined style={{ color: 'var(--accent-fg)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="系统级" value={summary?.machine ?? 0} prefix={<SafetyCertificateOutlined style={{ color: 'var(--green)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="关联软件变量" value={summary?.associated ?? 0} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="覆盖系统值" value={summary?.overrides ?? 0} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="敏感值已隐藏" value={summary?.masked ?? 0} /></Card>
      </div>

      <Card size="small" className="lp-card-elevated">
        {focusEntityId && relatedOnly && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, padding: '8px 10px', border: '1px solid var(--line)', borderRadius: 8, background: 'var(--bg)' }}>
            <span style={{ color: 'var(--tx2)', flex: 1 }}>正在查看 {focusEntityName || '该软件'} 使用的环境变量</span>
            <Button size="small" onClick={() => setRelatedOnly(false)}>查看全部变量</Button>
          </div>
        )}
        <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
          <Segmented
            value={scope}
            onChange={(value) => setScope(value as ScopeFilter)}
            options={[{ label: '全部', value: 'all' }, { label: '当前用户', value: 'user' }, { label: '系统级', value: 'machine' }]}
          />
          <Input.Search
            allowClear
            placeholder="搜索变量名、值或关联软件"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            style={{ maxWidth: 360, minWidth: 220 }}
          />
        </div>
        {items.length ? (
          <Table<EnvironmentItem>
            rowKey="id"
            size="small"
            dataSource={items}
            pagination={{ pageSize: 30, showSizeChanger: false, showTotal: (total) => `共 ${total} 项` }}
            scroll={{ x: 1040 }}
            columns={[
              {
                title: '变量名', dataIndex: 'name', width: 220,
                render: (name: string, item) => <span style={{ fontWeight: 600, color: 'var(--tx)' }}>{name}{item.overridden && <Tag bordered={false} style={{ marginLeft: 8 }}>被覆盖</Tag>}</span>,
              },
              {
                title: '值', dataIndex: 'preview', ellipsis: true,
                render: (value: string, item) => (
                  <code className="lp-mono" style={{ color: item.masked ? 'var(--tx3)' : 'var(--tx2)' }}>
                    {item.masked && <EyeInvisibleOutlined style={{ marginRight: 7 }} />}{value || '空值'}
                  </code>
                ),
              },
              {
                title: '关联软件', width: 250,
                render: (_, item) => item.relations.length ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                    {item.relations.slice(0, 2).map((relation) => (
                      <Tooltip key={relation.entity_id} title={`${relation.reason} · ${Math.round(relation.confidence * 100)}%`}>
                        <Button
                          size="small"
                          type="text"
                          onClick={() => onOpenSoftware?.(relation.entity_id)}
                          style={{ display: 'inline-flex', alignItems: 'center', gap: 6, maxWidth: 112, paddingInline: 4 }}
                        >
                          <SoftwareGlyph name={relation.name} icon={relation.icon} size={22} />
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{relation.name}</span>
                        </Button>
                      </Tooltip>
                    ))}
                    {item.relations.length > 2 && <Tag bordered={false}>+{item.relations.length - 2}</Tag>}
                  </div>
                ) : <span style={{ color: 'var(--tx3)' }}>通用或未识别</span>,
              },
              { title: '范围', width: 100, render: (_, item) => <Tag bordered={false}>{item.scope === 'user' ? '当前用户' : '系统级'}</Tag> },
              {
                title: '操作', width: 105, align: 'right',
                render: (_, item) => item.editable ? (
                  <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 4 }}>
                    <Tooltip title="编辑"><Button size="small" type="text" icon={<EditOutlined />} onClick={() => openEdit(item)} /></Tooltip>
                    <Tooltip title="删除"><Button size="small" danger type="text" icon={<DeleteOutlined />} onClick={() => void remove(item)} /></Tooltip>
                  </div>
                ) : <span style={{ color: 'var(--tx3)' }}>只读</span>,
              },
            ]}
          />
        ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的环境变量" />}
      </Card>

      {!!report?.changes.length && (
        <HistoryPanel
          title="最近变更"
          items={report.changes}
          itemKey={(change) => change.id}
          style={{ marginTop: 14 }}
          renderItem={(change) => (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '7px 0', borderBottom: '1px solid var(--line)' }}>
              <code className="lp-mono" style={{ color: 'var(--tx)', flex: 1 }}>{change.env_var}</code>
              <Tag bordered={false}>{change.action === 'env_delete' ? '已删除' : '已修改'}</Tag>
              {change.status === 'done' && <Button size="small" icon={<UndoOutlined />} onClick={() => void restore(change.id)}>撤销</Button>}
              {change.status === 'rolled_back' && <span style={{ color: 'var(--tx3)' }}>已撤销</span>}
            </div>
          )}
        />
      )}

      <Modal
        open={editorOpen}
        title={editing ? `编辑 ${editing.name}` : '新增环境变量'}
        okText="保存"
        cancelText="取消"
        onOk={() => void saveDraft()}
        onCancel={() => setEditorOpen(false)}
        destroyOnClose
      >
        <div style={{ display: 'grid', gap: 14, paddingTop: 8 }}>
          <label style={{ display: 'grid', gap: 6, color: 'var(--tx2)' }}>
            变量名
            <Input value={draftName} disabled={!!editing} onChange={(event) => setDraftName(event.target.value)} />
          </label>
          <label style={{ display: 'grid', gap: 6, color: 'var(--tx2)' }}>
            变量值
            <Input.TextArea value={draftValue} autoSize={{ minRows: 3, maxRows: 8 }} onChange={(event) => setDraftValue(event.target.value)} />
          </label>
        </div>
      </Modal>
    </div>
  );
}
