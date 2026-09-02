"""LostPath 内置知识库。

实测结论：注册表 + 系统集成点无法解释的 C 盘足迹，绝大部分是包管理器、
运行时、Electron 工具链自行创建的目录（它们从不"安装"，因此注册表里没有条目）。
17 条规则即可解释 82% 的未归因体积，故知识库是核心模块而非补丁。

三张表：
- ALIASES        目录名 -> 软件名关键词（解决 AppData 目录名 != DisplayName）
- MARKERS        目录内标识文件 -> 用于确认族系（自验证，不盲信别名）
- TOOLCHAIN      包管理器/运行时缓存规则（注册表完全看不见的部分）
"""
import os
import re

# ---------------------------------------------------------------- 别名表
# 目录名(规范化) -> (软件名必须包含的关键词, 说明)
# 只做精确键匹配，绝不做子串扫描：VSCode 系 6 个编辑器目录名互为子串。
ALIASES = {
    "code": (["visual studio code"], "VSCode 用户数据目录名为 Code"),
    "cursor": (["cursor"], "Cursor 编辑器"),
    "trae": (["trae"], "Trae 编辑器"),
    "windsurf": (["windsurf"], "Windsurf 编辑器"),
    "kiro": (["kiro"], "Kiro 编辑器"),
    # 注意：Tencent 已移入 VENDOR_ALIASES —— 实测 Roaming\Tencent 下有 8 个
    # 不同产品（xwechat/WeChat/WeMeet/WeGame/QQMusic/QQ/TIM），属共享厂商目录，
    # 整块归给单个软件是错的。
    "baidunetdisk": (["百度网盘", "baidunetdisk"], "百度网盘"),
    "dingtalk": (["钉钉", "dingtalk"], "钉钉"),
    "larkshell": (["飞书", "lark"], "飞书"),
    "doubao": (["豆包", "doubao"], "豆包"),
    "iqiyivideo": (["爱奇艺", "iqiyi"], "爱奇艺"),
    "douyin": (["抖音", "douyin"], "抖音"),
    "vmware": (["vmware"], "VMware"),
    "sunloginclient": (["向日葵", "sunlogin"], "向日葵远控"),
    "todesk": (["todesk"], "ToDesk 远控"),
}

# ------------------------------------------------------------ 标识文件表
# 族系标识：目录内存在这些相对路径 -> 可确认属于该族，用于给别名匹配加证据
MARKERS = {
    "vscode-family": [
        r"User\settings.json",
        r"User\globalStorage",
    ],
    "electron-app": [
        "Local Storage",
        "IndexedDB",
    ],
    "chromium-profile": [
        r"Default\Preferences",
        "Local State",
    ],
}

