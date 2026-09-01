import { memo, useEffect, useMemo, useState } from 'react';
import { Alert, Table, Tag } from 'antd';
import { fmtSize, LpNode, SoftwareEntity } from './api';

const pct = (confidence?: number | null) => (
  confidence == null ? '—' : `${Math.round(confidence * 100)}%`
);

function getTileHue(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = value.charCodeAt(index) + ((hash << 5) - hash);
  }
  return Math.abs(hash) % 360;
}

export const SoftwareGlyph = memo(function SoftwareGlyph({
  name, icon, size,
}: { name: string; icon?: string | null; size: number }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [icon]);
  const hue = useMemo(() => getTileHue(name || 'App'), [name]);
  const initial = (name?.[0] || '?').toUpperCase();

  return (
    <div
      style={{
        position: 'relative', width: size, height: size,
        borderRadius: Math.round(size * 0.28), background: 'var(--panel2)',
        border: '1px solid var(--line)',
        boxShadow: 'var(--shadow-sm)',
        display: 'grid', placeItems: 'center', fontSize: Math.round(size * 0.42),
        fontWeight: 700, flexShrink: 0, overflow: 'hidden', userSelect: 'none',
      }}
    >
      <span
        style={{
          display: 'grid', placeItems: 'center', width: '100%', height: '100%',
          background: `hsla(${hue}, 40%, 50%, 0.12)`,
          color: `hsl(${hue}, var(--tile-fg-s), var(--tile-fg-l))`, letterSpacing: 0,
        }}
      >
        {initial}
      </span>
      {icon && !failed && (
        <img
          src={icon}
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
          style={{
            position: 'absolute', inset: 0, width: '100%', height: '100%',
            objectFit: 'contain', padding: Math.round(size * 0.12), background: 'var(--panel2)',
          }}
        />
      )}
    </div>
  );
});

export const AppTile = memo(function AppTile({ e, size }: { e: SoftwareEntity; size: number }) {
  return <SoftwareGlyph name={e.name} icon={e.icon} size={size} />;
});

export function EvidenceBlock({ node }: { node: LpNode }) {
  const kids = [...(node.children ?? [])].sort((a, b) => (b.size ?? 0) - (a.size ?? 0));
  return (
    <div style={{ padding: '10px 14px', background: 'var(--bg)', borderLeft: '2px solid var(--accent-fg)', borderRadius: 6 }}>
      <div style={{ marginBottom: 6 }}>
        <b>判定：</b>
        {node.why || '—'}（置信度 {pct(node.conf)}）
        {node.family && <Tag style={{ marginLeft: 8 }} bordered={false}>族系 {node.family}</Tag>}
      </div>
      {!!node.evidence?.length && (
        <div style={{ marginBottom: 8 }}>
          <b>证据链：</b>
          {node.evidence.map((evidence, index) => (
            <div key={`${evidence.source}-${index}`} style={{ margin: '3px 0' }}>
              <Tag bordered style={{ color: 'var(--accent-fg)', borderColor: 'var(--accent-fg)', background: 'transparent' }}>{evidence.source}</Tag>
              {evidence.detail} <Tag bordered={false}>{pct(evidence.conf)}</Tag>
            </div>
          ))}
        </div>
      )}
      {node.redirect && (
        <Alert type="warning" showIcon style={{ marginBottom: 8 }} message={`junction 重定向 → ${node.redirect}（体积已去重，不计两次）`} />
      )}
      {!!kids.length && (
        <>
          <b>构成（子目录 {kids.length} 项）：</b>
          <Table<LpNode>
            size="small"
            rowKey="path"
            pagination={false}
            style={{ marginTop: 4 }}
            dataSource={kids.slice(0, 12)}
            columns={[
              { title: '大小', width: 100, render: (_, child) => <b className="lp-num" style={{ color: 'var(--cyan)' }}>{fmtSize(child.size)}</b> },
              { title: '子目录', render: (_, child) => <code className="lp-mono" style={{ fontSize: 12 }}>{child.path}</code> },
              { title: '说明', width: 240, render: (_, child) => child.role || child.why || '' },
            ]}
            footer={kids.length > 12 ? () => `其余 ${kids.length - 12} 项更小的子目录未列出` : undefined}
          />
        </>
      )}
    </div>
  );
}
