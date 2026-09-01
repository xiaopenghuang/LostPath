import { useCallback, useEffect, useState } from 'react';
import {
  Alert, App, Button, Card, Empty, Spin, Statistic, Tag, Tooltip,
} from 'antd';
import { DeleteOutlined, UndoOutlined } from '@ant-design/icons';
import {
  ACTION_LABEL, fetchRecycle, fmtSize, purgeRecycle, RecycleEntry, RecycleReport,
  rollbackOperation,
} from './api';

/**
 * 回收站。
 *
 * 这一页存在的理由：用户点了"清理缓存"，界面说腾出 2.23 GiB，但 系统盘可用空间一点没变
 * ——数据只是被移进了回收区。不让他看见里面有什么、也不给腾空的入口，那个"已腾出"就是
 * 句空话。所以这里既要显示"还没真腾出来"，也要给出真腾出来的办法。
 */
export default function RecyclePage({ onRefresh }: { onRefresh?: () => void }) {
  // 见 theme.tsx：静态 message/Modal 读全局主题，跟不上 ConfigProvider 切换。
  // 这一页的「永久删除，无法恢复」是全工具唯一不可撤销的操作，它的弹窗
  // 在浅色主题下按深色算法渲染——最需要看清的那个确认框，样式恰恰是错的。
  const { message, modal } = App.useApp();
  const [report, setReport] = useState<RecycleReport | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    fetchRecycle()
      .then(setReport)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(reload, [reload]);

  const restore = (e: RecycleEntry) => {
    modal.confirm({
      title: '还原这份数据？',
      okText: '还原',
      cancelText: '取消',
      content: (
        <div style={{ fontSize: 13 }}>
          <p>会移回原来的位置{e.env_var && `，并把 ${e.env_var} 还原成操作前的值`}：</p>
          <code className="lp-mono" style={{ fontSize: 11.5, wordBreak: 'break-all' }}>
            {e.source_path}
          </code>
        </div>
      ),
      onOk: async () => {
        const res = await rollbackOperation(e.id);
        if (res.ok) {
          message.success('已还原到原位置');
          reload();
          onRefresh?.();
        } else {
          message.error(res.refused ?? '还原失败');
        }
      },
    });
  };

  /** 永久删除。这是全工具唯一真正销毁数据的操作，所以话要说重。 */
  const destroy = (e: RecycleEntry) => {
    modal.confirm({
      title: '永久删除，无法恢复',
      okText: '确认永久删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      content: (
        <div style={{ fontSize: 13 }}>
          <p>
            将真正删除 <b>{fmtSize(e.size)}</b>（{e.files} 个文件）。
            删除后<b>无法再还原</b>，这是本工具唯一不可撤销的操作。
          </p>
          <code className="lp-mono" style={{ fontSize: 11.5, wordBreak: 'break-all' }}>
            {e.source_path}
          </code>
          {!e.expired && e.days_left != null && (
            <p style={{ color: 'var(--red)', marginTop: 8 }}>
              这条还在回收期内（剩 {e.days_left} 天）。到期后会在下次启动时自动删掉，
              到期后将自动清除。除非需要立即释放空间，否则无需手动删除 —— 此操作不可撤销。
            </p>
          )}
        </div>
      ),
      onOk: async () => {
        setBusy(true);
        try {
          const res = await purgeRecycle([e.id]);
          if (res.purged.includes(e.id)) {
            message.success(`已永久删除，腾出 ${fmtSize(e.size)}`);
          } else {
            const s = res.skipped.find((x) => x.id === e.id);
            message.error(s?.reason ?? '删除失败');
          }
          reload();
          onRefresh?.();
        } finally {
          setBusy(false);
        }
      },
    });
  };

  const purgeExpired = () => {
    const n = report?.summary.expired_count ?? 0;
    modal.confirm({
      title: `永久删除 ${n} 项已过期数据？`,
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      content: (
        <div style={{ fontSize: 13 }}>
          这些数据已超过 {report?.summary.recoverable_days} 天回收期，删除后
          <b>无法恢复</b>，可腾出 {fmtSize(report?.summary.expired_size ?? 0)}。
        </div>
      ),
      onOk: async () => {
        const res = await purgeRecycle();
        message.success(`已删除 ${res.purged.length} 项`);
        reload();
        onRefresh?.();
      },
    });
  };

  if (err) {
    return (
      <div style={{ padding: '22px 26px' }}>
        <Alert type="error" showIcon message="回收站加载失败" description={err} />
      </div>
    );
  }
  if (!report) {
    // tip 写在 Spin 上不会渲染（antd 5 只在 nest / fullscreen 模式下认它），
    // 所以说明文字单独放一行。role=status 让读屏软件念得出来。
    return (
      <div style={{ padding: 60, textAlign: 'center' }} role="status" aria-busy="true">
        <Spin />
        <div style={{ marginTop: 12, fontSize: 'var(--fs-md)', color: 'var(--tx2)' }}>
          读取回收区…
        </div>
      </div>
    );
  }

  const s = report.summary;

  return (
    <div className="lp-page" style={{ padding: '22px 26px' }}>
      {s.count > 0 && (
        <>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 14 }}
            message={`这里的 ${fmtSize(s.total_size)} 还占着 系统盘`}
            description={
              <span style={{ fontSize: 12.5 }}>
                清理操作只是把数据移到了回收区，磁盘空间并没有真正释放——这样才能撤销。
                确认不再需要就永久删除，空间才会真的腾出来。超过 {s.recoverable_days} 天的
                会标为已过期，可一键清掉。
              </span>
            }
          />
          {/* 这条必须显眼：回收区在 %LOCALAPPDATA%\LostPath 下，而卸载工具正是扫那里。
              真实发生过——用 Geek Uninstaller 卸载后整个数据目录被清空。丢的是用户
              自己的数据，而他以为只是卸了个磁盘分析工具。 */}
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 14 }}
            message="卸载 LostPath 前请先处理回收区内容"
            description={
              <span style={{ fontSize: 12.5 }}>
                这些数据存放在 LostPath 自己的数据目录下（
                <code className="lp-mono" style={{ fontSize: 11.5 }}>{s.recycle_root}</code>
                ）。<b>卸载时这个目录会被一并删除</b>——尤其是用 Geek Uninstaller、
                Revo 这类做残留深扫的第三方卸载工具，它们不看我们的设置。
                还想留着的，请先「还原」回原位；不要的直接「永久删除」。
                若两者均未处理即卸载，这 {fmtSize(s.total_size)} 数据将随之丢失。
              </span>
            }
          />
        </>
      )}

      <Card size="small" style={{ marginBottom: 14 }}>
        <div style={{ display: 'flex', gap: 44, alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <Statistic
            title={<span style={{ fontSize: 11.5, color: 'var(--tx3)' }}>回收区占用</span>}
            value={fmtSize(s.total_size)}
            valueStyle={{ fontSize: 27, fontWeight: 700, color: 'var(--red)' }}
          />
          <Statistic
            title={<span style={{ fontSize: 11.5, color: 'var(--tx3)' }}>条目</span>}
            value={s.count}
            valueStyle={{ fontSize: 27, fontWeight: 700, color: 'var(--tx)' }}
          />
          <Statistic
            title={<span style={{ fontSize: 11.5, color: 'var(--tx3)' }}>已过期（可安全清掉）</span>}
            value={s.expired_count}
            suffix={
              <span style={{ fontSize: 13, color: 'var(--tx3)' }}>
                · {fmtSize(s.expired_size)}
              </span>
            }
            valueStyle={{ fontSize: 27, fontWeight: 700, color: 'var(--green)' }}
          />
          <Button
            danger
            icon={<DeleteOutlined />}
            disabled={s.expired_count === 0}
            onClick={purgeExpired}
            style={{ marginLeft: 'auto' }}
          >
            清空已过期
          </Button>
        </div>
        <div style={{ fontSize: 11.5, color: 'var(--tx3)', marginTop: 12 }}>
          位置 <code className="lp-mono">{s.recycle_root}</code>
        </div>
      </Card>

      {s.count === 0 ? (
        <Card>
          <Empty description="回收区为空。清理与迁移操作产生的数据将暂存于此，以便随时撤销" />
        </Card>
      ) : (
        <Card size="small" title="回收区内容">
          {report.entries.map((e) => (
            <div
              key={e.id}
              style={{
                display: 'flex', alignItems: 'center', gap: 12, padding: '11px 12px',
                border: '1px solid var(--line)', borderRadius: 8, marginBottom: 6,
                background: 'var(--bg)',
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, color: 'var(--tx)' }}>
                  {ACTION_LABEL[e.action as keyof typeof ACTION_LABEL] ?? e.action ?? '来历不明'}
                  {e.env_var && (
                    <Tag color="cyan" bordered={false} style={{ marginLeft: 6 }}>
                      {e.env_var}
                    </Tag>
                  )}
                  {/* 搬运没走完的数据必须显式标出来。曾经出过回收区实存 3.22 GiB 而
                      这里显示"0 项"的状态——那些字节界面看不见、也不会被自动清掉。 */}
                  {e.unconfirmed && (
                    <Tooltip
                      title={
                        e.status === 'orphan'
                          ? '回收区里有这份数据，但没有任何操作记录认领它。可能是台账文件损坏或丢失。还原前请先确认原路径。'
                          : '这次搬运没有完成（台账只记下了打算搬到哪）。数据在这里，但源目录可能也还留着一部分，还原前请先看一眼原路径。'
                      }
                    >
                      {/* **不用 `<Tag color="orange">`。** antd 的预设色在浅色主题下
                          实测只有 3.34:1（低于正文 4.5:1 的下限），这是本项目已记录的
                          已知问题；我一开始就是那么写的，浏览器实测才抓出来。
                          改用已验过的 `--amber`（浅色 #9a6700，对三种底色 4.57/4.87/4.17）
                          配透明底，与 App.tsx 里那个"重新扫描以更新数据"同一套做法。 */}
                      <Tag
                        bordered
                        style={{
                          marginLeft: 6,
                          color: 'var(--amber)',
                          borderColor: 'var(--amber)',
                          background: 'transparent',
                        }}
                      >
                        搬运未完成
                      </Tag>
                    </Tooltip>
                  )}
                </div>
                <code
                  className="lp-mono"
                  style={{
                    fontSize: 11, color: 'var(--tx3)', display: 'block',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}
                >
                  {e.source_path ?? e.recycled_to}
                </code>
              </div>
              <div style={{ textAlign: 'right', minWidth: 84 }}>
                <div className="lp-num" style={{ color: 'var(--tx)', fontWeight: 600 }}>
                  {fmtSize(e.size)}
                </div>
                <div style={{ fontSize: 10.5, color: 'var(--tx3)' }}>{e.files} 个文件</div>
              </div>
              {e.expired ? (
                <Tag color="green" bordered={false}>已过期</Tag>
              ) : e.days_left == null ? (
                /* 孤儿条目没有台账记录，也就没有回收期。原先一律显示「还剩 ? 天」——
                   那个问号读起来像程序算错了，而事实是这份数据压根没有到期时间。 */
                <Tooltip title="没有台账记录，因此没有回收期。它不会被自动清理，需要你确认后手动删除。">
                  <Tag bordered={false}>无回收期</Tag>
                </Tooltip>
              ) : (
                <Tooltip title={`可恢复至 ${e.recoverable_until ?? '未知'}`}>
                  <Tag bordered={false}>还剩 {e.days_left} 天</Tag>
                </Tooltip>
              )}
              {/* 孤儿没有原路径可还原回去，还原按钮对它无意义 */}
              {e.source_path && (
                <Button size="small" icon={<UndoOutlined />} onClick={() => restore(e)}>
                  还原
                </Button>
              )}
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                loading={busy}
                onClick={() => destroy(e)}
              >
                永久删除
              </Button>
            </div>
          ))}
        </Card>
      )}
    </div>
  );
}
