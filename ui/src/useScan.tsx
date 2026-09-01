// 深度扫描的状态机。抽成独立模块是因为软件台账详情页也要能发起扫描——
// 原先它锁在 DashboardPage 里，详情页那个"重新扫描"只好挂成灰的。
import { useEffect, useRef, useState } from 'react';
import { App } from 'antd';
import { cancelScan, fetchScanStatus, ScanStatus, startScan, subscribeScan } from './api';

export type Scan = ReturnType<typeof useScan>;

export function useScan(onRefresh: () => void) {
  // 从 App context 取而不是 import 静态的 message/Modal：静态方法读全局主题，
  // 跟不上 ConfigProvider 的切换，浅色下弹窗仍是深色。详见 theme.tsx 注释。
  const { message, modal } = App.useApp();
  const [status, setStatus] = useState<ScanStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const unsub = useRef<(() => void) | null>(null);

  const stop = () => {
    unsub.current?.();
    unsub.current = null;
  };

  const listen = () => {
    stop();
    unsub.current = subscribeScan(
      (s) => {
        // idle 只在本地还没有已知任务时才接受：任务结束后服务端会回 idle，
        // 直接采纳会把刚跑完的结果冲掉，界面看起来像"什么都没发生"。
        if (s.state === 'idle') {
          setStatus((prev) => (prev && prev.state !== 'idle' ? prev : s));
          stop();
          return;
        }
        setStatus(s);
        if (s.state === 'done' || s.state === 'failed' || s.state === 'cancelled') {
          stop();
          if (s.state === 'done') onRefresh();
        }
      },
      // SSE 断了退回轮询一次，别让界面卡在中间态
      () => {
        fetchScanStatus().then(setStatus).catch(() => undefined);
      },
    );
  };

  // 挂载时认领可能已在跑的任务（切页面不该丢进度）
  useEffect(() => {
    fetchScanStatus()
      .then((s) => {
        setStatus(s);
        if (s.state === 'running' || s.state === 'pending') listen();
      })
      .catch(() => undefined);
    return stop;
  }, []);

  const begin = async () => {
    setStarting(true);
    try {
      const s = await startScan();
      setStatus(s);
      listen();
      if (s.conflict) message.info(s.conflict);
    } catch (e) {
      message.error(e instanceof Error ? e.message : '扫描启动失败');
    } finally {
      setStarting(false);
    }
  };

  /** 全盘扫描耗时以分钟计，且会覆盖当前快照（覆盖前自动归档留底），所以先问一声。 */
  const askBegin = () => {
    modal.confirm({
      title: '重新扫描 系统盘？',
      content:
        '扫描范围是整个 系统盘、不是单个软件——引擎只做全盘扫描，痕迹归因依赖全局视图。'
        + '全程只读，最后覆盖快照（覆盖前会归档上一份）。本机上次约 15 秒。',
      okText: '开始扫描',
      cancelText: '取消',
      onOk: begin,
    });
  };

  const askCancel = () => {
    modal.confirm({
      title: '取消本次扫描？',
      content: '扫描全程只读，中断不会改动现有快照，也不会留下半份数据。',
      okText: '取消扫描',
      cancelText: '继续扫描',
      onOk: async () => {
        await cancelScan();
        setStatus((p) => (p ? { ...p, cancel_requested: true } : p));
      },
    });
  };

  const busy = status?.state === 'running' || status?.state === 'pending';
  return { status, starting, busy, begin, askBegin, askCancel };
}
