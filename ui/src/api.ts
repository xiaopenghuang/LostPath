export interface Evidence {
  source: string;
  conf: number;
  detail: string;
}

export interface LpNode {
  name: string;
  path: string;
  size: number;
  files?: number;
  owner?: string | null;
  owner_kind?: string | null;
  conf?: number;
  zone?: string | null;
  role?: string | null;
  redirect?: string | null;
  why?: string | null;
  evidence?: Evidence[];
  family?: string | null;
  cat?: string | null;
  children?: LpNode[];
}

export type EntitySource =
  | 'registry'
  | 'appx'
  | 'portable'
  | 'publisher-bucket'
  | 'trace';

export interface SoftwareEntity {
  id: string;
  name: string;
  version?: string | null;
  publisher?: string | null;
  source: EntitySource;
  location?: string | null;
  location_basis?: string | null;
  location_exists: boolean;
  estimated_size?: number | null;
  fragments: string[];
  portable: boolean;
  exe_path?: string | null;
  icon?: string;
  icon_src?: string | null;
  traces?: LpNode[];
  traces_size?: number;
  /** source==='trace' 的合成实体：归因成功但注册表无条目（工具链缓存等） */
  owner_kind?: string | null;
  /** 官方重定向变量（UV_CACHE_DIR 等），M4 可改环境变量替代 junction */
  redirects?: string[] | null;
  /** 厂商共享目录：体积可见但不可整块处理 */
  shared_vendor?: boolean;
}

export interface BodyTreeNode {
  name: string;
  path: string;
  size: number;
  files: number;
  children: BodyTreeNode[];
}

export interface PortableCandidate {
  name: string;
  dir: string;
  exe: string;
  exe_count: number;
  size: number;
}

export interface SnapshotMeta {
  /** false = 尚未扫描本机（首次启动的正常状态，不是错误），UI 应走引导 */
  present: boolean;
  reason?: string;
  schema_version?: number | null;
  scanned_at?: string | null;
  machine?: string | null;
  /** v2 起有；v1 快照为 null。含目录/文件数与非管理员盲区目录数 */
  scan_stats?: {
    total_dirs: number;
    total_files: number;
    total_bytes: number;
    elapsed_sec: number;
    denied_count: number;
    reparse_count: number;
    hardlink_dedup_bytes: number;
  } | null;
  note?: string;
  /**
   * 这份快照的 `size` **没有**排除硬链接。v3 之前扫描器里的去重是死代码
   * （`os.DirEntry.stat()` 在 Windows 上 `st_nlink` 恒为 0），uv / pnpm 这类共用内容的
   * 缓存被高报数倍——实测 uv 逻辑 1.59 GiB、真实占盘 0.31 GiB。重扫即可修正。
   *
   * 与 `reason` 分开：`reason` 表示"这份快照可能不可用"，这里是"数据可用但数字过时"。
   */
  sizes_inflated?: boolean;
  sizes_reason?: string | null;
}

export interface LpData {
  built_from: string;
  items: LpNode[];
  software: SoftwareEntity[];
  unlinked_traces: LpNode[];
  snapshot: SnapshotMeta;
  summary: {
    entries: number;
    total_size: number;
    unknown_size: number;
    entities: number;
    located: number;
    portable: number;
    registry_raw: number;
    components: number;
    unlinked_size: number;
    linked_entities: number;
    synthetic_entities: number;
    synthetic_size: number;
  };
}

export interface DriveInfo {
  letter: string;
  total: number;
  free: number;
}

/** 计划里的一步。reversible 说明这步怎么撤销，"—" 表示该步本身无副作用。 */
export interface PlanStep {
  n: number;
  title: string;
  detail: string;
  reversible?: string;
}

/** 一条拦阻原因。code 供程序判断，reason 给人看。 */
export interface Blocker {
  code: string;
  reason: string;
}

export type PlanAction = 'redirect' | 'cleanup' | 'junction' | 'none';

