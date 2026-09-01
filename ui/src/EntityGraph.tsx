// 单个软件的关联图谱，画在软件台账详情页右栏。
//
// 曾经有一个独立的"图谱"页（侧栏入口 + 软件下拉），已撤掉：图谱表达的是"某个软件的
// 关联结构"，脱离具体软件单独看没有意义，而详情页本来就已经选定了软件。
//
// 只负责画一个已选定的实体，不含选择器。
import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Spin } from 'antd';
import { Graph } from '@antv/g6';
import { fetchBodyTree, fmtSize, LpNode } from './api';

export interface BodyTreeNode {
  name: string;
  path: string;
  size: number;
  files: number;
  children: BodyTreeNode[];
}

export interface SoftwareEntityLike {
  id?: string;
  name: string;
  location?: string | null;
  location_exists: boolean;
  estimated_size?: number | null;
  traces?: LpNode[];
  traces_size?: number;
}

const GiB = 2 ** 30;

/**
 * 定性 → 节点填充色，**按主题分开**。
 *
 * 原先这是一张单表（深色值），浅色主题下照样用：`#3fb950` 绿在白画布上
 * 实测 1.92:1、`#d29922` 黄 1.67:1，节点几乎和背景糊在一起。G6 画在 canvas
 * 上，拿不到 CSS 变量，所以不能像 DiskTreePage 那样交给 `var(--dot-*)`——
 * 只能在这里按 theme 取一份，与下面的 `PAL` 同机制、同一组值（见 tokens.css
 * 的 --dot-* 注释，那里记着两个主题各自的实测对比度）。
 */
