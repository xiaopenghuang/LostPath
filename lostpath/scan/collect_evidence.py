r"""快捷方式目标 exe 采集（只读）。产出 shortcuts.json 形态，供归因消费。

这是把"C 盘足迹"与"非 C 盘软件本体"连起来的唯一证据源（归因引擎 R4）：实测
187 条、97% 磁盘可验、126 条指向 G 盘，解决了注册表完全看不见的 Typora /
pgAdmin 4 / GamePP / GameViewer。

搬迁时删掉了两段 M0 一次性规则验证（Publisher 匹配覆盖力、rDNS 取段规则）：
它们的输入 attribution_v3.json 已随 probe/ 删除且无法再生成（v3 引擎源码早已
删除），而结论已经落在引擎里（R4/R5/R6）与 MEMORY.md。留着只会是跑不起来的
死代码。采集本身不依赖知识库，故此处不再 import KB。
"""
import argparse
import io
import json
import os
import re
import sys
from collections import defaultdict

from .. import sysdirs


def default_lnk_roots(user_home=None, program_data=None, public_user=None):
    r"""开始菜单与桌面四处——快捷方式实际存放位置。

    `user_home` / `program_data` 参数化与 `attribute_v4.footprint_roots` 同因：
    脱敏基准的路径是 `C:\Users\devuser`，硬编码则基准跑不起来。**别写死 `C:\`**——
    系统装 D 盘的机器上 ProgramData 与 Public 两处快捷方式会整批扫不到（静默漏掉，
    归因 R4 的唯一证据源就少一截）。`public_user` 取 `%PUBLIC%`，Windows 固定布局。
    """
    user = user_home or os.path.expanduser("~")
    pdata = program_data or sysdirs.program_data_dir()
    pub = public_user or (os.environ.get("PUBLIC") or
                          os.path.join(sysdirs.system_drive_root(), "Users", "Public"))
    return [
        os.path.join(user, r"AppData\Roaming\Microsoft\Windows\Start Menu"),
        os.path.join(pdata, r"Microsoft\Windows\Start Menu"),
        os.path.join(user, "Desktop"),
        os.path.join(pub, "Desktop"),
    ]


# 先粗抓候选，再按「存在于磁盘」筛选。上一版正则会把相邻两个路径粘成
# 'G:\GameViewerG:\GameViewer\GameViewer.exe'，故此处按盘符边界二次切分。
RAW_A = re.compile(rb"[A-Za-z]:\\[^\x00\r\n]{2,220}?\.exe", re.I)
RAW_W = re.compile(rb"(?:[A-Za-z]\x00)(?::\x00\\\x00)(?:[^\x00]\x00){2,220}?"
                   rb"\.\x00e\x00x\x00e\x00", re.I)
SPLIT_DRIVE = re.compile(r"(?=[A-Za-z]:\\)")


def clean_candidates(raw):
    """把可能粘连的路径按盘符边界切开，返回真实存在的那些。"""
    out = []
    for piece in SPLIT_DRIVE.split(raw):
        piece = piece.strip()
        if len(piece) < 8 or not re.match(r"^[A-Za-z]:\\", piece):
            continue
        if not piece.lower().endswith(".exe"):
            m = re.match(r"^(.*?\.exe)", piece, re.I)
            if not m:
                continue
            piece = m.group(1)
        out.append(piece)
    return out


def collect_shortcuts(roots=None, user_home=None):
    """扫 .lnk 抽目标 exe，返回 [{label, target, verified, lnk}]。

    不解析 lnk 结构而是正则抓路径：够用且不引第三方依赖。verified 表示目标 exe
    在磁盘上真实存在——归因据此区分"活软件"与"残留快捷方式"。
    """
    shortcuts = []
    seen = set()
    for root in (roots or default_lnk_roots(user_home)):
        if not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            for f in fn:
                if not f.lower().endswith(".lnk"):
                    continue
                p = os.path.join(dp, f)
                try:
                    with open(p, "rb") as fh:
                        blob = fh.read(64 * 1024)
                except OSError:
                    continue
                cands = []
                for m in RAW_A.finditer(blob):
                    cands += clean_candidates(m.group(0).decode("mbcs", "ignore"))
                for m in RAW_W.finditer(blob):
                    cands += clean_candidates(
                        m.group(0).decode("utf-16-le", "ignore"))
                if not cands:
                    continue
                # 优先取磁盘上真实存在的；否则取最长候选
                real = [c for c in cands if os.path.isfile(c)]
                tgt = max(real, key=len) if real else max(cands, key=len)
                label = os.path.splitext(f)[0]
                k = (label.lower(), tgt.lower())
                if k in seen:
                    continue
                seen.add(k)
                shortcuts.append({
                    "label": label,
                    "target": tgt,
                    "verified": bool(real),
                    "lnk": p,
                })
    return shortcuts


def main(argv=None):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ap = argparse.ArgumentParser(description="采集快捷方式目标 exe")
    ap.add_argument("-o", "--out", default="shortcuts.json")
    args = ap.parse_args(argv)

    shortcuts = collect_shortcuts()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(shortcuts, f, ensure_ascii=False, indent=1)

    ver = sum(1 for s in shortcuts if s["verified"])
    by_drive = defaultdict(int)
    for s in shortcuts:
        by_drive[s["target"][:2].upper()] += 1
    print(f"快捷方式 {len(shortcuts)} 条，目标 exe 磁盘可验 {ver} 条 "
          f"({ver / max(1, len(shortcuts)) * 100:.0f}%)")
    print(f"目标所在盘：{dict(sorted(by_drive.items(), key=lambda x: -x[1]))}")
    print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
