#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
银月 · Yinyue —— dsh-pet 独立宠物包（pet pack）安装脚本

把本仓库的 pet/ 目录装进 dsh-pet 的用户数据目录，重启 DSH 即可看到银月。

用法：
  python install.py              # 安装（或更新）
  python install.py --dry-run    # 只看会做什么，不写盘
  python install.py --verify     # 只检查已安装的那份是否合法
  python install.py --uninstall  # 卸载（只删本脚本装进去的文件）

安装目标：
  Windows   %USERPROFILE%\\.dsh\\dsh-pet\\pet\\yinyue-config.json
  macOS     ~/.dsh/dsh-pet/pet/yinyue-config.json
  Linux     ~/.dsh/dsh-pet/pet/yinyue-config.json

如果设置了环境变量 DSH_HOME，则用它作为 DSH 数据根（与插件 resolveDshHome() 同序）。

依赖：仅 Python 3.7+ 标准库。
"""
import os
import re
import sys
import json
import glob
import shutil
import filecmp

PREFIX = "yinyue"
HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PET = os.path.join(HERE, "pet")

CORNERS = {"top-left", "top-right", "bottom-left", "bottom-right"}
DISPLAYS = {"web", "desktop", "both", "none"}
ID_FORBIDDEN = re.compile(r"[\\/:\x00-\x1f]")


def dsh_home():
    env = os.environ.get("DSH_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".dsh")


def find_plugin(home):
    hits = sorted(glob.glob(os.path.join(home, "profiles", "*", "node_modules", "dsh-pet")))
    hits = [h for h in hits if os.path.isdir(h)]
    return hits[0] if hits else None


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        return {"__parse_error__": str(e)}


def collect_names(cfg):
    """配置里引用到的全部动画名。"""
    a = cfg.get("animations") or {}
    refs = set()
    for k in ("idle", "turn", "drag", "clicks"):
        refs |= {x for x in (a.get(k) or []) if isinstance(x, str)}
    for act in ((a.get("moves") or {}).get("actions") or []):
        if isinstance(act, dict) and isinstance(act.get("name"), str):
            refs.add(act["name"])
    for cat in (a.get("categories") or []):
        if isinstance(cat, dict):
            refs |= {x for x in (cat.get("actions") or []) if isinstance(x, str)}
    for pool in ((a.get("events") or {})).values():
        for slot in (pool if isinstance(pool, list) else []):
            if isinstance(slot, str):
                refs.add(slot)
    return refs


def check(cfg_path, anim_dir):
    """按 dsh-pet 0.3.8 的校验规则检查配置。返回 (hard, warn, refs, have)。"""
    hard, warn = [], []
    cfg = read_json(cfg_path)
    if "__parse_error__" in cfg:
        return ["配置文件不是合法 JSON：%s" % cfg["__parse_error__"]], [], set(), set()

    have = set()
    if os.path.isdir(anim_dir):
        have = {os.path.splitext(f)[0] for f in os.listdir(anim_dir) if f.lower().endswith(".webm")}

    pets = cfg.get("pets")
    if not isinstance(pets, list) or not pets:
        hard.append("pets 必须是非空数组")
        pets = []
    seen = set()
    for i, p in enumerate(pets):
        if not isinstance(p, dict):
            hard.append("pets[%d] 不是对象" % i)
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not pid.strip() or len(pid) > 64 or ID_FORBIDDEN.search(pid):
            hard.append("pets[%d].id 非法" % i)
        elif pid in seen:
            hard.append("pets[%d].id 重复：%s" % (i, pid))
        else:
            seen.add(pid)
        if not isinstance(p.get("size"), (int, float)) or isinstance(p.get("size"), bool) or p["size"] <= 0:
            hard.append("pets[%d].size 必须是正数" % i)
        if not isinstance(p.get("balanceEnabled"), bool):
            hard.append("pets[%d].balanceEnabled 必须是布尔" % i)
        if p.get("display") not in DISPLAYS:
            hard.append("pets[%d].display 必须是 web / desktop / both / none" % i)
        pos = p.get("position")
        if not isinstance(pos, dict):
            hard.append("pets[%d].position 必须是对象" % i)
        else:
            if pos.get("corner") not in CORNERS:
                hard.append("pets[%d].position.corner 非法" % i)

    a = cfg.get("animations")
    if not isinstance(a, dict):
        hard.append("animations 必须是对象")
    else:
        for k in ("idle", "turn", "drag", "clicks"):
            if not isinstance(a.get(k), list):
                hard.append("animations.%s 必须是数组" % k)
        ev = a.get("events")
        if not isinstance(ev, dict) or "balance" not in ev:
            hard.append("animations.events.balance 必填（余额档位池）")
        else:
            for name, pool in ev.items():
                if not isinstance(pool, list):
                    hard.append("animations.events.%s 必须是数组" % name)
        ws = (a.get("events") or {}).get("workStatus")
        if isinstance(ws, list) and len(ws) != 6:
            warn.append("events.workStatus 有 %d 档，应为 6 档（索引即档位）" % len(ws))

    w = cfg.get("animationWeights")
    if not isinstance(w, dict):
        hard.append("animationWeights 必须是对象")
    else:
        for k in ("idle", "turn", "move"):
            v = w.get(k)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                hard.append("animationWeights.%s 必须是非负数字" % k)

    refs = collect_names(cfg) if isinstance(a, dict) else set()
    missing = sorted(refs - have)
    if missing:
        hard.append("配置引用了不存在的素材（会 404 不播放）：%s" % "、".join(missing))

    return hard, warn, refs, have


def plan(dst_root):
    items = [(os.path.join(SRC_PET, PREFIX + "-config.json"),
              os.path.join(dst_root, PREFIX + "-config.json"))]
    anim_src = os.path.join(SRC_PET, PREFIX + "-animation")
    if os.path.isdir(anim_src):
        for f in sorted(os.listdir(anim_src)):
            if f.lower().endswith(".webm"):
                items.append((os.path.join(anim_src, f),
                              os.path.join(dst_root, PREFIX + "-animation", f)))
    return items


def main():
    dry = "--dry-run" in sys.argv
    uninstall = "--uninstall" in sys.argv
    verify_only = "--verify" in sys.argv

    home = dsh_home()
    dst_root = os.path.join(home, "dsh-pet", "pet")
    anim_dst = os.path.join(dst_root, PREFIX + "-animation")
    cfg_dst = os.path.join(dst_root, PREFIX + "-config.json")
    items = plan(dst_root)
    plugin = find_plugin(home)

    print("DSH_HOME   : %s" % home)
    print("安装目标   : %s" % dst_root)
    if plugin:
        ver = read_json(os.path.join(plugin, "package.json")).get("version", "?")
        print("dsh-pet    : 已安装 v%s" % ver)
    else:
        print("dsh-pet    : 未找到（%s/profiles/*/node_modules/dsh-pet）" % home)
        print("             请先安装 dsh-pet 插件，否则这份素材不会被加载。")
    print()

    if uninstall:
        n = 0
        for _, dst in items:
            if os.path.exists(dst):
                print("  删除", dst)
                if not dry:
                    os.remove(dst)
                n += 1
        if os.path.isdir(anim_dst) and not os.listdir(anim_dst):
            print("  删除空目录", anim_dst)
            if not dry:
                os.rmdir(anim_dst)
        print("\n%s %d 个文件" % ("将删除" if dry else "已删除", n))
        return 0

    if verify_only:
        hard, warn, refs, have = check(cfg_dst, anim_dst)
        print("校验已安装的那份：")
        print("  配置引用 :", "、".join(sorted(refs)) or "（无）")
        print("  素材已有 :", "、".join(sorted(have)) or "（无）")
        for w in warn:
            print("  · " + w)
        for e in hard:
            print("  ✗ " + e)
        if not hard:
            print("  ✓ 通过")
        return 0 if not hard else 1

    print("计划：")
    for src, dst in items:
        same = os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False)
        flag = "已是同文件，跳过" if same else ("覆盖" if os.path.exists(dst) else "新建")
        print("  %-12s %s" % (flag, os.path.basename(dst)))
    print()

    if dry:
        hard, warn, refs, have = check(os.path.join(SRC_PET, PREFIX + "-config.json"),
                                       os.path.join(SRC_PET, PREFIX + "-animation"))
        print("源目录预校验：")
        for w in warn:
            print("  · " + w)
        for e in hard:
            print("  ✗ " + e)
        if not hard:
            print("  ✓ 通过")
        print("\n（--dry-run，未写盘）")
        return 0

    for src, dst in items:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not (os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False)):
            shutil.copy2(src, dst)

    hard, warn, refs, have = check(cfg_dst, anim_dst)
    print("落地校验：")
    for w in warn:
        print("  · " + w)
    for e in hard:
        print("  ✗ " + e)
    if not hard:
        print("  ✓ 通过")
    print()
    print("装好了。刷新 DSH 页面（桌面窗口需右键宠物 →「重载配置」）即可看到银月。")
    if not plugin:
        print("注意：没找到 dsh-pet 插件，这些文件暂时不会被加载。")
    return 0 if not hard else 1


if __name__ == "__main__":
    sys.exit(main())