export interface Plan {
  path: string;
  name: string;
  action: PlanAction;
  size: number;
  files: number;
  owner?: string | null;
  owner_kind?: string | null;
  cat?: string | null;
  confidence: number;
  /** 预计能腾出的 C 盘空间 */
  reclaimable: number;
  target?: string | null;
  env_var?: string | null;
  redirect_mechanism?: { kind: string; var?: string; how?: string; note: string; hint: string } | null;
  steps: PlanStep[];
  notes: string[];
  blockers: Blocker[];
  /** 无拦阻且有动作时为 true。false 时看 blockers */
  executable: boolean;
  /** 非空表示这是子目录级计划：父目录整块不可动，只处理这一个子目录 */
  parent_path?: string | null;
}

export interface PlanReport {
  target_root: string | null;
  plans: Plan[];
  snapshot?: SnapshotMeta;
  hint?: string;
  summary: {
    total_candidates: number;
    executable: number;
    reclaimable: number;
    by_action: Record<string, { count: number; reclaimable: number }>;
    blocked: number;
    blocker_counts: Record<string, number>;
  };
}

/** 一条校验意见。code 供程序判断（界面要对系统盘那条做二次确认），message 给人看。 */
export interface TargetRootIssue {
  code: string;
  message: string;
}

/**
 * 目标位置的校验结果。
 *
 * **errors 与 warnings 分开是刻意的**：填系统盘技术上完全可行，只是腾不出空间，
 * 该由用户拍板；填网络盘则是执行到一半必然失败（junction 只能指向本地卷），
 * 不能让他有机会按下去。
 */
export interface TargetRootCheck {
  ok: boolean;
  normalized: string | null;
  errors: TargetRootIssue[];
  warnings: TargetRootIssue[];
}

export interface TargetRootInfo {
  /** 当前实际会用的位置 */
  effective: string;
  /** 没有自定义时自动挑出来的那个（非系统盘里剩余最大的） */
  auto: string;
  /** 用户存过的原始值，没设过为 null */
  saved: string | null;
  source: 'auto' | 'custom';
  /**
   * 设过但现在用不了——盘拔了、盘符变了、目录被别的程序删了。此时后端已**静默回落**
   * 到 auto，界面必须说出来：否则用户以为数据还会去他设的位置，拿着错的预期按执行。
   */
  saved_invalid: boolean;
  /** 系统盘盘符（如 "C:"）。界面在盘符下拉里据此标注"腾不出空间" */
  system_drive: string;
  /** saved 的校验意见，没设过时为空数组 */
  errors: TargetRootIssue[];
  warnings: TargetRootIssue[];
}

