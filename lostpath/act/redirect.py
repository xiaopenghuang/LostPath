r"""把知识库的"官方重定向"提示翻译成可执行机制。

**为什么需要这一层。** 知识库里 `redirect` 字段是给人看的提示，不是能直接执行的
环境变量名。实测本机 7 条带 redirect 的痕迹里，字面值有四种完全不同的东西：

    PLAYWRIGHT_BROWSERS_PATH   真的是单个环境变量，可自动改
    TEMP / TMP                 两个变量，且是系统级，改了影响整个操作系统
    npm config set cache       一条命令，不是环境变量
    settings.xml localRepository  要改配置文件

照字面"改环境变量即可"去实现，会做出一个能把用户系统 TEMP 改掉的功能。所以这里
按机制分三级，只有 `env` 级允许自动执行，其余交回给人并给出具体做法。

放在 `lostpath/act/` 而不是知识库里：这是"怎么动"的知识，与"这是什么"正交。归因
算法不该因为执行器的需要而改动（AGENTS.md 的约束）。
"""
from __future__ import annotations

# 机制分级：
#   env     单个用户级环境变量，程序可自动设置（唯一允许自动执行的一级）
#   manual  能改，但要改配置文件或跑命令，或涉及多个变量——交回给人，给出具体做法
#   unsafe  技术上能改，但不该由本工具改（影响面超出单个软件）
MECHANISMS: dict[str, dict] = {
    # ---------------- 可自动执行：单个用户级环境变量 ----------------
    "PLAYWRIGHT_BROWSERS_PATH": {
        "kind": "env", "var": "PLAYWRIGHT_BROWSERS_PATH",
        "note": "Playwright 启动时读该变量决定浏览器二进制位置",
    },
    "UV_CACHE_DIR": {
        "kind": "env", "var": "UV_CACHE_DIR",
        "note": "uv 全局缓存位置",
    },
    "YARN_CACHE_FOLDER": {
        "kind": "env", "var": "YARN_CACHE_FOLDER",
        "note": "Yarn 1.x 缓存目录；Yarn 2+ 用 .yarnrc.yml 的 cacheFolder",
    },
    "ELECTRON_BUILDER_CACHE": {
        "kind": "env", "var": "ELECTRON_BUILDER_CACHE",
        "note": "electron-builder 下载缓存",
    },
    "electron_config_cache": {
        "kind": "env", "var": "electron_config_cache",
        "note": "@electron/get 的下载缓存。变量名确实是小写，不是笔误",
    },
    "PIP_CACHE_DIR": {
        "kind": "env", "var": "PIP_CACHE_DIR",
        "note": "pip 下载与构建缓存",
    },
    "NUGET_PACKAGES": {
        "kind": "env", "var": "NUGET_PACKAGES",
        "note": "NuGet 全局包目录",
    },
    "GRADLE_USER_HOME": {
        "kind": "env", "var": "GRADLE_USER_HOME",
        "note": "Gradle 主目录（含缓存与 wrapper 分发）",
    },
    "CARGO_HOME": {
        "kind": "env", "var": "CARGO_HOME",
        "note": "Cargo 主目录（registry 缓存与已装二进制）",
    },
    "RUSTUP_HOME": {
        "kind": "env", "var": "RUSTUP_HOME",
        "note": "rustup 工具链安装位置",
    },
    "DENO_DIR": {
        "kind": "env", "var": "DENO_DIR",
        "note": "Deno 依赖缓存",
    },
    "BUN_INSTALL_CACHE_DIR": {
        "kind": "env", "var": "BUN_INSTALL_CACHE_DIR",
        "note": "Bun 安装缓存",
    },
    "CONDA_PKGS_DIRS": {
        "kind": "env", "var": "CONDA_PKGS_DIRS",
        "note": "Conda 包缓存。可接多个目录，本工具只写单个",
    },

    # ---------------- 要人工：改配置文件或跑命令 ----------------
    "npm config set cache": {
        "kind": "manual",
        "how": "npm config set cache \"<新路径>\" --global",
        "note": "npm 缓存位置存在 .npmrc 里，不认环境变量（NPM_CONFIG_CACHE 可用但"
                "会被 .npmrc 覆盖），故按官方做法跑命令",
    },
    "PNPM_HOME / store-dir": {
        "kind": "manual",
        "how": "pnpm config set store-dir \"<新路径>\"",
        "note": "PNPM_HOME 管的是可执行文件位置，不是 store；两者别混",
    },
    "settings.xml localRepository": {
        "kind": "manual",
        "how": "在 ~/.m2/settings.xml 里设 <localRepository>新路径</localRepository>",
        "note": "Maven 不读环境变量决定本地仓库位置",
    },
    "GOMODCACHE / GOCACHE": {
        "kind": "manual",
        "how": "go env -w GOMODCACHE=<新路径> GOCACHE=<新路径>",
        "note": "两个不同用途的目录，且 go env -w 写的是 go 自己的配置文件，"
                "比设环境变量更可靠",
    },
    "UV_TOOL_DIR / UV_PYTHON_INSTALL_DIR": {
        "kind": "manual",
        "how": "UV_TOOL_DIR=<新路径>（已装的 CLI 工具）"
               "与 UV_PYTHON_INSTALL_DIR=<新路径>（受管 Python 解释器）",
        "note": "Roaming\\uv 下是 tools\\ 与 python\\ 两类东西，各由不同变量决定，"
                "不是一个 UV_CACHE_DIR 能搬的（那个只管 Local\\uv 的缓存）。"
                "变量名是 UV_TOOL_DIR——实测 uv 0.7.13 不认 UV_TOOL_INSTALL_DIR，"
                "设了它 `uv tool dir` 仍返回默认位置，正是本模块要防的"
                "「设了个软件根本不读的变量」那一类",
    },

    # ---------------- 不该由本工具改：影响面超出单个软件 ----------------
    "TEMP / TMP": {
        "kind": "unsafe",
        "note": "TEMP/TMP 是全系统共用的临时目录，几乎每个进程都在用。改它影响"
                "范围远超单个软件，且迁移期间正在写临时文件的程序会出错。"
                "临时目录应当清理而非迁移",
    },
}


def resolve(redirect_hint: str | None) -> dict | None:
    """把 redirect 提示解析为机制。未登记的一律当 manual 处理，不猜。

    返回 {kind, var?, how?, note, hint} 或 None（本就没有重定向机制）。
    """
    if not redirect_hint:
        return None
    m = MECHANISMS.get(redirect_hint)
    if m:
        return {**m, "hint": redirect_hint}
    # 未登记：可能是知识库新加了规则而这里没跟上。不猜成环境变量——猜错会去设一个
    # 软件根本不读的变量，然后"迁移成功"但软件仍在老位置重新下载。
    return {
        "kind": "manual", "hint": redirect_hint,
        "how": f"参照该工具官方文档设置：{redirect_hint}",
        "note": "本工具尚未登记这条重定向机制的具体做法，故不自动执行",
    }


def is_auto_redirectable(redirect_hint: str | None) -> bool:
    """能否走"改环境变量"这条低风险路径。"""
    m = resolve(redirect_hint)
    return bool(m and m["kind"] == "env")