# --------------------------------------------------------------- 工具链表
# ⚠️ **这张表已无任何调用方**（`lookup_toolchain` / `match_toolchain` 全仓零引用，
# 生产与测试都不用）。它是 v3 遗留：当时一张表同时承担"这是什么角色"和"这属于谁"，
# 后来拆成了 ROLE_RULES + OWNER_TOOLCHAIN（见下方第 299 行与 399 行的说明）。
# **改这里不会有任何效果**——要改定性/归属规则请改 OWNER_TOOLCHAIN、ROLE_RULES 或
# ZONE_SCOPED_TOOLCHAIN。留着未删是因为拆分时有条目被移入 ROLE_RULES，删表前需要
# 逐条核对没有规则丢失，那是独立一轮的事。
# (匹配模式, 标签, 性质, 官方重定向方式)
# 性质取值：可再生缓存 / 用户数据 / 可清理 / 混合 / 必需
TOOLCHAIN = [
    (r"^ms-playwright$", "Playwright 浏览器二进制", "可再生缓存", "PLAYWRIGHT_BROWSERS_PATH"),
    (r"^ms-playwright-mcp$", "Playwright MCP 缓存", "可再生缓存", None),
    (r"^uv$", "uv (Python) 缓存", "可再生缓存", "UV_CACHE_DIR"),
    (r"^pip$", "pip 缓存", "可再生缓存", "PIP_CACHE_DIR"),
    (r"^npm-cache$", "npm 缓存", "可再生缓存", "npm config set cache"),
    (r"^Yarn$", "Yarn 缓存", "可再生缓存", "YARN_CACHE_FOLDER"),
    (r"^pnpm(-store)?$", "pnpm store", "可再生缓存", "PNPM_HOME / store-dir"),
    (r"^\.nuget$", "NuGet 包缓存", "可再生缓存", "NUGET_PACKAGES"),
    (r"^\.gradle$", "Gradle 缓存", "可再生缓存", "GRADLE_USER_HOME"),
    (r"^\.m2$", "Maven 本地仓库", "可再生缓存", "settings.xml localRepository"),
    (r"^\.cargo$", "Cargo 缓存", "可再生缓存", "CARGO_HOME"),
    (r"^\.conda$|^conda-pkgs$", "Conda 包缓存", "可再生缓存", "CONDA_PKGS_DIRS"),
    (r"^go$", "Go 模块缓存", "可再生缓存", "GOMODCACHE"),
    (r"^electron$", "Electron 预下载二进制", "可再生缓存", "electron_config_cache"),
    (r"^electron-builder$", "electron-builder 缓存", "可再生缓存", "ELECTRON_BUILDER_CACHE"),
    (r"^boost_interprocess$", "Boost IPC 残留", "可清理", None),
    (r"^D3DSCache$", "D3D 着色器缓存", "可再生缓存", None),
    (r"^DXCache$|^GLCache$|^ComputeCache$", "GPU 着色器缓存", "可再生缓存", None),
    (r"^Temp$|^tmp$", "临时目录", "可清理", "TEMP / TMP"),
    (r"^CrashDumps$", "崩溃转储", "可清理", None),
    (r"-updater$", "自动更新器下载缓存", "可再生缓存", None),
    (r"^WV2RTFixed$|^EBWebView$", "WebView2 运行时/数据", "混合", None),
]
TOOLCHAIN_RE = [(re.compile(p, re.I), lab, cat, redir) for p, lab, cat, redir in TOOLCHAIN]

# ------------------------------------------------------------- 定性分级表
CACHE_RE = re.compile(
    r"(^|[\\/_\-.])(cache|caches|cachestorage|codecache|gpucache|shadercache|"
    r"dxcache|d3dscache|blob_storage|service ?worker|serviceworker|"
    r"webstorage|crashpad|crashdumps?|logs?|temp|tmp|thumbnails?|"
    r"packagecache|downloads?|installer|updater?)([\\/_\-.]|$)", re.I)

DATA_RE = re.compile(
    r"(^|[\\/_\-.])(user ?data|userdata|globalstorage|workspacestorage|history|"
    r"bookmarks?|profiles?|saves?|savegames?|databases?|indexeddb|"
    r"local ?storage|存档|数据|备份)([\\/_\-.]|$)", re.I)

# 上面两张表要求缓存词前后是分隔符，于是驼峰和空格分隔的目录名整片漏掉：
# FontCache、CachedData、Installer Cache 实测都判成"未定性"。
#
# 但**不能**简单放宽成子串匹配：`Templates` 里含 "Temp"，那样会把 Office 模板
# （真用户数据）判成临时文件并提议删除。所以改成先按驼峰/分隔符切成词，再整词比对——
# Templates 切出来是 ["templates"]，与 "temp" 不相等，自然排除。
_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+|(?<=[a-z0-9])(?=[A-Z])")

CACHE_WORDS = {
    "cache", "caches", "cached", "cachestorage", "codecache", "gpucache",
    "shadercache", "dxcache", "d3dscache", "fontcache", "blobstorage",
    "serviceworker", "webstorage", "crashpad", "crashdump", "crashdumps",
    "log", "logs", "logfiles", "temp", "tmp", "thumbnail", "thumbnails",
    "packagecache", "download", "downloads", "installer", "update", "updater",
}

# 整词命中即用户数据。与 CACHE_WORDS 同时命中时缓存优先（见 classify_name）：
# 例如 CachedData 切出 cached + data，它是编译产物缓存，不是用户资料。
DATA_WORDS = {
    "userdata", "globalstorage", "workspacestorage", "history", "bookmark",
    "bookmarks", "profile", "profiles", "save", "saves", "savegame",
    "savegames", "database", "databases", "indexeddb", "localstorage",
    "template", "templates", "存档", "数据", "备份",
}


