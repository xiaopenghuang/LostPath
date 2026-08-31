#!/bin/sh
# 把 tools/hooks/ 下的钩子装进 .git/hooks/。
#
# 为什么需要这一步：.git/hooks 不受版本控制，所以钩子源码放在 tools/hooks/ 里跟着
# 仓库走，装一次生效。换机器或重新 clone 后要再跑一次。
#
# 卸载：rm .git/hooks/pre-commit
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO/tools/hooks"
DST="$REPO/.git/hooks"

if [ ! -d "$DST" ]; then
    echo "找不到 $DST —— 这里不是 git 仓库？"
    exit 1
fi

for hook in "$SRC"/*; do
    name="$(basename "$hook")"
    cp "$hook" "$DST/$name"
    chmod +x "$DST/$name"
    echo "已装 $name"
done
echo ""
echo "验证：改点东西后 git commit，应看到 [pre-commit] 跑快测试套件…"
