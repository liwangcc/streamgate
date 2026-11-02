"""Rule-based intent, risk and value classification.

Deterministic keyword rules, no model (see DEVLOG: "Rules, not an LLM, for scoring").
Order matters: risk is checked first so a risky question never slips in as a price
question; commerce intents come before greeting/spam so "主播你好，多少钱" is a price question.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentSpec:
    value: float      # base priority; 0 means never voiced
    ttl_s: float      # how long a pending item stays worth answering
    reply_chars: int  # typical spoken reply length (drives the airtime estimate)


INTENTS: dict[str, IntentSpec] = {
    "gift": IntentSpec(100, 90, 24),
    "howto_buy": IntentSpec(60, 45, 45),
    "price": IntentSpec(60, 45, 40),
    "shipping": IntentSpec(55, 45, 40),
    "stock": IntentSpec(55, 45, 35),
    "coupon": IntentSpec(50, 45, 40),
    "aftersales": IntentSpec(45, 60, 45),
    "demo": IntentSpec(40, 60, 70),
    "product_q": IntentSpec(35, 60, 50),
    "welcome": IntentSpec(10, 30, 24),
    "chitchat": IntentSpec(0, 0, 27),
    "spam": IntentSpec(0, 0, 27),
    "noise": IntentSpec(0, 0, 27),
    "risk": IntentSpec(0, 0, 45),
}

# Matched against NFKC-lowered text with whitespace removed (punctuation kept, for "100%").
RISK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "medical": (
        "治疗", "治好", "治愈", "根治", "能治", "治病", "包治", "疗效", "药效", "吃药", "药物",
        "处方", "偏方", "医生", "医院", "副作用", "过敏", "孕妇", "怀孕", "哺乳", "备孕",
        "失眠", "抑郁", "糖尿病", "高血压", "血压", "血糖", "降压", "降糖", "癌", "肿瘤",
        "减肥", "瘦身", "脱发", "生病", "疾病", "病情", "皮肤病", "长高", "增高", "长个", "敏感肌", "心脏",
    ),
    "legal": (
        "律师", "起诉", "违法", "犯法", "合法吗", "判刑", "坐牢", "离婚", "官司", "法院",
        "仲裁", "维权",
    ),
    "guarantee": (
        "保过", "包过", "保证", "包好", "包赚", "稳赚", "保本", "一定能", "百分百", "100%",
        "无效退款", "不过退款", "承诺",
    ),
    "absolute": (
        "最便宜", "最低价", "全网最", "全网第一", "销量第一", "排名第一", "行业第一", "最好的",
        "最强", "史上最", "绝对", "顶级", "国家级",
    ),
}

# Matched against the alphanumeric core. First match wins.
INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("aftersales", (
        "运费险", "退货", "退换", "换货", "售后", "七天无理由", "无理由", "质保", "保修",
        "退款", "坏了", "质量问题",
    )),
    ("shipping", (
        "发货", "快递", "物流", "几天到", "多久到", "几天能到", "多久能到", "包邮", "运费",
        "到货", "什么时候到", "啥时候到", "顺丰", "偏远",
    )),
    ("price", (
        "多少钱", "多钱", "价格", "价钱", "什么价", "啥价", "几块", "几元", "多少米", "怎么卖",
        "咋卖", "到手", "原价", "现价", "单价", "贵不贵", "贵吗", "便宜点", "便宜吗", "划算",
        "价位", "值不值",
    )),
    ("coupon", (
        "优惠", "券", "折扣", "打折", "满减", "赠品", "送什么", "福利", "秒杀", "红包",
    )),
    ("stock", (
        "有货", "没货", "现货", "库存", "还有吗", "还有没", "还剩", "补货", "缺货", "断货",
        "卖完", "抢完", "没了吗", "尺码", "码数", "几码", "大码", "小码", "颜色", "几个色",
        "什么色", "色号", "预售",
    )),
    ("howto_buy", (
        "怎么买", "咋买", "哪里买", "在哪买", "哪买", "怎么下单", "如何下单", "咋下单", "怎么拍",
        "咋拍", "拍哪个", "链接", "小黄车", "购物车", "上车", "怎么抢", "哪里拍", "从哪下单",
        "怎么购买", "想买", "我要买", "求链接",
    )),
    ("demo", (
        "演示", "讲解", "讲讲", "讲一下", "介绍", "展示", "试穿", "试用", "试一下", "试试",
        "上身", "看看", "看一下", "看下", "细节", "拿近", "近一点", "特写", "效果", "怎么用",
        "用法", "背面", "侧面",
    )),
)

# A greeting is a message made *only* of these pieces ("主播晚上好" yes, "主播好漂亮" no).
GREETING_PIECES = (
    "主播", "大家", "老板", "各位", "家人们", "宝宝们", "宝宝", "姐姐", "哥哥", "朋友们",
    "你好", "您好", "晚上好", "早上好", "中午好", "下午好", "晚安", "又来了", "我来了", "来了",
    "报到", "打卡", "哈喽", "hello", "hi", "嗨", "欢迎", "在吗", "在不在", "好",
    "啊", "呀", "哇", "呢", "哦", "吖", "们",
)
SPAM_TOKENS = (
    "666", "哈哈", "呵呵", "嘿嘿", "嘻嘻", "扣1", "扣2", "刷起来", "冲冲", "yyds", "nb", "牛",
    "绝绝子", "点赞",
)
QUESTION_WORDS = (
    "怎么", "什么", "啥", "哪", "几", "多久", "多大", "多重", "多长", "能不能", "可不可以",
    "有没有", "是不是", "会不会", "咋", "如何", "区别",
)
QUESTION_ENDINGS = ("吗", "呢", "么", "嘛", "不")

_BRACKET_EMOJI = re.compile(r"\[[^\[\]\s]{1,8}\]")   # Douyin text emoji such as [赞]
_WS = re.compile(r"\s+")
_RUNS = re.compile(r"(.)\1{2,}")
_SLOT = re.compile(r"(\d{1,3})号")
# "吃了能…/喝了会…/服用没事吧": any question about the effect of ingesting something is a health claim
_CONSUME_EFFECT = re.compile(r"(吃|喝|服用)了?(能|会|可以|没事|有事|有没有|对身体)")
_SIZE = re.compile(r"(?:xs|s|m|l|xl|xxl|xxxl|\d{2,3})码")   # "有XL码吗", "160码"


@dataclass(frozen=True)
class Classification:
    intent: str
    dedupe: str                  # normalized text used for duplicate detection
    slot: str | None = None      # product/link number such as "3" in "3号多少钱"
    risk_kind: str | None = None
    question: bool = False


def normalize(text: str) -> tuple[str, str]:
    """Return (flat, core): flat keeps punctuation, core keeps only letters/digits/CJK."""
    flat = unicodedata.normalize("NFKC", text).lower()
    flat = _WS.sub("", _BRACKET_EMOJI.sub("", flat))
    core = "".join(ch for ch in flat if ch.isalnum())
    return flat, core


def _risk_kind(flat: str, extra: tuple[str, ...] = ()) -> str | None:
    for kind, words in RISK_KEYWORDS.items():
        if any(w in flat for w in words):
            return kind
    if _CONSUME_EFFECT.search(flat):
        return "medical"
    if any(w and w in flat for w in extra):
        return "operator"
    return None


def _is_question(flat: str, core: str) -> bool:
    return (
        "?" in flat
        or core.endswith(QUESTION_ENDINGS)
        or any(w in core for w in QUESTION_WORDS)
    )


def _is_greeting(core: str) -> bool:
    rest = core
    for piece in _GREETING_ORDER:
        rest = rest.replace(piece, "")
    return not rest


_GREETING_ORDER = sorted(GREETING_PIECES, key=len, reverse=True)


def _is_spam(core: str) -> bool:
    return (
        core.isdigit()
        or (len(core) >= 2 and len(set(core)) == 1)
        or (len(core) <= 8 and any(t in core for t in SPAM_TOKENS))
    )


def classify(ev, extra_risk: tuple[str, ...] = ()) -> Classification:
    """``extra_risk``: operator-supplied risk words (Config.extra_risk_words), already NFKC-lowered."""
    if ev.kind == "gift":
        return Classification("gift", dedupe="gift")
    if ev.kind in ("follow", "entrance"):
        return Classification("welcome", dedupe=ev.kind)

    flat, core = normalize(ev.text)
    dedupe = _RUNS.sub(r"\1\1", core) or flat
    question = _is_question(flat, core)

    risk = _risk_kind(flat, extra_risk)
    if risk:
        return Classification("risk", dedupe, risk_kind=risk, question=question)
    if not core:
        return Classification("noise", dedupe)

    m = _SLOT.search(core)
    slot = m.group(1) if m else None
    for intent, words in INTENT_KEYWORDS:
        if any(w in core for w in words):
            return Classification(intent, dedupe, slot=slot, question=question)
    if _SIZE.search(core):
        return Classification("stock", dedupe, slot=slot, question=question)

    if _is_spam(core):
        return Classification("spam", dedupe)
    if _is_greeting(core):
        return Classification("welcome", dedupe)
    if question:
        return Classification("product_q", dedupe, slot=slot, question=True)
    return Classification("chitchat", dedupe)


def bigrams(s: str) -> frozenset[str]:
    return frozenset(s[i:i + 2] for i in range(len(s) - 1)) if len(s) > 1 else frozenset({s})


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)