def tokens(name):
    """按驼峰与非字母数字切词，返回小写整词列表。"""
    return [t.lower() for t in _TOKEN_SPLIT.split(name or "") if t]

# 高风险：网上教程常建议删除，实际会导致软件无法修复/更新/卸载
HIGH_RISK = [
    (r"^[A-Z]:\\Windows\\Installer$", "MSI 缓存，删除后无法修复/更新/卸载软件，仅可用官方工具收缩"),
    (r"^[A-Z]:\\Windows\\WinSxS$", "组件存储，仅可用 DISM /StartComponentCleanup"),
    (r"^[A-Z]:\\Windows\\System32\\DriverStore$", "驱动存储，删除导致设备无法重装驱动"),
    # Package Cache 有两处：ProgramData（全机）与 AppData\Local（按用户安装）。
    # 原先只登记了前者，而后者名字带空格、恰好躲过了 CACHE_RE 的分隔符要求，于是
    # 一直显示"未定性"——看着像安全的漏网，实则是两个缺陷互相掩盖。分词匹配上线后
    # 它会被判成可再生缓存并提议删除，而删掉它同样让 VS/VC++ 修复与卸载失败。
    (r"^[A-Z]:\\ProgramData\\Package Cache$", "VS/VC++ 安装缓存，删除后修复安装失败"),
    (r"^[A-Z]:\\Users\\[^\\]+\\AppData\\Local\\Package Cache$",
     "VS/VC++ 按用户安装缓存，删除后修复/卸载失败"),
    (r"^[A-Z]:\\ProgramData\\Kaspersky Lab(?:\\.*)?$",
     "卡巴斯基隔离区、病毒库与自保护服务数据，只能通过产品界面维护"),
]
HIGH_RISK_RE = [(re.compile(p, re.I), why) for p, why in HIGH_RISK]


# ------------------------------------------------------------ 厂商别名表
# 目录名(规范化) -> 规范厂商名。命中即强制按"厂商节点"处理并逐子目录归因。
# 实测依据：Roaming\Tencent 下有 xwechat/WeChat/WeMeet/WeGame/QQMusic/QQ/TIM
# 共 8 个产品，整块判给"腾讯会议"是与 v1 同类的错误。
# 另一个作用：解决目录名(英文) 与 注册表 Publisher(中文) 对不上的问题，
# 例如 Tencent vs 腾讯科技(深圳)有限公司。
VENDOR_ALIASES = {
    "tencent": "腾讯",
    "microsoft": "Microsoft",
    "microsoftcorporation": "Microsoft",
    "nvidia": "NVIDIA",
    "nvidiacorporation": "NVIDIA",
    "google": "Google",
    "adobe": "Adobe",
    "mozilla": "Mozilla",
    "apple": "Apple",
    "oracle": "Oracle",
    "jetbrains": "JetBrains",
    "epicgames": "Epic Games",
}

# 归一化厂商名时要剥掉的公司后缀（实测 Adobe / Adobe Systems Incorporated
# 若不剥离会被当成两个厂商）
CORP_SUFFIX = [
    "corporation", "incorporated", "technologies", "technology",
    "software", "systems", "limited", "company", "holdings",
    "inc", "ltd", "llc", "corp", "gmbh", "co", "sa", "ag", "bv",
    "股份有限公司", "有限公司", "有限责任公司", "科技", "网络", "信息", "软件",
]

# 安装路径中不具区分度的路径分量，不能用于归因
GENERIC_PATH_PARTS = {
    "programfiles", "programfilesx86", "programdata", "appdata", "local",
    "roaming", "locallow", "users", "windows", "bin", "install", "app",
    "apps", "common", "commonfiles", "resources", "application",
    "applications", "steamapps", "steamlibrary", "current", "release",
    "x64", "x86", "win32", "win64", "target", "dist", "build", "lib",
    "program", "files", "system32", "temp", "tmp", "data",
}


