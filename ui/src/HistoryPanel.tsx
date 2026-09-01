import { useEffect, useState } from 'react';
import { Collapse, Pagination, Tag } from 'antd';
import type { CSSProperties, ReactNode } from 'react';

export default function HistoryPanel<T>({
  title,
  items,
  itemKey,
  renderItem,
  pageSize = 5,
  defaultExpanded = false,
  headerExtra,
  beforeRows,
  style,
}: {
  title: string;
  items: T[];
  itemKey: (item: T) => string;
  renderItem: (item: T) => ReactNode;
  pageSize?: number;
  defaultExpanded?: boolean;
  headerExtra?: ReactNode;
  beforeRows?: ReactNode;
  style?: CSSProperties;
}) {
  const [page, setPage] = useState(1);
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  useEffect(() => {
    setPage((current) => Math.min(current, pages));
  }, [pages]);
  const visible = items.slice((page - 1) * pageSize, page * pageSize);

  return (
    <Collapse
      className="lp-history-panel"
      size="small"
      style={style}
      defaultActiveKey={defaultExpanded ? ['history'] : []}
      items={[{
        key: 'history',
        label: (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7 }}>
            <span>{title}</span>
            <Tag bordered={false} style={{ marginInlineEnd: 0 }}>{items.length}</Tag>
          </span>
        ),
        extra: headerExtra,
        children: (
          <>
            {beforeRows}
            {visible.map((item) => (
              <div key={itemKey(item)}>{renderItem(item)}</div>
            ))}
            {items.length > pageSize && (
              <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 8 }}>
                <Pagination
                  size="small"
                  current={page}
                  pageSize={pageSize}
                  total={items.length}
                  showSizeChanger={false}
                  onChange={setPage}
                />
              </div>
            )}
          </>
        ),
      }]}
    />
  );
}
