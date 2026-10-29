#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
银月动作注册器 —— 把 pet/yinyue-animation/ 里的新素材自动注册进 pet/yinyue-config.json

用法：
  python register_actions.py                     # 注册到已安装的 DSH（$DSH_HOME 或 ~/.dsh）
  python register_actions.py --dry-run           # 只看会做什么，不写盘
  python register_actions.py --pet-dir ./pet     # 直接操作指定目录（仓库里改配置用）
  python register_actions.py --anim-dir D:/tmp   # 素材不在默认位置时单独指定
  python register_actions.py --action-share 30   # 手工指定随机小动作占比（默认按数量自适应）

  素材默认位置 = <pet-dir>/yinyue-animation/
  配置默认位置 = <pet-dir>/yinyue-config.json

它做四件事：
  1. 按文件名前缀把新素材路由到对应池子（见下方 ROUTES）
  2. 已在配置里的动作跳过（幂等，反复跑不会重复）
  3. 清理「配置里引用了、但文件不存在」的条目（否则点击无反应 / 404）
  4. 按 pets[0].fixedEnabled 与动作数量重新配平 animationWeights 与 categories.weight
     —— 每次动画播完都会重抽，所以占比就等于频率。只有 1~2 个小动作时压到 15%
     （约半分钟一次），攒到 13 个以上才放到 75%。嫌吵/嫌少就 --action-share 手调。

命名规范（文件名 = 动画名，不含扩展名）：
  待机-xxx        → animations.idle
  回应-xxx        → animations.clicks
  转向-xxx        → animations.turn
  拖拽-xxx        → animations.drag
  移动-xxx        → animations.moves.actions
  小动作-xxx      → animations.categories（银月专属）
  仪态-xxx        → animations.categories（银月专属）
  俏皮-xxx        → animations.categories（银月专属）
  碎碎念-xxx      → animations.events.whisper
  工作-<档位>-xxx → animations.events.workStatus[档位]   档位 0-5，必须带
  余额-<档位>-xxx → animations.events.balance[档位]      档位 0-5，必须带

  档位含义（workStatus）：0 思考 / 1 工作 / 2 整理 / 3 等待 / 4 成功 / 5 出错
  balance 按余额已用百分比分档：0 <20% / 1 <40% / 2 <60% / 3 <80% / 4 <100% / 5 =100%