const CAT_FILL: Record<'dark' | 'light', Record<string, string>> = {
  dark: {
    可再生缓存: '#3fb950',
    可清理: '#3fb950',
    不可动: '#f85149',
    混合: '#d29922',
    容器: '#a371f7',
  },
  light: {
    可再生缓存: '#1a7f37',
    可清理: '#1a7f37',
    不可动: '#cf222e',
    混合: '#9a6700',
    容器: '#8250df',
  },
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const PAL: Record<'dark' | 'light', any> = {
  dark: {
    center: '#4493f8', body: '#2f81f7', other: '#6e7681', ring2: '#39424e',
    tx: '#e6edf3', tx2: '#9fb3d1', labelBg: 'rgba(8,12,22,0.78)',
    edge: 'rgba(240,246,252,0.16)', edge2: 'rgba(240,246,252,0.08)', stroke: '#0d1117',
    // 定性色挂在调色板上，buildGraph 只认 p 一个入参，这样它不必再多知道
    // theme 是什么——取色仍是"一处决定"。
    cat: CAT_FILL.dark,
  },
  light: {
    center: '#0969da', body: '#218bff', other: '#6e7781', ring2: '#b9c4cf',
    tx: '#1f2328', tx2: '#57606a', labelBg: 'rgba(255,255,255,0.88)',
    edge: 'rgba(31,35,40,0.3)', edge2: 'rgba(31,35,40,0.14)', stroke: '#ffffff',
    cat: CAT_FILL.light,
  },
};

/** 等容器拿到真实尺寸。约 2 秒内没等到就返回 false，交给调用处显式报出来。 */
function waitForSize(el: HTMLElement, tries = 120): Promise<boolean> {
  return new Promise((resolve) => {
    let n = 0;
    const tick = () => {
      const { width, height } = el.getBoundingClientRect();
      if (width > 1 && height > 1) return resolve(true);
      if (++n >= tries) return resolve(false);
      requestAnimationFrame(tick);
    };
    tick();
  });
}

// 半径按详情页右栏的实际宽度（约 450px）定。曾经还有一套给整页图谱用的大半径，
// 但图谱页已撤掉——图谱是"某个软件的关联结构"，脱离具体软件看没有意义。
const R1_BASE = 150;
const R2_BASE = 104;

/**
 * 布局随痕迹条数自适应。
 *
 * 写死半径的话，臂多了就挤成一团：一环上的节点按等角分布，臂数翻倍则相邻节点间距
 * 减半，标签先叠上，再叠圆点。所以臂多时把一环撑大（周长随之变大，间距回来），同时
 * 少画二环子节点——二环本来就是补充信息，挤在一起反而看不清主结构。
 *
 * 本机实测最多 6 条（NVIDIA），多数软件只有 1 条，所以 6 以内保持原样不动，
 * 免得为了照顾极少数情形把常见情形也改了。autoFit 会把整张图缩放到容器内，
 * 撑大半径不会溢出，只是节点显得小一些。
 */
function layoutFor(arms: number) {
  if (arms <= 6) return { r1: R1_BASE, r2: R2_BASE, kids: 5 };
  if (arms <= 10) return { r1: R1_BASE * 1.5, r2: R2_BASE * 0.8, kids: 3 };
  return { r1: R1_BASE * 2.1, r2: R2_BASE * 0.62, kids: 2 };
}

export function buildGraph(
  entity: SoftwareEntityLike,
  bodyTree: BodyTreeNode | null,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  p: any,
) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const nodes: any[] = [];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const edges: any[] = [];
  const lbl = (size: number, total: number) => size >= total * 0.03;

  nodes.push({
    id: 'c:root',
    style: {
      x: 0, y: 0, fill: p.center, size: 26,
      // 中心节点带一圈光晕：它是这张图的主体，应当是视觉起点。
      // shadow 而非再画一个大圆，省一个节点也不影响 autoFit 的包围盒。
      shadowColor: p.center,
      shadowBlur: 18,
      stroke: p.stroke,
      lineWidth: 2,
      labelText: entity.name, labelFontSize: 12, labelFill: p.tx,
      labelPlacement: 'bottom', labelBackground: true, labelBackgroundFill: p.labelBg,
      labelBackgroundRadius: 4,
      labelFontWeight: 600,
    },
  });

  const arms: { id: string; node: LpNode | BodyTreeNode; color: string }[] = [];
  if (entity.location) {
    arms.push({
      id: 'b:body',
      node: {
        name: '本体', path: entity.location, size: entity.estimated_size ?? 0,
        children: bodyTree ? bodyTree.children : [],
      },
      color: p.body,
    });
  }
  for (const t of entity.traces ?? []) {
    arms.push({ id: 't:' + t.path, node: t, color: p.cat[t.cat ?? ''] ?? p.other });
  }

  const { r1: R1, r2: R2, kids: KID_MAX } = layoutFor(arms.length);
  // 边宽按体积占比给：粗细本身就说明"哪条臂是大头"，不用去读标签才知道。
  const armTotal = Math.max(1, arms.reduce((s, a) => s + (a.node.size ?? 0), 0));

  arms.forEach((a, i) => {
    const ang = (2 * Math.PI * i) / arms.length - Math.PI / 2;
    const x1 = Math.cos(ang) * R1;
    const y1 = Math.sin(ang) * R1 * 0.86;
    nodes.push({
      id: a.id,
      style: {
        x: x1, y: y1, fill: a.color,
        size: 7 + Math.sqrt((a.node.size ?? 0) / GiB) * 5.5,
        // 描边取节点自身颜色的半透明外圈，让圆点在深浅底上都有轮廓
        stroke: p.stroke, lineWidth: 1.5,
        labelText: `${a.node.name}  ${fmtSize(a.node.size)}`,
        labelFontSize: 10, labelFill: p.tx, labelPlacement: 'bottom',
        labelBackground: true, labelBackgroundFill: p.labelBg, labelBackgroundRadius: 4,
      },
    });
    edges.push({
      source: 'c:root',
      target: a.id,
      style: {
        // 边用臂自己的颜色而不是统一灰：一眼能看出这条臂是缓存(绿)还是不可动(红)，
        // 不必先找到端点再对照颜色。
        stroke: a.color,
        strokeOpacity: 0.55,
        // 1.2 ~ 4px 按体积占比。占比大的那条视觉上就该更"重"。
        lineWidth: 1.2 + ((a.node.size ?? 0) / armTotal) * 2.8,
      },
    });

    const kids = (a.node.children ?? []).slice(0, KID_MAX);
    kids.forEach((k, j) => {
      const spread = Math.min(1.4, 0.28 * kids.length);
      const ka = ang + (kids.length === 1 ? 0 : (j / (kids.length - 1) - 0.5) * spread * 2);
      const id = a.id + '|k:' + k.path;
      const st: Record<string, unknown> = {
        x: x1 + Math.cos(ka) * R2,
        y: y1 + Math.sin(ka) * R2 * 0.86,
        fill: p.ring2,
        size: 4 + Math.sqrt((k.size ?? 0) / GiB) * 5,
      };
      if (lbl(k.size ?? 0, a.node.size ?? 1)) {
        st.labelText = k.name;
        st.labelFontSize = 9;
        st.labelFill = p.tx2;
        st.labelPlacement = 'bottom';
        st.labelBackground = true;
        st.labelBackgroundFill = p.labelBg;
        st.labelBackgroundRadius = 4;
      }
      st.stroke = p.stroke;
      st.lineWidth = 1;
      nodes.push({ id, style: st });
      // 外环边用臂色的更淡一档，保持"这几个子目录属于那条臂"的视觉归属
      edges.push({
        source: a.id,
        target: id,
        style: { stroke: a.color, strokeOpacity: 0.28, lineWidth: 1 },
      });
    });
  });
  return { nodes, edges };
}

