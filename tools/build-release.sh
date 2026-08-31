#!/usr/bin/env bash
# 一键打出可分发的 Windows 安装包。在 Git Bash 里跑：
#
#     sh tools/build-release.sh
#
# 产物 release/LostPath Setup <版本>.exe。目标机器不需要 Python、conda 或 Node。
#
# 顺序不能调：引擎 exe 要把 ui/dist 打进去，所以前端必须先构建完；安装包又要把
# 引擎 exe 放进 resources，所以引擎必须先打完。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 解释器默认取 PATH 里的 python；装在别处（conda 环境等）就用 LOSTPATH_PY 指过去：
#     LOSTPATH_PY=/g/conda/envs/lostpath/python.exe sh tools/build-release.sh
# 不写死绝对路径——那种配置只对一台机器成立。
PY="${LOSTPATH_PY:-python}"

cd "$ROOT"

echo "==> 1/4 前端构建（含 tsc 类型检查）"
( cd ui && npm run build )

echo "==> 2/4 图标（ico/LostPath.ico 已在仓库里，仅在源图变更时才需重生成）"
if [ ! -f ico/LostPath.ico ]; then
  "$PY" tools/make_icon.py
fi

echo "==> 3/4 引擎单 exe"
"$PY" -m PyInstaller tools/engine.spec --noconfirm --distpath dist --workpath build
test -f dist/lostpath-engine.exe || { echo "引擎 exe 没打出来"; exit 1; }

echo "==> 4/4 安装包（NSIS）"
# CSC_IDENTITY_AUTO_DISCOVERY=false 是必需的：没有签名证书，不关掉 electron-builder
# 会去找证书然后失败。（另外两个镜像变量按需自备，见下方注释。）
#
# package.json 里 signAndEditExecutable=false 也是必需的：electron-builder 的
# winCodeSign 包解压时要为 macOS 的 dylib 建符号链接，而 Windows 上建符号链接需要
# 管理员或开发者模式。关掉它就不再下载那个包。
# **但它连带禁用了 rcedit**，而 rcedit 正是把图标写进 exe 的工具——所以图标改由
# desktop/after-pack.js 在 afterPack 阶段用 rcedit 自己嵌。少了那个钩子，安装包和
# 引擎 exe 都有图标，偏偏用户在开始菜单看到的 LostPath.exe 是 Electron 默认图标。
#
# 若报 "remove ...\app.asar: 被另一进程使用"：有 LostPath / 引擎进程还开着，或某个
# 终端的工作目录在 release/win-unpacked 里（Windows 会锁住进程的 CWD）。关掉即可，
# 或临时换个输出目录：npx electron-builder --win nsis --config.directories.output=../release2
cd desktop
# 镜像按需自备。中国大陆直连 GitHub 常常下不动 electron 与 electron-builder 的二进制，
# 那时在跑脚本前 export 这两个变量（npm 会继承下去）：
#   export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
#   export ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
# 默认不设：把某个地区的镜像写死在公开脚本里，对其他地区的人是净损失。
CSC_IDENTITY_AUTO_DISCOVERY=false \
  npm run dist

cd "$ROOT"
echo
# 产物默认落 release/（desktop/package.json 的 directories.output）。但上面注释里那个
# app.asar 被锁的绕道会把它输出到 release2/，所以这里两处都找——原先写死
# `ls -la release/*.exe` 在走过绕道之后会空手而归，配合 set -e 直接以非 0 退出，
# 看起来像"构建失败"，而其实包已经打好了，只是在另一个目录。
found=""
for d in release release2; do
  for f in "$d"/*.exe; do
    [ -e "$f" ] || continue
    found="yes"
    ls -la "$f"
  done
done
if [ -z "$found" ]; then
  echo "安装包没打出来：release/ 与 release2/ 里都没有 .exe" >&2
  exit 1
fi
echo "完成。"
