import { useEffect, useMemo, useState } from 'react';
import { Alert, App as AntdApp, Button, Drawer, Popconfirm, Tag, Tree } from 'antd';
import type { DataNode } from 'antd/es/tree';
import { CAT_COLOR, fmtSize, ignorePath, KIND_COLOR, KIND_LABEL, LpData, LpNode } from './api';
import { EvidenceBlock } from './SoftwareShared';

/**
 * 定性 → 状态点颜色。
 *
 * 原先这里是一张深色专用的字面量表（`#34d399` / `#f87171` / `#fbbf24` /
 * `#a78bfa`），连目录名与体积也写死成 `#dbe4ff` / `#7dd3fc`。**浅色主题下
 * 这一整页等于不可用**：实测目录名对 白底 只有 1.27:1、体积 1.67:1，而 WCAG
 * 正文要求 4.5:1——不是"浅一点"，是白纸上的浅灰。这一页恰好是用户逐目录读路径
 * 决定删不删的地方，看不清路径的后果不是难受而是删错。
 *
 * 现在一律走 `--dot-*` / `--tx` / `--cyan`，双主题各自取过线值（见 tokens.css）。
 */
const CAT_DOT: Record<string, string> = {
  可再生缓存: 'var(--dot-green)',
  可清理: 'var(--dot-green)',
  不可动: 'var(--dot-red)',
  混合: 'var(--dot-amber)',
  容器: 'var(--dot-purple)',
};

function dotColor(n: LpNode, depth: number): string {
  if (depth === 0 && !n.owner) return 'var(--dot-red)';
  if (n.cat && CAT_DOT[n.cat]) return CAT_DOT[n.cat];
  return n.owner ? 'var(--dot-blue)' : 'var(--tx3)';
}

function toTreeData(items: LpNode[]): DataNode[] {
  const rootMax = Math.max(1, ...items.map((item) => item.size ?? 0));
  const conv = (n: LpNode, depth: number): DataNode => {
    const size = n.size ?? 0;
    const ratio = size === 0
      ? 0
      : Math.min(100, Math.max(4, Math.round((size / rootMax) * 100)));
    return {
      key: n.path,
      title: (
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 8,
            width: '100%',
            minWidth: 620,
            padding: '2px 0',
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: 4,
              background: dotColor(n, depth),
              boxShadow: `0 0 6px color-mix(in srgb, ${dotColor(n, depth)} 45%, transparent)`,
              display: 'inline-block',
              flexShrink: 0,
            }}
          />
          <b className="lp-num" style={{ minWidth: 80, color: 'var(--cyan)', fontSize: 'var(--fs-sm)' }}>
            {fmtSize(n.size)}
          </b>
          {/* 紧凑内联体积占比条 */}
          <span
            style={{
              width: 54,
              height: 4,
              borderRadius: 2,
              background: 'var(--line)',
              display: 'inline-flex',
              overflow: 'hidden',
              flexShrink: 0,
            }}
          >
            <span
              style={{
                width: `${ratio}%`,
                background: 'var(--blue2)',
                borderRadius: 2,
              }}
            />
          </span>
          <span style={{ color: depth === 0 && !n.owner ? 'var(--red)' : 'var(--tx)', fontWeight: depth === 0 ? 600 : 400 }}>
            {n.name}
          </span>
          {depth === 0 && !n.owner && <Tag color="red" bordered={false}>未归因</Tag>}
          {depth === 0 && n.owner && n.owner !== n.name && (
            <Tag color={KIND_COLOR[n.owner_kind ?? 'unknown']} bordered={false}>
              {n.owner_kind === 'vendor' ? KIND_LABEL[n.owner_kind] : n.owner}
            </Tag>
          )}
          {n.role && <Tag bordered={false}>{n.role}</Tag>}
          {n.redirect && <Tag color="orange" bordered={false}>junction</Tag>}
          {(n.children?.length ?? 0) > 0 && (
            <span style={{ color: 'var(--tx3)', fontSize: 'var(--fs-xs)', marginLeft: 'auto' }}>
              +{n.children!.length} 子目录
            </span>
          )}
        </span>
      ),
      children: [...(n.children ?? [])]
        .sort((a, b) => (b.size ?? 0) - (a.size ?? 0))
        .map((c) => conv(c, depth + 1)),
    };
  };
  return [...items]
    .sort((a, b) => (b.size ?? 0) - (a.size ?? 0))
    .map((it) => conv(it, 0));
}

export default function DiskTreePage({
  data,
  onOpenOwner,
  onRulesChanged,
}: {
  data: LpData;
  onOpenOwner: (name?: string | null) => boolean;
  onRulesChanged?: () => void;
}) {
  const { message } = AntdApp.useApp();
  const treeData = useMemo(() => toTreeData(data.items), [data]);
  const pathIndex = useMemo(() => {
    const m = new Map<string, LpNode>();
    const walk = (n: LpNode) => {
      m.set(n.path, n);
      n.children?.forEach(walk);
    };
    data.items.forEach(walk);
    return m;
  }, [data]);
  const [picked, setPicked] = useState<LpNode | null>(null);
  const [treeHeight, setTreeHeight] = useState(() => Math.max(320, window.innerHeight - 190));
  useEffect(() => {
    const resize = () => setTreeHeight(Math.max(320, window.innerHeight - 190));
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);

  return (
    <div style={{ padding: 16, height: '100%', overflowY: 'auto' }}>
      <Alert
        style={{ marginBottom: 12 }}
        type="info"
        showIcon={false}
        message="按体积降序排列。选择目录可查看证据、所属软件或添加保留规则；红色标记为未归因目录，绿色标记为可再生缓存。"
      />
      <Tree
        blockNode
        height={treeHeight}
        virtual
        defaultExpandedKeys={[data.items[0]?.path].filter(Boolean) as string[]}
        treeData={treeData}
        onSelect={(keys) => {
          const path = keys[0] as string | undefined;
          if (!path) return;
          const node = pathIndex.get(path);
          if (!node) return;
          setPicked(node);
        }}
      />
      <Drawer open={!!picked} onClose={() => setPicked(null)} width={560} title={picked?.path}>
        {picked && (
          <>
            <EvidenceBlock node={picked} />
            <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--line)', display: 'flex', alignItems: 'center', gap: 10 }}>
              {picked.owner && (
                <Button
                  size="small"
                  type="primary"
                  onClick={() => {
                    if (onOpenOwner(picked.owner)) setPicked(null);
                  }}
                >
                  查看所属软件
                </Button>
              )}
              <Popconfirm
                title="保留这条路径？"
                description="它和下面的子目录将不再进入清理或迁移计划。"
                okText="保留"
                cancelText="取消"
                onConfirm={async () => {
                  try {
                    await ignorePath(picked.path, '用户在磁盘全景中手动保留');
                    message.success('已保留此路径，下一次出计划时生效');
                    onRulesChanged?.();
                    setPicked(null);
                  } catch (e) {
                    message.error(e instanceof Error ? e.message : '保存规则失败');
                  }
                }}
              >
                <Button size="small">保留此路径</Button>
              </Popconfirm>
              <span style={{ color: 'var(--tx3)', fontSize: 'var(--fs-xs)' }}>
                规则只阻止后续操作，不会修改现有文件
              </span>
            </div>
          </>
        )}
      </Drawer>
    </div>
  );
}