export default function EntityGraph({
  entity, theme, height = 300,
}: {
  entity?: SoftwareEntityLike | null;
  theme: 'dark' | 'light';
  height?: number | string;
}) {
  const [bodyTree, setBodyTree] = useState<BodyTreeNode | null>(null);
  const [loadingTree, setLoadingTree] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<'loading' | 'ok' | 'fail'>('loading');
  const [renderKey, setRenderKey] = useState(0);

  useEffect(() => {
    setBodyTree(null);
    if (!entity?.location || !entity.location_exists) return;
    let dropped = false;
    setLoadingTree(true);
    fetchBodyTree(entity.location)
      .then((t) => !dropped && setBodyTree(t))
      .catch(() => !dropped && setBodyTree(null))
      .finally(() => !dropped && setLoadingTree(false));
    return () => {
      dropped = true;
    };
  }, [entity?.id, entity?.location, entity?.location_exists]);

  useEffect(() => {
    let disposed = false;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let graph: any = null;
    (async () => {
      if (!entity) return;
      setState('loading');
      try {
        if (!ref.current) return;
        // G6 往 0×0 容器渲染时不报错，只是什么都看不见——高度靠 flex/百分比传递时
        // 很容易在挂载瞬间还是 0。等到真有尺寸再画，等不到就明说，别留个空白框。
        const sized = await waitForSize(ref.current);
        if (disposed) return;
        if (!sized) {
          setState('fail');
          return;
        }
        const { nodes, edges } = buildGraph(entity, bodyTree, PAL[theme]);
        // 尊重 prefers-reduced-motion：那个设置就是用来说"别给我动画"的。
        // 关掉时图仍然是最终状态，只是不做入场过渡。
        const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
        graph = new Graph({
          container: ref.current,
          data: { nodes, edges },
          autoFit: 'view',
          padding: 32,
          // 入场动画：原先是一次性硬出现，现在淡入并轻微放大，视线能跟上
          // "先出中心、再展开臂"这个结构。280ms 是 token 里 --dur-slow 那一档。
          animation: reduce ? false : { duration: 280, easing: 'ease-out' },
          behaviors: [
            'drag-canvas',
            'zoom-canvas',
            'drag-element',
            // 悬停高亮：鼠标指到哪个节点，它和它的边亮起来、其余压暗。
            // 臂多的软件（NVIDIA 有 6 条）光靠颜色分不清哪条边连哪个点。
            { type: 'hover-activate', degree: 1 },
          ],
          node: {
            style: { stroke: PAL[theme].stroke, lineWidth: 1, cursor: 'pointer' },
            state: {
              // active = 悬停命中（含 degree:1 带上的邻居）
              active: { lineWidth: 3, stroke: PAL[theme].center, shadowBlur: 12 },
              // inactive = 同一次悬停里没命中的，压暗但不隐藏
              inactive: { fillOpacity: 0.35, labelOpacity: 0.35 },
            },
          },
          edge: {
            style: { stroke: PAL[theme].edge },
            state: {
              active: { strokeOpacity: 0.95, lineWidth: 2.5 },
              inactive: { strokeOpacity: 0.12 },
            },
          },
        });
        await graph.render();
        if (disposed) graph.destroy();
        else setState('ok');
      } catch (e) {
        console.error(e);
        if (!disposed) setState('fail');
      }
    })();
    return () => {
      disposed = true;
      try {
        graph?.destroy();
      } catch {
        /* 忽略 */
      }
    };
  }, [entity?.id, bodyTree, theme, renderKey]);

  const nothing = !entity || (!entity.location && (entity.traces?.length ?? 0) === 0);
  if (nothing) {
    return (
      <div style={{ height, display: 'grid', placeItems: 'center', padding: 12 }}>
        <span style={{ fontSize: 12, color: 'var(--tx3)', textAlign: 'center' }}>
          没有可画的关系：既没定位到本体，也没归因到 系统盘痕迹
        </span>
      </div>
    );
  }

  return (
    <div style={{ position: 'relative', height, width: '100%' }}>
      <div ref={ref} style={{ height: '100%', width: '100%' }} />
      {state === 'fail' && (
        <Alert
          type="warning"
          message="图谱渲染失败（辅助视图）"
          action={<Button size="small" onClick={() => setRenderKey((value) => value + 1)}>重试</Button>}
          style={{ position: 'absolute', top: 10, left: 10 }}
        />
      )}
      {state === 'loading' && !loadingTree && (
        <div
          role="status"
          aria-busy="true"
          style={{
            position: 'absolute', top: 8, right: 10, display: 'flex',
            alignItems: 'center', gap: 6, padding: '3px 8px', borderRadius: 6,
            background: 'var(--panel)', border: '1px solid var(--line)',
          }}
        >
          <Spin size="small" />
          <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--tx2)' }}>绘制图谱…</span>
        </div>
      )}
      {loadingTree && (
        // tip 在这里更不可能渲染（既非 nest 也非 fullscreen），而图谱上方本来
        // 就该说明"外环还在加载"，否则用户看到的是一张少了外环的图，会当成结论。
        <div
          role="status"
          aria-busy="true"
          style={{
            position: 'absolute', top: 8, right: 10, display: 'flex',
            alignItems: 'center', gap: 6, padding: '3px 8px', borderRadius: 6,
            background: 'var(--panel)', border: '1px solid var(--line)',
          }}
        >
          <Spin size="small" />
          <span style={{ fontSize: 'var(--fs-xs)', color: 'var(--tx2)' }}>读取子目录…</span>
        </div>
      )}
    </div>
  );
}