def norm_key(s):
    """规范化为比较键：小写、去除所有非字母数字与非汉字字符。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())


def strip_version(name):
    """剥离 DisplayName 的版本/形态后缀，得到可比较的产品名。

    实测必需：注册表里是 'HexHub 5.1.9' / 'OpenSquilla 0.5.4' /
    'Cursor (User)' / '微信开发者工具 2.01.2510260'，而 AppData 目录名
    只有 'HexHub' / '@opensquilla' / 'Cursor' / '微信开发者工具'。
    v1/v2 都因此漏掉了这批本可高置信归因的目录。
    """
    s = (name or "").strip()
    s = re.sub(r"\((?:user|machine|x64|x86|64|32)[^)]*\)", " ", s, flags=re.I)
    s = re.sub(r"\bversion\b", " ", s, flags=re.I)
    s = re.sub(r"\b\d+(?:\.\d+)+(?:\.\d+)*\b", " ", s)
    s = re.sub(r"\b\d{4,}\b", " ", s)
    s = re.sub(r"[\s_\-]+$", "", s)
    return s.strip()


def vendor_key(publisher):
    """厂商归一化键：剥离公司后缀，使 NVIDIA / NVIDIA Corporation 合并。"""
    k = norm_key(publisher)
    changed = True
    while changed:
        changed = False
        for suf in CORP_SUFFIX:
            if k.endswith(suf) and len(k) > len(suf):
                k = k[: -len(suf)]
                changed = True
    return k


def lookup_vendor_alias(dirname):
    """目录名 -> 规范厂商名，命中则应作为厂商节点处理。"""
    return VENDOR_ALIASES.get(norm_key(dirname))


def is_generic_part(part):
    return norm_key(part) in GENERIC_PATH_PARTS


def lookup_alias(dirname):
    """目录名 -> (关键词列表, 说明)。仅精确键匹配。

    绝不做子串扫描：VSCode 系 6 个编辑器的目录名互为子串，
    子串匹配会把 Roaming\\Code 判给 "Codex Account Switch"。
    """
    key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (dirname or "").lower())
    hit = ALIASES.get(key)
    if hit:
        return hit[0], hit[1]
    return None, None


def probe_markers(path):
    """探测目录内的族系标识文件。

    返回 (family, hit_marker) 或 (None, None)。
    用途：别名/名称匹配只是"名字像"，标识文件才证明"结构确实是"。
    """
    for fam, rels in MARKERS.items():
        for rel in rels:
            if os.path.exists(os.path.join(path, rel)):
                return fam, rel
    return None, None


def lookup_toolchain(dirname, path=None):
    """匹配包管理器/运行时缓存规则。

    path 目前不参与匹配，保留参数是为了后续按目录内结构做二次确认
    （例如 uv 缓存下应有 archive-v0/ 或 builds-v0/）。
    """
    for rx, lab, cat, redir in TOOLCHAIN_RE:
        if rx.search(dirname):
            return {"label": lab, "category": cat, "redirect": redir, "rule": rx.pattern}
    return None


# 兼容旧名
match_toolchain = lookup_toolchain


def high_risk(path):
    for rx, why in HIGH_RISK_RE:
        if rx.match(path):
            return why
    return None


def classify_name(name):
    """按名称给出性质，返回 (category, matched_by) 或 (None, None)。

    两层：先用原有的分隔符正则（保留既有判定不变），再用整词匹配兜住驼峰与空格分隔
    的写法。缓存优先于用户数据——CachedData 这类切出 cached+data 两词都命中，它是
    编译产物而非用户资料。
    """
    if CACHE_RE.search(name):
        return "可再生缓存", "名称含缓存特征"
    if DATA_RE.search(name):
        return "用户数据", "名称含用户数据特征"

    tk = set(tokens(name))
    if tk & CACHE_WORDS:
        hit = sorted(tk & CACHE_WORDS)[0]
        return "可再生缓存", f"名称分词含缓存特征（{hit}）"
    if tk & DATA_WORDS:
        hit = sorted(tk & DATA_WORDS)[0]
        return "用户数据", f"名称分词含用户数据特征（{hit}）"
    return None, None


# ==================================================================== v4 新增
# v3 根因缺陷：TOOLCHAIN 一张表同时承担「这是什么角色」和「这属于谁」两件事，
# 于是 label 被误当 owner。实测暴露出三种同源表现：
#   Local\Temp            owner="临时目录"        应为 系统
#   apifox-updater        owner="自动更新器下载缓存" 应为 Apifox
#   Local\Packages        owner=None            应为 容器节点
# v4 把两件事拆开：ROLE_RULES 只定角色，归属另有来源。


# ---------------------------------------------------------- 角色规则（无归属）
# (模式, 角色标签, 性质, 归属策略, 官方重定向)
# 归属策略：
#   system  -> 系统所有，不属于任何单个软件
#   stem    -> 剥掉角色后缀得到词干，再去匹配软件（apifox-updater -> apifox）
#   inherit -> 继承父目录归属（父目录已归因时才有意义）
#   fixed:X -> 固定归属 X
ROLE_RULES = [
    (r"^Temp$|^tmp$", "临时目录", "可清理", "system", "TEMP / TMP"),
    (r"^CrashDumps$", "崩溃转储", "可清理", "system", None),
    (r"^D3DSCache$", "D3D 着色器缓存", "可再生缓存", "system", None),
    (r"^DXCache$|^GLCache$|^ComputeCache$", "GPU 着色器缓存", "可再生缓存", "system", None),
    (r"^boost_interprocess$", "Boost IPC 残留", "可清理", "system", None),
    (r"^INetCache$|^INetCookies$|^WebCache$", "IE/WinINet 缓存", "可再生缓存", "system", None),
    (r"^WV2RTFixed$|^EBWebView$", "WebView2 运行时/数据", "混合",
     "fixed:Microsoft Edge WebView2", None),
    (r"-updater$|_updater$", "自动更新器下载缓存", "可再生缓存", "stem", None),
    (r"-cache$|_cache$", "缓存目录", "可再生缓存", "stem", None),
    (r"^Crash Reports$|^CrashReports$|^Crashpad$", "崩溃报告", "可清理", "inherit", None),
]
ROLE_RE = [(re.compile(p, re.I), lab, cat, own, redir)
           for p, lab, cat, own, redir in ROLE_RULES]

# 剥离角色后缀用（stem 策略）
ROLE_SUFFIX_RE = re.compile(r"[-_](updater|update|cache|helper|setup|installer)$", re.I)


# ------------------------------------------------------------ 容器目录表
# 这些目录本身不属于任何软件，它们是「一批软件各自子目录」的容器。
# v3 把它们留在未归因里，导致 Packages 0.72 GiB 无归属 —— 实际其 4 个子目录
# 全部已精确归因，问题只是容器自身没有类型。
# (路径模式, 容器标签, 子项归属依据)
CONTAINERS = [
    (r"\\AppData\\Local\\Packages$", "Appx 应用数据容器", "PackageFamilyName"),
    # 盘符写 [A-Z] 配 re.I，与 HIGH_RISK 同口径——原先写死 `^C:` ，系统装 D 盘的机器上
    # 这个容器不被识别，于是它自身体积算成未归因、子目录也不下钻。
    (r"^[A-Z]:\\ProgramData\\Package Cache$", "MSI/VC++ 安装包缓存容器", "MSI ProductCode"),
    (r"\\AppData\\Local\\Programs$", "per-user 安装容器", "子目录即软件"),
    (r"\\AppData\\Local\\Microsoft\\WindowsApps$", "Appx 执行别名容器", "AppExecLink"),
]
CONTAINER_RE = [(re.compile(p, re.I), lab, by) for p, lab, by in CONTAINERS]


# --------------------------------------------------------- rDNS 段停用词
# 反向 DNS 形态目录名（com.x.y / io.github.x.y）取段时必须排除的词。
# 实测教训：取「末段」会错 —— com.ccswitch.desktop -> desktop -> 撞
# "Docker Desktop"；com.lgdy88.codex-enhance.manager -> manager -> 撞
# "Neat Download Manager"。这是 v1 子串匹配错误换了个马甲。
# 正确做法：剔除停用词后取最长段。
RDNS_STOP = {
    "com", "cn", "io", "org", "net", "dev", "app", "github", "gitee", "www",
    "desktop", "manager", "client", "launcher", "electron", "tauri", "ui",
    "inc", "ltd", "co", "team", "studio", "software", "tech", "labs",
}
RDNS_RE = re.compile(r"^[a-z0-9]+(\.[a-z0-9][a-z0-9\-]*){2,}$", re.I)


# ------------------------------------------------- 通用词（不可作为软件名）
# 目录名是这些词时，绝不能拿去和注册表做模糊匹配，也不能当「未注册软件」名。
GENERIC_WORDS = {
    "temp", "tmp", "cache", "caches", "data", "config", "settings", "logs",
    "log", "bin", "lib", "share", "local", "roaming", "user", "users",
    "default", "common", "shared", "public", "programs", "packages",
    "update", "updater", "updates", "installer", "setup", "download",
    "downloads", "backup", "profile", "profiles", "plugins", "plugin",
    "extensions", "node", "python", "electron", "chromium", "webview",
    "crashdumps", "crashreports", "history", "storage", "db", "database",
}


def role_of(name):
    """角色规则匹配，返回 (label, cat, own_policy, redirect) 或 None。"""
    for rx, lab, cat, own, redir in ROLE_RE:
        if rx.search(name):
            return lab, cat, own, redir
    return None


def role_stem(name):
    """剥掉角色后缀得到词干：apifox-updater -> apifox。"""
    return ROLE_SUFFIX_RE.sub("", name)


def container_of(path):
    """容器目录匹配，返回 (label, by) 或 None。"""
    for rx, lab, by in CONTAINER_RE:
        if rx.search(path):
            return lab, by
    return None


# -------------------------------------------------- 工具链（工具自身即归属）
# 与 ROLE_RULES 的区别：这里目录的所有者就是那个工具本身，label 可安全用作
# owner（npm-cache 属于 npm）。而 ROLE_RULES 里的 label 只是角色，不是归属。
# 从原 TOOLCHAIN 表中筛出「工具即所有者」的条目，Temp / -updater /
# D3DSCache / CrashDumps / WebView2 等已移入 ROLE_RULES。
OWNER_TOOLCHAIN = [
    (r"^ms-playwright$", "Playwright", "可再生缓存", "PLAYWRIGHT_BROWSERS_PATH"),
    (r"^ms-playwright-mcp$", "Playwright MCP", "可再生缓存", None),
    (r"^ms-playwright-go$", "Playwright (Go)", "可再生缓存", None),
    (r"^uv$", "uv (Python)", "可再生缓存", "UV_CACHE_DIR"),
    (r"^pip$", "pip", "可再生缓存", "PIP_CACHE_DIR"),
    (r"^npm-cache$", "npm", "可再生缓存", "npm config set cache"),
    (r"^node-gyp$", "node-gyp", "可再生缓存", None),
    (r"^Yarn$", "Yarn", "可再生缓存", "YARN_CACHE_FOLDER"),
    (r"^pnpm(-store)?$", "pnpm", "可再生缓存", "PNPM_HOME / store-dir"),
    (r"^\.nuget$", "NuGet", "可再生缓存", "NUGET_PACKAGES"),
    (r"^\.gradle$", "Gradle", "可再生缓存", "GRADLE_USER_HOME"),
    (r"^\.m2$", "Maven", "可再生缓存", "settings.xml localRepository"),
    (r"^\.cargo$", "Cargo (Rust)", "可再生缓存", "CARGO_HOME"),
    (r"^\.rustup$", "rustup", "可再生缓存", "RUSTUP_HOME"),
    (r"^\.conda$|^conda-pkgs$", "Conda", "可再生缓存", "CONDA_PKGS_DIRS"),
    (r"^go$|^go-build$", "Go", "可再生缓存", "GOMODCACHE / GOCACHE"),
    (r"^electron$", "Electron", "可再生缓存", "electron_config_cache"),
    (r"^electron-builder$", "electron-builder", "可再生缓存", "ELECTRON_BUILDER_CACHE"),
    (r"^deno$", "Deno", "可再生缓存", "DENO_DIR"),
    (r"^bun$", "Bun", "可再生缓存", "BUN_INSTALL_CACHE_DIR"),
]
OWNER_TOOLCHAIN_RE = [(re.compile(p, re.I), lab, cat, redir)
                      for p, lab, cat, redir in OWNER_TOOLCHAIN]


# --------------------------------------- 同名目录在不同 zone 下是不同的东西
# 上面那张表只按目录名匹配，于是 `AppData\Local\uv` 与 `AppData\Roaming\uv` 被同一条
# `^uv$` 通吃。但这两个目录装的根本不是一类东西：
#
#   Local\uv    是缓存（archive-v0 / wheels-v5 / environments-v2），由 UV_CACHE_DIR 决定
#   Roaming\uv  是 python\（受管解释器）与 tools\（已装的 CLI 工具），
#               分别由 UV_PYTHON_INSTALL_DIR 与 UV_TOOL_DIR 决定
#
# 通吃的后果是给用户看的理由是错的：Roaming 那侧显示"可再生缓存 · 改 UV_CACHE_DIR
# 即可"，而设那个变量对它毫无作用，删掉 tools\ 会让用户已装的命令直接消失。定性成
# 「混合」让计划器按 not_cleanable 拦下——宁可少腾这 0.06 GiB，也不能拿一个错理由
# 建议用户删东西（DESIGN.md §1：归因必须带证据）。
#
# (目录名正则, 路径约束正则, 归属标签, 性质, 重定向提示, 角色标签)
# 角色标签单列是因为 attribute_v4 原本把 role 硬拼成 "<label> 缓存/数据"——这里既然
# 不是缓存，那句话本身就得换掉，否则 cat 改对了而理由里还写着"缓存"。
ZONE_SCOPED_TOOLCHAIN = [
    (r"^uv$", r"\\AppData\\Roaming\\uv\\?$", "uv (Python)", "混合",
     "UV_TOOL_DIR / UV_PYTHON_INSTALL_DIR", "uv (Python) 工具与解释器"),
]
ZONE_SCOPED_RE = [(re.compile(n, re.I), re.compile(p, re.I), lab, cat, redir, role)
                  for n, p, lab, cat, redir, role in ZONE_SCOPED_TOOLCHAIN]


def lookup_owner_toolchain(dirname, path=None):
    """工具链归属查询，返回 dict(label, cat, redirect[, role]) 或 None。

    **path 参与匹配**：先查 zone 限定表（见 ZONE_SCOPED_TOOLCHAIN），命中即返回。
    path 为 None 时只走通用表——调用方拿不到路径时不该被 zone 规则影响。
    """
    if path:
        for rx, prx, lab, cat, redir, role in ZONE_SCOPED_RE:
            if rx.search(dirname) and prx.search(path):
                return {"label": lab, "cat": cat, "redirect": redir, "role": role}
    for rx, lab, cat, redir in OWNER_TOOLCHAIN_RE:
        if rx.search(dirname):
            return {"label": lab, "cat": cat, "redirect": redir}
    return None


# ------------------------------------------- 安装器文件名 -> 软件名（兜底）
# 用于 Package Cache 下 GUID 对不上注册表的目录：目录内的安装器文件名本身
# 就是证据（实测 2 个 SHA 形态目录内含 vcredist_x64.exe）。
INSTALLER_HINTS = [
    (r"^vcredist", "Visual C++ 可再发行组件"),
    (r"^vc_redist", "Visual C++ 可再发行组件"),
    (r"^dotnet-runtime|^windowsdesktop-runtime", ".NET 运行时"),
    (r"^ndp\d|^dotnetfx", ".NET Framework"),
    (r"^wdksetup|^sdksetup|^winsdksetup", "Windows SDK"),
    (r"^python-3|^python-2", "Python"),
    (r"^node-v", "Node.js"),
]
INSTALLER_HINTS_RE = [(re.compile(p, re.I), name) for p, name in INSTALLER_HINTS]


def installer_hint(filenames):
    """从目录内文件名列表推断软件名，返回 (软件名, 命中的文件名) 或 None。"""
    for fn in filenames:
        for rx, name in INSTALLER_HINTS_RE:
            if rx.search(fn):
                return name, fn
    return None


def rdns_pick(name):
    """反向 DNS 目录名取最长非停用词段，取不到返回 None。"""
    if not RDNS_RE.match(name):
        return None
    segs = [s for s in name.split(".") if s.lower() not in RDNS_STOP]
    return max(segs, key=len) if segs else None


def looks_like_product(name):
    """目录名是否像一个软件名（可用于「未注册软件」节点命名）。"""
    n = name.strip().lower()
    if len(n) < 3 or n in GENERIC_WORDS:
        return False
    if n.startswith(".") or re.match(r"^\{?[0-9a-f\-]{8,}\}?$", n):
        return False
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", name))