export async function fetchTargetRoot(): Promise<TargetRootInfo> {
  const r = await fetch('/api/target-root');
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

/** 只校验不保存，供输入时给即时反馈。校验里含一次真实写入探测，所以是 POST。 */
export async function checkTargetRoot(path: string): Promise<TargetRootCheck> {
  const r = await fetch('/api/target-root/check', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

/**
 * 保存目标位置。传 null 清除自定义、回落到自动挑。
 *
 * 后端校验不过时返回 400，body 仍是同一个形状（ok=false + errors），所以这里不区分
 * 状态码——调用方只看 ok 就够了。
 */
export async function saveTargetRoot(
  path: string | null,
): Promise<TargetRootCheck & { effective?: string; auto?: string }> {
  const r = await fetch('/api/target-root', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  return r.json();
}

export interface OverrideEntry {
  source: string;
  root: string;
  /** 这条现在还能不能用。失效时后端静默回落到全局根，界面必须显式说出来。 */
  valid: boolean;
  errors: TargetRootIssue[];
}

/**
 * 给单个源目录指定专属的目标根。`path` 传 null 清掉这一条。
 *
 * 回来的 `target` 是**后端算好的**完整目标路径。界面直接显示它，不自己拼——
 * 镜像规则只该有一份（planner.mirror_suffix），前端复制一遍必然与后端漂移，
 * 症状是"界面显示的位置和实际搬过去的不一样"，而两边都看起来对。
 */
export async function setTargetRootOverride(
  source: string,
  path: string | null,
): Promise<TargetRootCheck & { target?: string; action?: string; error?: string }> {
  const r = await fetch('/api/target-root/override', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source, path }),
  });
  return r.json();
}

export async function fetchTargetRootOverrides(): Promise<OverrideEntry[]> {
  const r = await fetch('/api/target-root/overrides');
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  const d = await r.json();
  return Array.isArray(d.overrides) ? d.overrides : [];
}

export async function fetchPlan(targetRoot?: string): Promise<PlanReport> {
  const q = targetRoot ? `?target_root=${encodeURIComponent(targetRoot)}` : '';
  const r = await fetch(`/api/plan${q}`);
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

/** 一次操作的回滚记录。status=planned 说明中途崩了，需人工检查。 */
export interface Operation {
  id: string;
  action: string;
  status: 'planned' | 'dry_run' | 'done' | 'failed' | 'rolled_back' | 'unreadable';
  created_at: string;
  recoverable_until?: string;
  source_path?: string;
  size?: number;
  files?: number;
  recycled_to?: string | null;
  env_var?: string | null;
  env_previous?: string | null;
  env_new?: string | null;
  failure?: string | null;
  steps_done?: { step: string; at: string }[];
  manifest_path?: string;
  /** 有值表示回收区数据已被永久删除，不可再还原。只看 recycled_to 是否为空会误判 */
  purged_at?: string | null;
  /**
   * 搬完后实测的体积。**清空回收区能腾出的是 freeable，不是 logical。**
   * 硬链接让同一份内容被多个路径共用，逐文件累加会重复计数——实测 uv 缓存
   * logical 1.59 GiB 而 freeable 只有 0.31 GiB。计划里的 reclaimable 是上界，
   * 这个才是实数。
   */
  freed?: {
    logical: number;
    dedup: number;
    freeable: number;
    files: number;
    linked_files: number;
  } | null;
  /** 执行前就落盘的"打算搬到哪"。搬运中途失败时靠它找回数据 */
  recycle_intent?: string | null;
}

export interface OperationsReport {
  operations: Operation[];
  summary: { total: number; rollbackable: number; recycle_bytes: number };
}

/**
 * 执行一条计划。**dryRun 默认 true**——要真动手必须显式传 false。
 *
 * 后端只接受路径并从快照里查记录，不接受调用方自带的目标；不在快照里的路径一律 404。
 */
export async function executePlan(
  path: string,
  dryRun = true,
  targetRoot?: string,
): Promise<{ ok: boolean; op?: Operation; refused?: string; error?: string; hint?: string }> {
  const r = await fetch('/api/act/execute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, dry_run: dryRun, target_root: targetRoot }),
  });
  const body = await r.json();
  if (r.ok) return { ok: true, op: body as Operation };
  return { ok: false, refused: body.refused, error: body.error, hint: body.hint };
}

export async function rollbackOperation(
  opId: string,
): Promise<{ ok: boolean; op?: Operation; refused?: string }> {
  const r = await fetch('/api/act/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ op_id: opId }),
  });
  const body = await r.json();
  return r.ok ? { ok: true, op: body as Operation } : { ok: false, refused: body.refused };
}