修改前会自动备份成 yinyue-config.json.bak-<时间戳>。
"""
import os
import re
import sys
import json
import glob
import shutil
import time

PREFIX = "yinyue"

# 无序池：前缀 → 路由目标
ROUTES = [
    ("待机", "idle"),
    ("回应", "clicks"),
    ("转向", "turn"),
    ("拖拽", "drag"),
    ("移动", "moves"),
    ("小动作", "categories"),
    ("仪态", "categories"),
    ("俏皮", "categories"),
    ("碎碎念", "whisper"),
]

# 有序池：前缀 + 档位号
INDEXED_RE = re.compile(r"^(工作|余额)-([0-5])-(.+)$")
INDEXED_POOL = {"工作": "workStatus", "余额": "balance"}

CATEGORY_ID = "银月专属"

# 序列化时，短于这个长度且只含标量的数组/对象保持单行（避免加一个动作就几百行 diff）
INLINE_MAX = 96


def dsh_home():
    env = os.environ.get("DSH_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".dsh")


def read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _inline_ok(x):
    """能不能一行写完：只含标量，且渲染后不太长。"""
    if isinstance(x, dict):
        kids = list(x.values())
    elif isinstance(x, list):
        kids = x
    else:
        return False
    if any(isinstance(k, (dict, list)) for k in kids):
        return False
    return len(json.dumps(x, ensure_ascii=False)) <= INLINE_MAX


def _inline_render(x):
    if isinstance(x, dict):
        return "{ " + ", ".join("%s: %s" % (json.dumps(k, ensure_ascii=False), _inline_render(v))
                                for k, v in x.items()) + " }"
    if isinstance(x, list):
        return "[" + ", ".join(_inline_render(v) for v in x) + "]"
    return json.dumps(x, ensure_ascii=False)


def _render(x, indent=0, root=False):
    """手写序列化：短数组/短对象保持单行，其余缩进展开。

    json.dump(indent=2) 会把 ["待机"] 拆成三行，导致加一个动作就产生几百行 diff。
    配置是给人看也给人改的，保持原作者的排版风格比「标准缩进」重要。
    root=True 时顶层各节之间留一个空行（和原文件一致）。
    """
    pad = " " * indent
    if isinstance(x, dict):
        if not x:
            return "{}"
        if _inline_ok(x):
            return _inline_render(x)
        parts = []
        for i, (k, v) in enumerate(x.items()):
            chunk = "%s  %s: %s" % (pad, json.dumps(k, ensure_ascii=False), _render(v, indent + 2))
            if root and i > 0 and isinstance(v, (dict, list)):
                chunk = "\n" + chunk
            parts.append(chunk)
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"
    if isinstance(x, list):
        if not x:
            return "[]"
        if _inline_ok(x):
            return _inline_render(x)
        body = ",\n".join("%s  %s" % (pad, _render(v, indent + 2)) for v in x)
        return "[\n" + body + "\n" + pad + "]"
    return json.dumps(x, ensure_ascii=False)


def write_json(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_render(data, root=True))
        f.write("\n")


def collect_all(cfg):
    """配置里已经引用到的全部动画名。"""
    a = cfg.get("animations") or {}
    refs = set()
    for k in ("idle", "turn", "drag", "clicks"):
        for x in (a.get(k) or []):
            if isinstance(x, str):
                refs.add(x)
    for act in ((a.get("moves") or {}).get("actions") or []):
        if isinstance(act, dict) and isinstance(act.get("name"), str):
            refs.add(act["name"])
    for cat in (a.get("categories") or []):
        for x in (cat.get("actions") or []):
            if isinstance(x, str):
                refs.add(x)
    for pool in ((a.get("events") or {}) or {}).values():
        for slot in (pool if isinstance(pool, list) else []):
            if isinstance(slot, str):
                refs.add(slot)
            elif isinstance(slot, list):
                refs.update(x for x in slot if isinstance(x, str))
    return refs


def route_of(stem):
    """文件名 → (池子, 档位或 None)。认不出返回 None。"""
    m = INDEXED_RE.match(stem)
    if m:
        return INDEXED_POOL[m.group(1)], int(m.group(2))
    for pfx, pool in ROUTES:
        if stem.startswith(pfx):
            return pool, None
    return None


def fallback_name(cfg, have):
    """兜底动画名。事件池里绝不能留空串（animationsValid 会判整段非法）。"""
    a = cfg.get("animations") or {}
    for x in (a.get("idle") or []):
        if isinstance(x, str) and x and x in have:
            return x
    if "待机" in have:
        return "待机"
    return sorted(have)[0] if have else "待机"


def add_to_pool(cfg, pool, slot, name, fb):
    """把 name 写进指定池子。返回 (结果, 旧值)，结果 ∈ added / replaced / skip。"""
    a = cfg.setdefault("animations", {})

    if pool in ("idle", "turn", "drag", "clicks"):
        lst = a.setdefault(pool, [])
        if name in lst:
            return "skip", None
        lst.append(name)
        return "added", None

    if pool == "moves":
        moves = a.setdefault("moves", {})
        acts = moves.setdefault("actions", [])
        if any(isinstance(x, dict) and x.get("name") == name for x in acts):
            return "skip", None
        acts.append({"name": name})
        return "added", None

    if pool == "categories":
        cats = a.setdefault("categories", [])
        cat = next((c for c in cats if isinstance(c, dict) and c.get("id") == CATEGORY_ID), None)
        if cat is None:
            cat = {"id": CATEGORY_ID, "weight": 0, "actions": []}
            cats.append(cat)
        acts = cat.setdefault("actions", [])
        if name in acts:
            return "skip", None
        acts.append(name)
        return "added", None

    if pool in ("whisper", "balance", "workStatus"):
        ev = a.setdefault("events", {})
        lst = ev.setdefault(pool, [])
        if slot is None:
            if name in lst:
                return "skip", None
            lst.append(name)
            return "added", None
        # 补位必须用真实动画名，空串会让 animationsValid 判整段非法
        while len(lst) <= slot:
            lst.append(fb)
        cur = lst[slot]
        if cur == name:
            return "skip", None
        if isinstance(cur, list):
            if name in cur:
                return "skip", None
            cur.append(name)
            return "added", None
        if isinstance(cur, str) and cur and cur != fb:
            lst[slot] = name
            return "replaced", cur
        lst[slot] = name
        return "added", None

    return "skip", None


def prune_missing(cfg, have, fb):
    """移除引用了不存在文件的条目。返回被移除的名字列表。

    事件池里的失效条目改成兜底动画名而不是留空 —— 空串会让整段 animations 失效。
    """
    removed = []
    a = cfg.get("animations") or {}

    def alive(x):
        if isinstance(x, str) and x and x not in have:
            removed.append(x)
            return False
        return True

    for k in ("idle", "turn", "drag", "clicks"):
        if isinstance(a.get(k), list):
            a[k] = [x for x in a[k] if alive(x)]

    for pool in (a.get("events") or {}).values():
        if not isinstance(pool, list):
            continue
        for i, slot in enumerate(pool):
            if isinstance(slot, str):
                if not alive(slot):
                    pool[i] = fb
            elif isinstance(slot, list):
                pool[i] = [x for x in slot if alive(x)] or [fb]

    if isinstance(a.get("moves"), dict):
        a["moves"]["actions"] = [x for x in (a["moves"].get("actions") or [])
                                 if isinstance(x, dict) and alive(x.get("name"))]
    for cat in (a.get("categories") or []):
        if isinstance(cat, dict):
            cat["actions"] = [x for x in (cat.get("actions") or []) if alive(x)]
    return removed


def action_share_for(total_actions):
    """随机小动作应该占多少比例。

    每次动画播完都会重新抽一次，所以「比例」直接等于频率：5 秒的动画、
    比例 60% ⇒ 平均每 8 秒就插一个，太吵。动作越多越敢放开，
    1~2 个时压到 15%（约半分钟一次），攒到 13 个以上才接近内置默认。
    """
    for cap, share in ((2, 15), (4, 30), (8, 50), (12, 65)):
        if total_actions <= cap:
            return share
    return 75


def balance(cfg, forced_share=None):
    """按 fixedEnabled 与动作数量配平 idle/turn/move 与 categories.weight。

    forced_share：手工指定随机小动作占比（0~100），给了就不走自适应。
    返回说明文本。
    """
    pets = cfg.get("pets") or []
    fixed = bool(pets[0].get("fixedEnabled")) if pets else True

    cats = (cfg.get("animations") or {}).get("categories") or []
    cats = [c for c in cats if isinstance(c, dict) and c.get("actions")]
    if not cats:
        # 没有分类可抽：权重全给 idle，避免全部落到 FALLBACK
        cfg.setdefault("animationWeights", {})["idle"] = 100
        cfg["animationWeights"]["turn"] = 0
        cfg["animationWeights"]["move"] = 0
        return "categories 为空 → animationWeights 保持 idle=100"

    total_actions = sum(len(c["actions"]) for c in cats)
    share = forced_share if forced_share is not None else action_share_for(total_actions)

    if fixed:
        turn, move = 0, 0
    else:
        # 会走会翻的宠物要留出转向/移动的份额
        turn, move = 5, 15
        share = min(share, 100 - turn - move)
    idle = 100 - turn - move - share

    # 按动作数量比例分 share，余数给第一个
    given = 0
    for i, c in enumerate(cats):
        if i == len(cats) - 1:
            c["weight"] = share - given
        else:
            w = round(share * len(c["actions"]) / total_actions)
            c["weight"] = w
            given += w

    cfg.setdefault("animationWeights", {})
    cfg["animationWeights"]["idle"] = idle
    cfg["animationWeights"]["turn"] = turn
    cfg["animationWeights"]["move"] = move
    detail = "、".join("%s=%d" % (c["id"], c["weight"]) for c in cats)
    return "animationWeights idle=%d turn=%d move=%d｜小动作占比 %d%%（%d 个动作）｜%s" % (
        idle, turn, move, share, total_actions, detail)


def opt(name):
    """取 --name <值>。没给返回 None。"""
    if name not in sys.argv:
        return None
    i = sys.argv.index(name)
    if i + 1 >= len(sys.argv) or sys.argv[i + 1].startswith("--"):
        sys.exit("✗ %s 后面缺一个值" % name)
    return sys.argv[i + 1]


def main():
    dry = "--dry-run" in sys.argv

    # 随机小动作占比：不给就走自适应（按动作数量）
    forced_share = None
    share_arg = opt("--action-share")
    if share_arg is not None:
        try:
            forced_share = int(share_arg)
        except ValueError:
            sys.exit("✗ --action-share 需要 0~100 的整数")
        if not 1 <= forced_share <= 100:
            sys.exit("✗ --action-share 需要在 1~100 之间"
                     "（0 关不掉：插件里 totalW || 1 会把 0 兜成 1，动作照播；"
                     "真要停就删掉 animations.categories）")

    # 目录：--pet-dir 优先（改仓库内的配置），否则用已安装的 DSH 数据根
    pet_dir = opt("--pet-dir")
    if pet_dir:
        pet_dir = os.path.abspath(os.path.expanduser(pet_dir))
    else:
        pet_dir = os.path.join(dsh_home(), "dsh-pet", "pet")

    cfg_path = os.path.join(pet_dir, PREFIX + "-config.json")
    anim_dir = opt("--anim-dir") or os.path.join(pet_dir, PREFIX + "-animation")
    anim_dir = os.path.abspath(os.path.expanduser(anim_dir))

    print("配置 :", cfg_path)
    print("素材 :", anim_dir)
    print()

    if not os.path.exists(cfg_path):
        print("✗ 找不到配置文件：" + cfg_path)
        print("  先运行 install.py 安装，或用 --pet-dir 指向仓库的 pet/ 目录")
        return 1
    if not os.path.isdir(anim_dir):
        print("✗ 找不到素材目录：" + anim_dir)
        return 1

    cfg = read_json(cfg_path)
    before = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    have = {os.path.splitext(f)[0] for f in os.listdir(anim_dir) if f.lower().endswith(".webm")}
    print("素材目录里有 %d 个 .webm" % len(have))

    referenced = collect_all(cfg)
    print("配置里已引用 %d 个" % len(referenced))
    fb = fallback_name(cfg, have)
    print("兜底动画：%s（用于补空档位 / 替换失效引用）" % fb)
    print()

    # 1) 新增 / 替换
    added = []
    replaced = []
    unknown = []
    for stem in sorted(have - referenced):
        r = route_of(stem)
        if r is None:
            unknown.append(stem)
            continue
        pool, slot = r
        res, old = add_to_pool(cfg, pool, slot, stem, fb)
        where = pool if slot is None else "%s[%d]" % (pool, slot)
        if res == "added":
            added.append((stem, where))
        elif res == "replaced":
            replaced.append((stem, where, old))

    # 2) 清理失效引用
    removed = prune_missing(cfg, have, fb)

    # 核心池被清空不会报错，但表现为「点了没反应 / 不待机」，提示一下
    warns = []
    for k, label in (("idle", "待机循环"), ("clicks", "点击回应"),
                     ("turn", "转向"), ("drag", "拖拽")):
        if isinstance((cfg.get("animations") or {}).get(k), list) and not cfg["animations"][k]:
            warns.append("%s 池（%s）是空的" % (k, label))

    # 3) 配平
    note = balance(cfg, forced_share)

    # 4) 自检：事件池里绝不能有空串 —— animationsValid 会因此判整段 animations 非法，
    #    插件随即回落内置默认动画（表现是「宠物突然变成默认外观」）
    bad = []
    for pname, pool in ((cfg.get("animations") or {}).get("events") or {}).items():
        if not isinstance(pool, list):
            bad.append(pname + "（不是数组）")
            continue
        if not pool:
            bad.append(pname + "（空池）")
        for i, s in enumerate(pool):
            if isinstance(s, str) and not s:
                bad.append("%s[%d]" % (pname, i))
            elif isinstance(s, list) and not s:
                bad.append("%s[%d]" % (pname, i))
    if bad:
        print("✗ 自检失败，未写盘：事件池有空槽 → %s" % "、".join(bad))
        print("  空串/空数组会让 dsh-pet 判整段 animations 非法，宠物会回落到内置默认外观。")
        return 1

    # ---- 报告 ----
    if added:
        print("新增注册：")
        for stem, where in added:
            print("  + %-24s → %s" % (stem, where))
    else:
        print("新增注册：无（没有发现未注册的素材）")

    if replaced:
        print("\n替换档位动画：")
        for stem, where, old in replaced:
            print("  ~ %-24s → %s（原：%s）" % (stem, where, old))

    if removed:
        print("\n清理失效引用（文件不存在，会 404）：")
        for x in removed:
            print("  - " + x)

    if unknown:
        print("\n⚠ 认不出归属，已跳过（请按命名规范改名）：")
        for x in unknown:
            print("  ? " + x)
        print("  规范见脚本顶部注释，或 ACTION_PROMPTS.md")

    if warns:
        print("\n⚠ 注意：")
        for w in warns:
            print("  · " + w)

    print("\n权重：" + note)

    if dry:
        print("\n（--dry-run，未写盘）")
        return 0

    if not added and not replaced and not removed and before == json.dumps(cfg, ensure_ascii=False, sort_keys=True):
        print("\n配置无变化，不写盘。")
        return 0

    bak = cfg_path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(cfg_path, bak)
    write_json(cfg_path, cfg)
    print("\n已写入。备份：%s" % os.path.basename(bak))
    print("生效：右键宠物 →「重载配置」（浏览器端刷新页面）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
