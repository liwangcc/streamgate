import pytest

from streamgate.rules import classify, normalize

from helpers import ev


@pytest.mark.parametrize("text,intent", [
    ("这个多少钱", "price"),
    ("多少钱", "price"),
    ("3号链接什么价", "price"),
    ("有没有优惠券", "coupon"),
    ("已经下单了，什么时候发货", "shipping"),
    ("包邮吗", "shipping"),
    ("还有货吗", "stock"),
    ("有XL码吗", "stock"),
    ("第一次来，怎么买", "howto_buy"),
    ("在哪里买", "howto_buy"),
    ("能退吗？七天无理由吗", "aftersales"),
    ("能演示一下吗", "demo"),
    ("什么材质的", "product_q"),
    ("主播你好", "welcome"),
    ("主播你好，多少钱", "price"),      # commerce beats greeting
    ("666", "spam"),
    ("哈哈哈哈", "spam"),
    ("好好好", "spam"),
    ("？？？", "noise"),
    ("[赞][赞]", "noise"),
    ("😂😂😂", "noise"),
    ("主播好漂亮", "chitchat"),
    ("保过吗", "risk"),
    ("能治好失眠吗", "risk"),
    ("是全网最低价吗", "risk"),
    ("能保证效果吗，多少钱", "risk"),   # risk beats price
    ("百分百有效吗", "risk"),
    ("100%正品吗", "risk"),
])
def test_intent_table(text, intent):
    assert classify(ev(1, text)).intent == intent


def test_first_time_viewer_is_not_an_absolute_claim():
    assert classify(ev(1, "第一次来，怎么买")).intent == "howto_buy"


def test_risk_kind_is_reported():
    assert classify(ev(1, "保过吗")).risk_kind == "guarantee"
    assert classify(ev(1, "能治好失眠吗")).risk_kind == "medical"
    assert classify(ev(1, "这样做违法吗")).risk_kind == "legal"
    assert classify(ev(1, "是全网最低价吗")).risk_kind == "absolute"


@pytest.mark.parametrize("text", ["吃了能长高吗", "小孩吃了没事吧", "喝了会失眠吗", "敏感肌能用吗",
                                  "老人心脏不好能用吗", "吃了会不会长胖", "服用有没有副作用", "能增高吗"])
def test_health_claim_questions_are_risk(text):
    assert classify(ev(1, text)).intent == "risk"


@pytest.mark.parametrize("text", ["主播吃饭了吗", "好吃吗", "这个能用多久", "宝宝能穿吗", "用了能退吗"])
def test_health_pattern_does_not_catch_ordinary_questions(text):
    assert classify(ev(1, text)).intent != "risk"


def test_operator_risk_words_extend_the_lexicon():
    assert classify(ev(1, "戴了能转运吗")).intent != "risk"
    c = classify(ev(1, "戴了能转运吗"), extra_risk=("转运",))
    assert c.intent == "risk" and c.risk_kind == "operator"


def test_slot_is_extracted():
    assert classify(ev(1, "3号链接多少钱")).slot == "3"
    assert classify(ev(1, "多少钱")).slot is None


def test_full_width_input_is_normalized():
    assert classify(ev(1, "多少钱？")).question is True
    assert normalize("ＡＢＣ１２３")[1] == "abc123"


def test_dedupe_key_collapses_character_runs():
    assert classify(ev(1, "6666666")).dedupe == classify(ev(2, "666")).dedupe


def test_event_kinds_map_to_intents():
    assert classify(ev(1, "送出了玫瑰", kind="gift", gift=1.0)).intent == "gift"
    assert classify(ev(1, "关注了主播", kind="follow")).intent == "welcome"
    assert classify(ev(1, "进入了直播间", kind="entrance")).intent == "welcome"
