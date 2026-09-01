import { useDeferredValue, useEffect, useMemo, useState } from 'react';
import {
  App, Button, Card, Checkbox, Empty, Input, Modal, Segmented, Space, Spin, Statistic,
  Table, Tag, Tooltip,
} from 'antd';
import {
  ApiOutlined, AppstoreOutlined, DeleteOutlined, FolderOpenOutlined, LinkOutlined,
  MenuOutlined, PlusOutlined, PoweroffOutlined, ReloadOutlined,
  SafetyCertificateOutlined, UndoOutlined,
} from '@ant-design/icons';
import {
  ContextMenuItem, ContextMenuKind, ContextMenuReport, createContextMenu,
  deleteContextMenu, disableContextMenu, fetchContextMenus, restoreContextMenu,
} from './api';
import { SoftwareGlyph } from './SoftwareShared';
import HistoryPanel from './HistoryPanel';

type KindFilter = 'all' | ContextMenuKind;
type StateFilter = 'all' | 'active' | 'disabled';

const KIND_LABEL: Record<ContextMenuKind, string> = {
  command: '菜单命令',
  handler: '扩展处理器',
};

export default function ContextMenuPage({
  focusEntityId,
  focusEntityName,
  onOpenSoftware,
}: {
  focusEntityId?: string | null;
  focusEntityName?: string | null;
  onOpenSoftware?: (entityId: string) => void;
}) {
  const { message, modal } = App.useApp();
  const [report, setReport] = useState<ContextMenuReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const deferredQuery = useDeferredValue(query);
  const [kind, setKind] = useState<KindFilter>('all');
  const [state, setState] = useState<StateFilter>('active');
  const [relatedOnly, setRelatedOnly] = useState(!!focusEntityId);
  const [creatorOpen, setCreatorOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [customName, setCustomName] = useState('');
  const [customExecutable, setCustomExecutable] = useState('');
  const [customSurfaces, setCustomSurfaces] = useState<string[]>([
    'folder', 'folder_background',
  ]);

  useEffect(() => {
    if (!focusEntityId) return;
    setRelatedOnly(true);
    setKind('all');
    setState('all');
  }, [focusEntityId]);

  const load = async (refresh = false) => {
    setLoading(true);
    try {
      setReport(await fetchContextMenus(refresh));
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : '右键菜单读取失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const disable = async (item: ContextMenuItem) => {
    const preview = await disableContextMenu(item.id, true);
    if (!preview.ok) {
      message.error(preview.refused ?? '预演失败');
      return;
    }
    const surfaceText = item.surfaces.map((surface) => surface.name).join('、');
    modal.confirm({
      title: `禁用「${item.name}」？`,
      okText: '确认禁用',
      cancelText: '取消',
      content: item.kind === 'handler'
        ? `该扩展会从 ${surfaceText} 的右键菜单中一起隐藏。只写入当前用户级阻止标记，可随时恢复。`
        : `该命令会从 ${surfaceText} 的右键菜单中隐藏。原命令和程序文件不会删除。`,
      onOk: async () => {
        const result = await disableContextMenu(item.id, false);
        if (!result.ok) throw new Error(result.refused ?? '禁用失败');
        message.success('右键菜单已禁用');
        await load(true);
      },
    });
  };

  const restore = async (item: ContextMenuItem) => {
    const operationId = item.manage.action_id;
    if (!operationId) return;
    const result = await restoreContextMenu(operationId);
    if (!result.ok) {
      message.error(result.refused ?? '恢复失败');
      return;
    }
    message.success('右键菜单已恢复');
    await load(true);
  };

  const pickExecutable = async () => {
    const path = await window.lostpath?.pickExecutable();
    if (path) setCustomExecutable(path);
  };

  const openCreator = () => {
    setCustomName('');
    setCustomExecutable('');
    setCustomSurfaces(['folder', 'folder_background']);
    setCreatorOpen(true);
  };

  const prepareCreate = async () => {
    if (!customName.trim() || !customExecutable.trim() || !customSurfaces.length) {
      message.error('请填写菜单名称、程序路径并选择出现位置');
      return;
    }
    setCreating(true);
    try {
      const preview = await createContextMenu(
        customName.trim(), customExecutable.trim(), customSurfaces, true,
      );
      if (!preview.ok) {
        message.error(preview.refused ?? '预演失败');
        return;
      }
      const selected = customSurfaces.map((surface) => ({
        files: '所有文件', folder: '文件夹',
        folder_background: '文件夹空白处', drive: '磁盘',
      }[surface])).filter(Boolean).join('、');
      modal.confirm({
        title: `添加「${customName.trim()}」？`,
        okText: '确认添加',
        cancelText: '返回修改',
        content: `将在 ${selected} 中创建当前用户级菜单。LostPath 会自动传入所选文件或目录路径。`,
        onOk: async () => {
          const result = await createContextMenu(
            customName.trim(), customExecutable.trim(), customSurfaces, false,
          );
          if (!result.ok) throw new Error(result.refused ?? '添加失败');
          setCreatorOpen(false);
          message.success('自定义右键菜单已添加');
          await load(true);
        },
      });
    } finally {
      setCreating(false);
    }
  };

  const remove = async (item: ContextMenuItem) => {
    const preview = await deleteContextMenu(item.id, true);
    if (!preview.ok) {
      message.error(preview.refused ?? '预演失败');
      return;
    }
    modal.confirm({
      title: `删除自定义菜单「${item.name}」？`,
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      content: '只删除这一处 LostPath 创建的菜单。完整注册表键会先备份，可从本页恢复。',
      onOk: async () => {
        const result = await deleteContextMenu(item.id, false);
        if (!result.ok) throw new Error(result.refused ?? '删除失败');
        message.success('自定义右键菜单已删除');
        await load(true);
      },
    });
  };

  const restoreRemoved = async (operationId: string) => {
    const result = await restoreContextMenu(operationId);
    if (!result.ok) {
      message.error(result.refused ?? '恢复失败');
      return;
    }
    message.success('自定义右键菜单已恢复');
    await load(true);
  };

  const items = useMemo(() => {
    const q = deferredQuery.trim().toLocaleLowerCase();
    return (report?.items ?? []).filter((item) => {
      if (relatedOnly && focusEntityId && item.entity?.entity_id !== focusEntityId) return false;
      if (kind !== 'all' && item.kind !== kind) return false;
      if (state === 'active' && item.manage.disabled) return false;
      if (state === 'disabled' && !item.manage.disabled) return false;
      return !q || [
        item.name,
        item.provider,
        item.target,
        item.entity?.name,
        ...item.surfaces.map((surface) => surface.name),
      ].some((value) => value?.toLocaleLowerCase().includes(q));
    });
  }, [deferredQuery, focusEntityId, kind, relatedOnly, report?.items, state]);

  if (!report && loading) {
    return <div style={{ minHeight: 360, display: 'grid', placeItems: 'center' }}><Spin /></div>;
  }

  const summary = report?.summary;
  return (
    <div className="lp-page" style={{ padding: '22px 26px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, color: 'var(--tx)' }}>右键管理</h1>
          <div style={{ marginTop: 4, color: 'var(--tx2)', fontSize: 'var(--fs-sm)' }}>
            管理资源管理器菜单命令与第三方扩展
          </div>
        </div>
        <Tooltip title="重新读取右键菜单注册表">
          <Button
            aria-label="重新读取右键菜单注册表"
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => void load(true)}
            style={{ marginLeft: 'auto' }}
          />
        </Tooltip>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreator}>添加菜单</Button>
      </div>

      {error && <Card size="small" style={{ marginBottom: 14, color: 'var(--red)' }}>{error}</Card>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginBottom: 14 }}>
        <Card size="small" className="lp-card-elevated"><Statistic title="菜单项" value={summary?.total ?? 0} prefix={<MenuOutlined style={{ color: 'var(--accent-fg)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="扩展处理器" value={summary?.handlers ?? 0} prefix={<ApiOutlined style={{ color: 'var(--green)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="已关联软件" value={summary?.associated ?? 0} prefix={<LinkOutlined style={{ color: 'var(--accent-fg)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="自定义菜单" value={summary?.custom ?? 0} prefix={<PlusOutlined style={{ color: 'var(--green)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="可禁用" value={summary?.manageable ?? 0} prefix={<PoweroffOutlined style={{ color: 'var(--amber)' }} />} /></Card>
        <Card size="small" className="lp-card-elevated"><Statistic title="系统保护" value={summary?.protected ?? 0} prefix={<SafetyCertificateOutlined style={{ color: 'var(--tx2)' }} />} /></Card>
      </div>

      <Card size="small" className="lp-card-elevated">
        {focusEntityId && relatedOnly && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, padding: '8px 10px', border: '1px solid var(--line)', borderRadius: 'var(--radius-sm)', background: 'var(--bg)' }}>
            <span style={{ color: 'var(--tx2)', flex: 1 }}>正在查看 {focusEntityName || '该软件'} 的右键菜单</span>
            <Button size="small" onClick={() => setRelatedOnly(false)}>查看全部菜单</Button>
          </div>
        )}
        <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
          <Segmented
            value={kind}
            onChange={(value) => setKind(value as KindFilter)}
            options={[
              { label: '全部类型', value: 'all' },
              { label: '菜单命令', value: 'command' },
              { label: '扩展处理器', value: 'handler' },
            ]}
          />
          <Segmented
            value={state}
            onChange={(value) => setState(value as StateFilter)}
            options={[
              { label: '启用中', value: 'active' },
              { label: '已禁用', value: 'disabled' },
              { label: '全部状态', value: 'all' },
            ]}
          />
          <Input.Search
            allowClear
            placeholder="搜索菜单、软件或目标文件"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            style={{ maxWidth: 360, minWidth: 240 }}
          />
        </div>

        {items.length ? (
          <Table<ContextMenuItem>
            rowKey="id"
            size="small"
            dataSource={items}
            pagination={{ pageSize: 25, showSizeChanger: false, showTotal: (total) => `共 ${total} 项` }}
            scroll={{ x: 940 }}
            expandable={{
              expandedRowRender: (item) => (
                <div style={{ padding: '4px 14px', display: 'grid', gap: 6, color: 'var(--tx2)', fontSize: 'var(--fs-sm)' }}>
                  <div>{item.manage.reason}</div>
                  {item.entity && <div><b style={{ color: 'var(--tx)' }}>关联依据：</b>{item.entity.reason} · {Math.round(item.entity.confidence * 100)}%</div>}
                  {item.target && <div><b style={{ color: 'var(--tx)' }}>扩展文件：</b><code className="lp-mono" style={{ color: 'var(--tx3)' }}>{item.target}</code></div>}
                  {item.registry_paths.map((path) => <code key={path} className="lp-mono" style={{ color: 'var(--tx3)' }}>{path}</code>)}
                </div>
              ),
            }}
            columns={[
              {
                title: '菜单项', width: 285,
                render: (_, item) => (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                    <SoftwareGlyph name={item.entity?.name || item.name} icon={item.entity?.icon} size={32} />
                    <div style={{ minWidth: 0 }}>
                      <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--tx)', fontWeight: 600 }}>{item.name}</div>
                      {item.entity && onOpenSoftware ? (
                        <Button type="link" size="small" icon={<AppstoreOutlined />} onClick={() => onOpenSoftware(item.entity!.entity_id)} style={{ height: 'auto', padding: 0, fontSize: 'var(--fs-xs)' }}>
                          {item.entity.name}
                        </Button>
                      ) : <div style={{ color: 'var(--tx3)', fontSize: 'var(--fs-xs)' }}>未关联软件台账</div>}
                    </div>
                  </div>
                ),
              },
              {
                title: '类型', width: 120,
                render: (_, item) => (
                  <Tag bordered style={{ color: item.kind === 'handler' ? 'var(--green)' : 'var(--accent-fg)', borderColor: item.kind === 'handler' ? 'var(--green)' : 'var(--accent-fg)', background: 'transparent' }}>
                    {item.custom ? '自定义命令' : KIND_LABEL[item.kind]}
                  </Tag>
                ),
              },
              {
                title: '出现位置', width: 245,
                render: (_, item) => (
                  <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                    {item.surfaces.slice(0, 3).map((surface) => <Tag key={surface.id} bordered={false}>{surface.name}</Tag>)}
                    {item.surfaces.length > 3 && <Tag bordered={false}>+{item.surfaces.length - 3}</Tag>}
                  </div>
                ),
              },
              {
                title: '状态', width: 120,
                render: (_, item) => item.manage.disabled
                  ? <Tag bordered style={{ color: 'var(--tx3)', borderColor: 'var(--line2)', background: 'transparent' }}>已禁用</Tag>
                  : item.system_component
                    ? <Tag bordered style={{ color: 'var(--tx2)', borderColor: 'var(--line2)', background: 'transparent' }}>系统保护</Tag>
                    : <Tag bordered style={{ color: 'var(--green)', borderColor: 'var(--green)', background: 'transparent' }}>启用中</Tag>,
              },
              {
                title: '操作', width: 170, align: 'right',
                render: (_, item) => (
                  <Space size={6}>
                    {item.manage.can_restore
                      ? <Button size="small" icon={<UndoOutlined />} onClick={() => void restore(item)}>恢复</Button>
                      : item.manage.can_disable
                        ? <Button size="small" icon={<PoweroffOutlined />} onClick={() => void disable(item)}>禁用</Button>
                        : <Tooltip title={item.manage.reason}><span style={{ color: 'var(--tx3)' }}>{item.manage.external ? '外部禁用' : '只读'}</span></Tooltip>}
                    {item.manage.can_delete && (
                      <Tooltip title="删除这条自定义菜单">
                        <Button aria-label={`删除 ${item.name}`} size="small" danger icon={<DeleteOutlined />} onClick={() => void remove(item)} />
                      </Tooltip>
                    )}
                  </Space>
                ),
              },
            ]}
          />
        ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选下没有右键菜单项" />}
      </Card>

      {!!report?.removed.length && (
        <HistoryPanel
          title="已删除自定义菜单"
          items={report.removed}
          itemKey={(item) => item.operation_id}
          style={{ marginTop: 14 }}
          renderItem={(item) => (
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 0', borderBottom: '1px solid var(--line)' }}>
              <span style={{ color: 'var(--tx)', flex: 1 }}>{item.name}</span>
              <span style={{ color: 'var(--tx3)', fontSize: 'var(--fs-sm)' }}>{item.surface || '右键菜单'}</span>
              <Button size="small" icon={<UndoOutlined />} onClick={() => void restoreRemoved(item.operation_id)}>恢复</Button>
            </div>
          )}
        />
      )}

      <Modal
        open={creatorOpen}
        title="添加自定义右键菜单"
        onCancel={() => setCreatorOpen(false)}
        footer={[
          <Button key="cancel" onClick={() => setCreatorOpen(false)}>取消</Button>,
          <Button key="create" type="primary" loading={creating} onClick={() => void prepareCreate()}>
            预演并添加
          </Button>,
        ]}
        width={580}
      >
        <div style={{ display: 'grid', gap: 16, paddingTop: 8 }}>
          <div>
            <label htmlFor="context-menu-name" style={{ display: 'block', marginBottom: 6, color: 'var(--tx)', fontWeight: 600 }}>菜单名称</label>
            <Input
              id="context-menu-name"
              value={customName}
              maxLength={80}
              placeholder="例如 使用 VS Code 打开"
              onChange={(event) => setCustomName(event.target.value)}
            />
          </div>
          <div>
            <label htmlFor="context-menu-executable" style={{ display: 'block', marginBottom: 6, color: 'var(--tx)', fontWeight: 600 }}>启动程序</label>
            <Space.Compact style={{ width: '100%' }}>
              <Input
                id="context-menu-executable"
                value={customExecutable}
                placeholder="选择或粘贴 exe 的完整路径"
                onChange={(event) => setCustomExecutable(event.target.value)}
              />
              <Button icon={<FolderOpenOutlined />} disabled={!window.lostpath?.pickExecutable} onClick={() => void pickExecutable()}>浏览</Button>
            </Space.Compact>
          </div>
          <div>
            <div style={{ marginBottom: 8, color: 'var(--tx)', fontWeight: 600 }}>出现位置</div>
            <Checkbox.Group
              value={customSurfaces}
              onChange={(values) => setCustomSurfaces(values.map(String))}
              options={[
                { label: '所有文件', value: 'files' },
                { label: '文件夹', value: 'folder' },
                { label: '文件夹空白处', value: 'folder_background' },
                { label: '磁盘', value: 'drive' },
              ]}
            />
            <div style={{ marginTop: 8, color: 'var(--tx3)', fontSize: 'var(--fs-sm)' }}>
              LostPath 会根据位置自动传入所选文件、目录或当前文件夹路径，不需要手写命令参数。
            </div>
          </div>
        </div>
      </Modal>
    </div>
  );
}
