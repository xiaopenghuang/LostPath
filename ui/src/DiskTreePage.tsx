import { useMemo, useState } from 'react';
import { Alert, Drawer, Tag, Tree } from 'antd';
import type { DataNode } from 'antd/es/tree';
import { CAT_COLOR, fmtSize, KIND_COLOR, KIND_LABEL, LpData, LpNode } from './api';
import { EvidenceBlock } from './SoftwarePage';

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
  const conv = (n: LpNode, depth: number): DataNode => ({
    key: n.path,
    title: (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, minWidth: 500 }}>
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: 4,
            background: dotColor(n, depth),
            // 原先是 `0 0 8px ${dotColor()}55` —— 往颜色字面量尾部拼 alpha 的
            // 写法在改成 var() 之后失效（`var(--x)55` 不是合法颜色，整条
            // box-shadow 被丢弃，静默无光晕）。改用 currentColor 无法覆盖此处，
            // 故直接给固定的柔光：点本身已过 3:1，光晕纯装饰。
            boxShadow: '0 0 6px rgba(127, 145, 180, 0.45)',
            display: 'inline-block',
            flexShrink: 0,
          }}
        />
        <b className="lp-num" style={{ minWidth: 86, color: 'var(--cyan)' }}>{fmtSize(n.size)}</b>
        <span style={{ color: depth === 0 && !n.owner ? 'var(--red)' : 'var(--tx)' }}>{n.name}</span>
        {depth === 0 && !n.owner && <Tag color="red" bordered={false}>未归因</Tag>}
        {depth === 0 && n.owner && n.owner !== n.name && (
          <Tag color={KIND_COLOR[n.owner_kind ?? 'unknown']} bordered={false}>
            {n.owner_kind === 'vendor' ? KIND_LABEL[n.owner_kind] : n.owner}
          </Tag>
        )}
        {n.role && <Tag bordered={false}>{n.role}</Tag>}
        {n.redirect && <Tag color="orange" bordered={false}>junction</Tag>}
        {(n.children?.length ?? 0) > 0 && (
          <span style={{ color: 'var(--tx3)', fontSize: 12 }}>+{n.children!.length} 子目录</span>
        )}
      </span>
    ),
    children: [...(n.children ?? [])]
      .sort((a, b) => (b.size ?? 0) - (a.size ?? 0))
      .map((c) => conv(c, depth + 1)),
  });
  return [...items]
    .sort((a, b) => (b.size ?? 0) - (a.size ?? 0))
    .map((it) => conv(it, 0));
}

export default function DiskTreePage({
  data,
  onOpenOwner,
}: {
  data: LpData;
  onOpenOwner: (name?: string | null) => boolean;
}) {
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

  return (
    <div style={{ padding: 16, height: '100%', overflowY: 'auto' }}>
      <Alert
        style={{ marginBottom: 12 }}
        type="info"
        showIcon={false}
        message="按体积降序排列。点击已归因目录可跳转至对应软件；红色标记为未归因目录，绿色标记为可再生缓存。"
      />
      <Tree
        blockNode
        defaultExpandedKeys={[data.items[0]?.path].filter(Boolean) as string[]}
        treeData={treeData}
        onSelect={(keys) => {
          const path = keys[0] as string | undefined;
          if (!path) return;
          const node = pathIndex.get(path);
          if (!node) return;
          if (node.owner) onOpenOwner(node.owner);
          else setPicked(node);
        }}
      />
      <Drawer open={!!picked} onClose={() => setPicked(null)} width={560} title={picked?.path}>
        {picked && <EvidenceBlock node={picked} />}
      </Drawer>
    </div>
  );
}
