"""Deterministic generator for a 30-minute, ~100 events/min live-commerce replay + gold labels.

Gold labels come from the *template category*, never from ``rules.py``. Some templates are
deliberately phrased so the keyword rules miss them ("hard" categories); the gap shows up
honestly in ``rules_vs_gold`` and the effective-reply metrics.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

# (label, should_reply, risk, weight, templates)
COMMENT_CATEGORIES: list[tuple[str, bool, bool, float, list[str]]] = [
    ("price", True, False, 8, [
        "这个多少钱", "多少钱", "多少钱啊", "价格多少", "{n}号多少钱", "{n}号链接什么价", "啥价",
        "到手价多少", "现在什么价", "贵不贵", "能便宜点吗", "怎么卖的", "{n}号多钱", "今天什么价格",
        "这个价格是多少呀", "直播间价格多少",
    ]),
    ("coupon", True, False, 3, [
        "有没有优惠券", "有券吗", "怎么领券", "有什么优惠", "能打折吗", "满减怎么算", "有赠品吗",
        "今天有福利吗", "新人有优惠吗",
    ]),
    ("howto_buy", True, False, 6, [
        "怎么买", "在哪里买", "哪里买", "链接在哪", "怎么下单", "{n}号链接在哪", "第一次来，怎么买",
        "怎么拍", "拍哪个链接", "从哪下单", "购物车第几个", "求链接",
    ]),
    ("shipping", True, False, 4, [
        "什么时候发货", "已经下单了，什么时候发货", "包邮吗", "发什么快递", "几天能到", "多久到货",
        "新疆发货吗", "今天拍明天能发吗", "运费多少", "顺丰吗",
    ]),
    ("stock", True, False, 4, [
        "还有货吗", "还有吗", "{color}的还有吗", "有{size}吗", "有几个颜色", "库存还有多少",
        "什么时候补货", "{size}码有吗", "卖完了吗", "有现货吗",
    ]),
    ("aftersales", True, False, 2, [
        "能退吗", "七天无理由吗", "有运费险吗", "坏了怎么办", "怎么退货", "保修多久", "售后怎么联系",
    ]),
    ("product_q", True, False, 5, [
        "什么材质的", "适合多大孩子", "主播多高多重", "{n}号和{m}号有什么区别", "能机洗吗", "会不会起球",
        "容量多大", "保质期多久", "是什么牌子", "重不重", "尺码偏大还是偏小", "s码能穿吗我九十斤",
        "这个适合送人吗",
    ]),
    ("demo", True, False, 6, [
        "能演示一下吗", "试穿一下", "再讲讲{n}号", "看看细节", "拿近一点看看", "上身效果怎么样",
        "展示一下背面", "讲解一下怎么用", "能试用一下吗", "介绍下这个",
    ]),
    # useful, phrased in ways the keyword rules do not know
    ("hard_useful", True, False, 3, [
        "给我来一单咋整", "跟昨天那个一样不", "{n}号还能抢不", "老板几号上新", "我要一个",
        "求讲{n}号", "{color}那款还能要不", "主播穿的是几号", "拍了能改地址不", "直接拍就行？",
    ]),
    ("greeting", False, False, 12, [
        "主播你好", "主播好", "大家好", "晚上好", "来了来了", "打卡", "我来了", "又来了", "hello",
        "嗨主播", "主播晚上好",
    ]),
    ("chitchat", False, False, 11, [
        "主播好漂亮", "今天好开心", "好可爱", "好看", "喜欢", "支持主播", "下次还来", "天气好冷",
        "主播声音好好听", "刚下班", "我在看", "这个背景好看",
    ]),
    # low value but question-shaped: likely false positives for any question detector
    ("chitchat_q", False, False, 3, [
        "主播吃饭了吗", "背景音乐叫什么", "主播哪里人", "今天播到几点", "主播几岁了", "你们在哪个城市",
    ]),
    ("spam", False, False, 18, [
        "666", "6666666", "111", "扣1", "哈哈哈哈哈", "冲冲冲", "好好好", "牛", "yyds", "[赞][赞][赞]",
        "啊啊啊啊", "888",
    ]),
    ("noise", False, False, 10, [
        "？？？", "。。。", "😂😂😂", "👍", "[微笑]", "!!!", "~~~", "[鼓掌][鼓掌]", "……", "？",
    ]),
    ("risk", False, True, 4, [
        "保过吗", "考试保过吗", "能治好失眠吗", "吃了能减肥吗", "孕妇可以用吗", "能根治吗",
        "是全网最低价吗", "能保证效果吗", "百分百有效吗", "过敏了怎么办", "治脱发吗",
        "离婚了财产怎么分", "这样做违法吗", "高血压能吃吗", "是不是全网第一", "无效退款吗",
    ]),
    # risky, phrased in ways the keyword rules do not know
    ("hard_risk", False, True, 1, [
        "敏感肌能用吗", "小孩吃了没事吧", "吃了能长高吗", "老人心脏不好能用吗",
    ]),
]

GIFTS = [("小心心", 0.1, 50), ("玫瑰", 0.1, 25), ("人气票", 0.1, 10), ("墨镜", 9.9, 10), ("跑车", 120.0, 4),
         ("嘉年华", 300.0, 1)]
COLORS = ["黑色", "白色", "米色", "灰色", "粉色", "卡其色"]
SIZES = ["S", "M", "L", "XL", "XXL", "大码"]
NAME_HEAD = ["小", "阿", "老", "", "", "大"]
NAME_BODY = ["林", "杰", "雨", "美", "周", "王", "李", "陈", "芳", "敏", "强", "磊", "静", "丽", "军", "洋",
             "勇", "艳", "娟", "涛", "明", "超", "秀", "霞", "平", "刚", "桂", "鹏", "婷", "宇"]
NAME_TAIL = ["", "", "", "姐", "哥", "酱", "子", "宝", "妈"]

# (start_min, end_min, events_per_min, phase) for a 30-minute stream; averages ~100/min
SEGMENTS = [(0, 4, 70, None), (4, 9, 95, None), (9, 12, 160, "sale"), (12, 19, 90, None),
            (19, 21, 150, "demo"), (21, 30, 92, None)]
PHASE_BOOST = {"sale": {"price": 3, "howto_buy": 3, "stock": 3, "coupon": 3},
               "demo": {"demo": 3, "product_q": 2}}


def _fill(t: str, rng: random.Random) -> str:
    n = rng.randint(1, 12)
    m = n % 12 + 1
    return t.format(n=n, m=m, color=rng.choice(COLORS), size=rng.choice(SIZES))


def _decorate(text: str, label: str, rng: random.Random) -> str:
    if label in ("spam", "noise"):
        return text
    r = rng.random()
    if r < 0.10:
        text = "主播，" + text
    elif r < 0.13:
        text = "老板 " + text
    r = rng.random()
    if r < 0.20 and not text.endswith(("？", "?")):
        text += "？"
    elif r < 0.25:
        text += "啊"
    elif r < 0.29:
        text += "[捂脸]"
    return text


def _users(rng: random.Random, n: int) -> list[dict]:
    users, seen = [], set()
    for i in range(n):
        if rng.random() < 0.15:
            name = f"用户{rng.randint(1000, 9999)}"
        else:
            name = rng.choice(NAME_HEAD) + rng.choice(NAME_BODY) + rng.choice(NAME_TAIL)
        if name in seen:
            name += str(i % 100)
        seen.add(name)
        tier = rng.choices(["regular", "new", "fan"], weights=[70, 15, 15])[0]
        users.append({"user_id": f"u_{20001 + i}", "username": name, "tier": tier})
    return users


def generate(seed: int = 20261102, minutes: float = 30.0, rate: float = 100.0) -> tuple[list[bytes], list[dict]]:
    rng = random.Random(seed)
    users = _users(rng, 900)
    chatty = users[:60]
    scale_t, scale_r = minutes / 30.0, rate / 100.0

    events: list[dict] = []
    gold: list[dict] = []
    last_comment: dict[str, tuple[float, str, str]] = {}
    for start, end, per_min, phase in SEGMENTS:
        t, t_end = start * 60.0 * scale_t, end * 60.0 * scale_t
        lam = per_min * scale_r / 60.0
        boost = PHASE_BOOST.get(phase, {})
        weights = [w * boost.get(label, 1) for label, _, _, w, _ in COMMENT_CATEGORIES]
        while True:
            t += rng.expovariate(lam)
            if t >= t_end:
                break
            u = rng.choice(chatty) if rng.random() < 0.3 else rng.choice(users)
            eid = f"s{len(events) + 1:05d}"
            ev = {"id": eid, "room_id": "synth", "platform": "douyin", "user_id": u["user_id"],
                  "username": u["username"], "text": "", "ts": round(t, 3), "kind": "comment",
                  "user_tier": u["tier"], "gift_value": 0.0, "raw": {}}
            k = rng.random()
            gift_p = 0.04 * (2 if phase == "demo" else 1)
            if k < gift_p:
                name, value, _ = rng.choices(GIFTS, weights=[g[2] for g in GIFTS])[0]
                count = rng.choice([1, 1, 1, 3, 5])
                ev.update(kind="gift", user_tier="gift", gift_value=round(value * count, 2),
                          text=f"送出了{name}x{count}")
                label, should, risk = "gift", True, False
            elif k < gift_p + 0.03:
                ev.update(kind="follow", text="关注了主播")
                label, should, risk = "follow", False, False
            elif k < gift_p + 0.10:
                ev.update(kind="entrance", text="进入了直播间")
                label, should, risk = "entrance", False, False
            else:
                prev = last_comment.get(u["user_id"])
                if prev and t - prev[0] < 90 and rng.random() < 0.07:
                    text, label = prev[1], prev[2]          # viewer repeats themself
                else:
                    cat = rng.choices(COMMENT_CATEGORIES, weights=weights)[0]
                    label = cat[0]
                    text = _decorate(_fill(rng.choice(cat[4]), rng), label, rng)
                spec = next(c for c in COMMENT_CATEGORIES if c[0] == label)
                should, risk = spec[1], spec[2]
                ev["text"] = text
                last_comment[u["user_id"]] = (t, text, label)
            events.append(ev)
            gold.append({"id": eid, "label": label, "should_reply": should, "risk": risk})

    # light timestamp jitter: ~1% of events arrive slightly out of order in the file
    for ev in events:
        if rng.random() < 0.01:
            ev["ts"] = round(max(0.0, ev["ts"] - rng.uniform(0.2, 1.5)), 3)

    lines = [json.dumps(ev, ensure_ascii=False).encode("utf-8") for ev in events]
    _inject_faults(lines, events, gold, rng)
    return lines, gold


def _inject_faults(lines: list[bytes], events: list[dict], gold: list[dict], rng: random.Random) -> None:
    """~1.2% garbage lines and ~0.5% adapter re-deliveries, inserted at random positions."""
    n_bad = max(1, round(len(events) * 0.012))
    n_redeliver = max(1, round(len(events) * 0.005))

    def near(i: int) -> dict:
        return dict(events[min(i, len(events) - 1)])

    makers = [
        lambda i, e: b'{"id": "bad_trunc", "text": "\xe5\xa4\x9a\xe5\xb0\x91\xe9\x92\xb1", "ts": ',
        lambda i, e: b"not json at all",
        lambda i, e: b"[1, 2, 3]",
        lambda i, e: b"null",
        lambda i, e: b"\xff\xfe\xfa broken utf-8",
        lambda i, e: _dump({**e, "id": f"bad{i}_nulltext", "text": None}),
        lambda i, e: _dump({**e, "id": f"bad{i}_numtext", "text": 12345}),
        lambda i, e: _dump({**e, "id": f"bad{i}_empty", "text": "   "}),
        lambda i, e: _dump({**e, "id": f"bad{i}_long", "text": "买" * 5000}),
        lambda i, e: _dump({**e, "id": f"bad{i}_strts", "ts": "abc"}),
        lambda i, e: _dump({**e, "id": f"bad{i}_negts", "ts": -5}),
        lambda i, e: _dump({**e, "id": f"bad{i}_like", "kind": "like", "text": "点赞了"}),
        lambda i, e: _dump({**e, "id": f"bad{i}_giftval", "kind": "gift", "user_tier": "gift",
                            "text": "送出了玫瑰x1", "gift_value": "lots"}),
        lambda i, e: _dump({"id": f"bad{i}_notext", "ts": e["ts"]}),
    ]
    for j in range(n_bad):
        pos = rng.randrange(1, len(lines))
        line = makers[j % len(makers)](j, near(pos))
        lines.insert(pos, line)
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("id"):
                long_text = isinstance(obj.get("text"), str) and len(obj["text"]) > 1000
                is_gift = obj.get("kind") == "gift" and obj.get("text")
                gold.append({"id": obj["id"], "label": "spam" if long_text else ("gift" if is_gift else "malformed"),
                             "should_reply": bool(is_gift), "risk": False})
        except (ValueError, UnicodeDecodeError):
            pass
    for _ in range(n_redeliver):
        pos = rng.randrange(1, len(lines) - 4)
        lines.insert(pos + rng.randint(1, 3), lines[pos])


def _dump(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def write(out: str | Path, seed: int = 20261102, minutes: float = 30.0, rate: float = 100.0) -> tuple[Path, Path, int]:
    lines, gold = generate(seed, minutes, rate)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\n".join(lines) + b"\n")
    gold_path = out.with_name(out.name.removesuffix(".jsonl") + ".gold.jsonl")
    gold_path.write_text("".join(json.dumps(g, ensure_ascii=False) + "\n" for g in gold), encoding="utf-8")
    return out, gold_path, len(lines)