export async function fetchOperations(): Promise<OperationsReport> {
  const r = await fetch('/api/act/operations');
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

/** 回收区里的一条。size 是磁盘实测值，不是归因时记的快照值。 */
export interface RecycleEntry {
  id: string;
  /** 孤儿条目（回收区里有数据但无台账记录）为 null——它没有可查的动作与原路径 */
  action: string | null;
  source_path: string | null;
  recycled_to: string;
  size: number;
  files: number;
  created_at: string;
  recoverable_until?: string;
  /** 回收期还剩几天，0 表示已过期 */
  days_left: number | null;
  expired: boolean;
  status: string;
  env_var?: string | null;
  /**
   * 这份数据的搬运**没有完成**（台账只记了意图，或压根没有台账记录认领它）。
   * 曾经出过回收区实存 3.22 GiB 而界面显示"0 项"的状态，就是因为这类数据不被认领。
   */
  unconfirmed?: boolean;
  freed?: Operation['freed'];
}

export interface RecycleReport {
  entries: RecycleEntry[];
  summary: {
    count: number;
    total_size: number;
    expired_count: number;
    expired_size: number;
    recoverable_days: number;
    recycle_root: string;
  };
}

export async function fetchRecycle(): Promise<RecycleReport> {
  const r = await fetch('/api/act/recycle');
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

/**
 * 永久删除回收区数据。**不可恢复。**
 *
 * forceIds 为空时只清已过期项；要提前删必须点名 id——后端不接受"清空全部"这种
 * 一刀切指令，避免一次误点毁掉全部可恢复数据。
 */
export async function purgeRecycle(
  forceIds?: string[],
): Promise<{ purged: string[]; skipped: { id: string; reason: string; until?: string }[] }> {
  const r = await fetch('/api/act/purge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force_ids: forceIds ?? null }),
  });
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

export interface SettingsReport {
  paths: {
    data_root: string;
    latest_snapshot: string;
    icons_dir: string;
    portable_config: string;
    /** true = 走了环境变量覆盖 */
    override_active: boolean;
    /** 覆盖用的变量名，未覆盖时为 null */
    override_var: string | null;
  };
  snapshot: {
    present: boolean;
    scanned_at?: string | null;
    machine?: string | null;
    schema_version?: number | null;
    total_dirs?: number | null;
    total_files?: number | null;
    total_bytes?: number | null;
    elapsed_sec?: number | null;
    denied_count?: number | null;
    /** 具体被拒的目录（最多 40 条）。只给数字用户无从判断漏了什么 */
    denied_sample?: string[] | null;
    reparse_count?: number | null;
    /**
     * 产出这份快照时进程是否提权。**与 `engine.elevated` 是两件事**——后者是此刻
     * 的进程权限。判"该不该重扫"必须用两者的差，不能用 `denied_count > 0`：
     * 以管理员扫完照样有读不到的目录（系统保护、独占占用），那样会永远提示重扫。
     *
     * null / undefined = 旧快照没记这个字段，按"不知道"处理。
     */
    elevated?: boolean | null;
    /**
     * 这份快照的 `size` **没有**排除硬链接，uv / pnpm 这类共用内容的缓存会被高报数倍
     * （实测 uv 逻辑 1.59 GiB、真实占盘 0.31 GiB，虚高 412%）。重扫即可修正。
     *
     * 与 `reason` 分开：`reason` 表示"这份快照可能不可用"，这里是"数据可用但数字过时"。
     */
    sizes_inflated?: boolean;
    sizes_reason?: string | null;
  };
  recycle: {
    recoverable_days: number;
    recycle_root: string;
    /** 'startup' = 每次启动引擎时自动清掉过期项 */
    auto_purge?: string | null;
  };
  engine: { python: string; bind: string; elevated: boolean; scan_root: string };
}

export async function fetchSettings(): Promise<SettingsReport> {
  const r = await fetch('/api/settings');
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

export const OP_STATUS_LABEL: Record<string, { text: string; color: string }> = {
  done: { text: '已完成 · 可回滚', color: 'green' },
  rolled_back: { text: '已回滚', color: 'default' },
  failed: { text: '失败', color: 'red' },
  planned: { text: '中途中断，需检查', color: 'orange' },
  dry_run: { text: '仅预演', color: 'blue' },
  unreadable: { text: '记录损坏', color: 'red' },
};

export const ACTION_LABEL: Record<PlanAction, string> = {
  redirect: '改环境变量',
  cleanup: '清理缓存',
  junction: '迁移并建链接',
  none: '不处理',
};

/** 拦阻原因的人话说明。用于把 code 归类展示，避免同类原因重复占屏。 */
export const BLOCKER_LABEL: Record<string, string> = {
  missing: '目录已不存在（快照过期）',
  already_linked: '已是链接，体积不在本盘',
  too_small: '体积太小，不值当',
  not_cleanable: '性质不属于可清理/可再生缓存',
  low_confidence: '归因置信度不足',
  owner_container: '容器目录，应逐子目录处理',
  owner_system: '归属系统，影响面过大',
  owner_vendor: '厂商共享目录，含多个产品',
  unsafe_redirect: '重定向影响面超出单个软件',
  manual_redirect: '官方做法需手动执行',
  env_var_conflict: '同一环境变量被多个目录申领',
  target_full: '目标盘空间不足',
  in_use: '软件正在运行',
};

export type ScanState = 'idle' | 'pending' | 'running' | 'done' | 'failed' | 'cancelled';

export interface ScanResult {
  entries: number;
  total_size: number;
  unknown_size: number;
  scanned_files: number;
  scanned_dirs: number;
  /** 非管理员盲区：拒绝访问的目录数，需向用户明示 */
  denied_count: number;
  reparse_count: number;
  registry_apps: number;
  appx: number;
  shortcuts: number;
  scan_elapsed_sec: number;
  snapshot_path: string;
  /** 覆盖前归档的上一份快照，回滚用 */
  archived_previous: string | null;
  evidence_hits: Record<string, number>;
  index_warnings: string[];
}

export interface ScanStatus {
  state: ScanState;
  job_id?: string;
  /** 取消已请求但工作线程还没跑到检查点时为 true，与 state 是两件事 */
  cancel_requested?: boolean;
  phase?: string | null;
  phase_label?: string | null;
  percent?: number;
  detail?: string;
  elapsed_sec?: number;
  error?: string | null;
  result?: ScanResult | null;
  /** 冲突时后端给的提示（不叫 error，那个键被任务自身的错误占了） */
  conflict?: string;
}

export async function startScan(): Promise<ScanStatus> {
  const r = await fetch('/api/scan', { method: 'POST' });
  const body: ScanStatus = await r.json();
  // 409 = 已有任务在跑。这不是失败，把那个任务的状态交给调用方接管即可。
  if (!r.ok && r.status !== 409) throw new Error(body.conflict ?? `服务返回 ${r.status}`);
  return body;
}

export async function fetchScanStatus(): Promise<ScanStatus> {
  const r = await fetch('/api/scan/status');
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

export async function cancelScan(): Promise<ScanStatus> {
  const r = await fetch('/api/scan/cancel', { method: 'POST' });
  return r.json();
}

/**
 * 订阅扫描进度。返回退订函数。
 *
 * SSE 断线时浏览器会自动重连，但任务结束后服务端主动关流，此时重连会拿到
 * state=idle 并再次触发 onUpdate —— 所以终态要由调用方负责退订，否则
 * "完成"会被随后的 idle 覆盖掉。
 */
export function subscribeScan(
  onUpdate: (s: ScanStatus) => void,
  onError?: () => void,
): () => void {
  const es = new EventSource('/api/scan/events');
  es.onmessage = (ev) => {
    try {
      onUpdate(JSON.parse(ev.data) as ScanStatus);
    } catch {
      /* 心跳帧等非 JSON 内容忽略 */
    }
  };
  es.onerror = () => {
    es.close();
    onError?.();
  };
  return () => es.close();
}

export async function fetchDrives(): Promise<DriveInfo[]> {
  const r = await fetch('/api/drives');
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

export type EntityStatusKey = 'doable' | 'risk' | 'ok' | 'none';

/**
 * 台账列表与详情页头部的状态徽标。
 *
 * **"能不能处理"一律以计划器为准，不再自己按 cat 猜。** 原先这里用
 * `cats.every(c => 可再生缓存 || 可清理)` 判"建议清理"，有两个毛病：
 *
 * ① 它宣称的是一个**动作**，而 cat 只说得出"这是什么"，说不出"现在能不能动"。
 *    计划器还会查磁盘实况——目录正被占用、归因置信度不足、体积不够门槛、环境变量
 *    已被用户自己设过、目标盘容量不足，任何一条都会让它实际不可执行。于是列表说
 *    "建议清理"，点进迁移中心却一件也做不了。详情页下方的可处理性卡片早就改用了
 *    计划器，所以同一屏上两个说法会互相打架。
 * ② `every` 要求一个软件的**全部**痕迹都可清理，混进一条未定性的就整个否掉——而
 *    实际操作单元是目录。计划器早已放弃这套整块判断（见 MEMORY 里"定性下沉到子
 *    目录"那节），这里是最后一处遗留。
 *
 * plans 尚未就绪时退回纯描述性标签，**不显示任何动作主张**——宁可少说一句，也不
 * 说一句待会儿要被推翻的。
 */
export function entityStatus(
  e: SoftwareEntity,
  plans?: Map<string, Plan> | null,
): { key: EntityStatusKey; label: string; color: string } {
  const traces = e.traces ?? [];
  if (!traces.length) return { key: 'none', label: '无 C 盘痕迹', color: 'default' };

  if (plans) {
    // 子目录级计划的 path 不在 traces 里（traces 只有顶层），靠 parent_path 认领。
    // 不认的话，像 VS Code 那样"父目录整块不可动、下面某个缓存子目录可清"的软件
    // 会被算成一件可做的都没有。
    const own = new Set(traces.map((t) => t.path.toLowerCase()));
    let n = 0;
    for (const p of plans.values()) {
      if (!p.executable) continue;
      if (own.has(p.path.toLowerCase())
          || (p.parent_path && own.has(p.parent_path.toLowerCase()))) n += 1;
    }
    // 可处理优先于"含不可动项"：计划器已经把不该碰的全排除在这个计数之外，所以
    // 两者并存时报机会不会误导。旧逻辑里两者互斥是因为 every 的全称主张，现在
    // "N 处可处理"只是个局部计数，与"另外还有不可动项"并不冲突。
    if (n > 0) return { key: 'doable', label: `可处理 ${n} 处`, color: 'green' };
  }

  if (traces.some((t) => (t.cat ?? '') === '不可动'))
    return { key: 'risk', label: '含不可动项', color: 'red' };
  return { key: 'ok', label: '已归因', color: 'blue' };
}

export async function fetchData(): Promise<LpData> {
  const r = await fetch('/api/data');
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

export async function fetchBodyTree(path: string): Promise<BodyTreeNode | null> {
  const r = await fetch(`/api/body-tree?path=${encodeURIComponent(path)}`);
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

export async function scanPortable(path: string): Promise<PortableCandidate[]> {
  const r = await fetch('/api/portable/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

export async function confirmPortable(
  items: { name: string; dir?: string; exe?: string }[],
): Promise<{ ok: boolean; total_portable: number }> {
  const r = await fetch('/api/portable/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items }),
  });
  if (!r.ok) throw new Error(`服务返回 ${r.status}`);
  return r.json();
}

export function fmtSize(b?: number | null): string {
  if (b == null) return '—';
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let v = b;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(v >= 10 ? 1 : 2)} ${u[i]}`;
}

export const CAT_COLOR: Record<string, string> = {
  可再生缓存: 'green',
  可清理: 'green',
  不可动: 'red',
  混合: 'orange',
  容器: 'purple',
  未定性: 'default',
};

export const ZONE_LABEL: Record<string, string> = {
  LocalAppData: 'Local\\AppData',
  RoamingAppData: 'Roaming\\AppData',
  ProgramData: 'ProgramData',
  LocalLow: 'LocalLow',
};

export const KIND_LABEL: Record<string, string> = {
  app: '应用',
  app_unregistered: '未注册应用',
  vendor: '厂商',
  toolchain: '工具链',
  container: '容器',
  system: '系统',
  unknown: '未归因',
};

export const KIND_COLOR: Record<string, string> = {
  app: 'blue',
  app_unregistered: 'gold',
  vendor: 'geekblue',
  toolchain: 'cyan',
  container: 'purple',
  system: 'default',
  unknown: 'red',
};

export const SOURCE_LABEL: Record<EntitySource, string> = {
  registry: '注册表',
  appx: '商店应用',
  portable: '便携',
  'publisher-bucket': '组件聚合',
  trace: '未注册（痕迹推定）',
};

export const SOURCE_COLOR: Record<EntitySource, string> = {
  registry: 'blue',
  appx: 'purple',
  portable: 'green',
  'publisher-bucket': 'default',
  trace: 'cyan',
};
