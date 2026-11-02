import pytest

from streamgate.config import Config
from streamgate.events import Event, Rejected, parse_line, parse_obj

from helpers import line

CFG = Config()


def test_valid_line_parses():
    e = parse_line(line(), 1, CFG)
    assert isinstance(e, Event)
    assert (e.id, e.ts, e.kind, e.user_id, e.username, e.text) == ("x1", 1.0, "comment", "u1", "小林", "多少钱")
    assert e.flags == ()


@pytest.mark.parametrize("raw", [b"not json", b'{"id": "a", "text": ', b"", b"\xff\xfe\xfa", b"{" * 5000])
def test_unparseable_lines_are_rejected_not_raised(raw):
    r = parse_line(raw, 7, CFG)
    assert isinstance(r, Rejected) and r.reason == "malformed" and r.seq == 7


@pytest.mark.parametrize("raw", [b"[1, 2]", b"null", b'"text"', b"42"])
def test_non_objects_are_malformed(raw):
    r = parse_line(raw, 1, CFG)
    assert isinstance(r, Rejected) and "expected a JSON object" in r.detail


@pytest.mark.parametrize("text", [None, 123, ["a"], {"t": 1}])
def test_bad_text_type_is_malformed(text):
    r = parse_line(line(text=text), 1, CFG)
    assert isinstance(r, Rejected) and r.reason == "malformed" and r.id == "x1"


def test_missing_text_is_malformed():
    assert isinstance(parse_obj({"id": "a", "ts": 1.0}, 1, CFG), Rejected)


@pytest.mark.parametrize("text", ["", "   ", "\t\n", "\u200b\u200b", "\x00\x01"])
def test_empty_text_is_malformed(text):
    r = parse_line(line(text=text), 1, CFG)
    assert isinstance(r, Rejected) and r.detail == "empty text"


def test_control_and_zero_width_characters_are_stripped():
    e = parse_line(line(text="多\u200b少\x00钱\ufeff"), 1, CFG)
    assert e.text == "多少钱"


def test_oversize_text_is_truncated_and_flagged():
    e = parse_line(line(text="买" * 5000), 1, CFG)
    assert isinstance(e, Event) and len(e.text) == CFG.max_text_chars and "truncated" in e.flags


@pytest.mark.parametrize("ts", [None, "abc", "1.5", -1, True, float("inf")])
def test_bad_timestamps_are_malformed(ts):
    obj = {"id": "a", "text": "hi", "kind": "comment"}
    if ts is not None:
        obj["ts"] = ts
    r = parse_obj(obj, 1, CFG)
    assert isinstance(r, Rejected) and r.reason == "malformed"


def test_nan_timestamp_from_json_is_malformed():
    assert isinstance(parse_line(b'{"id": "a", "text": "hi", "ts": NaN}', 1, CFG), Rejected)


def test_unknown_kind_is_unsupported():
    r = parse_line(line(kind="like"), 1, CFG)
    assert isinstance(r, Rejected) and r.reason == "unsupported" and r.ts == 1.0


def test_bad_gift_value_is_zeroed_and_flagged():
    e = parse_line(line(kind="gift", text="送出了玫瑰", gift_value="lots"), 1, CFG)
    assert isinstance(e, Event) and e.gift_value == 0.0 and "bad_gift_value" in e.flags


def test_gift_without_text_is_fine():
    e = parse_obj({"id": "g", "ts": 2.0, "kind": "gift", "gift_value": 10}, 1, CFG)
    assert isinstance(e, Event) and e.text == "" and e.gift_value == 10.0


def test_missing_ids_are_synthesized_per_event():
    a = parse_obj({"text": "多少钱", "ts": 1.0}, 3, CFG)
    b = parse_obj({"text": "多少钱", "ts": 1.0}, 4, CFG)
    assert a.id != b.id and a.user_id != b.user_id and "no_id" in a.flags and "no_user" in a.flags


def test_bom_and_bytes_are_accepted():
    assert isinstance(parse_line(b"\xef\xbb\xbf" + line(), 1, CFG), Event)


def test_live_arrival_time_overrides_client_ts():
    e = parse_obj({"id": "a", "text": "多少钱", "ts": "garbage"}, 1, CFG, arrival_ts=42.0)
    assert isinstance(e, Event) and e.ts == 42.0


def test_unknown_tier_falls_back_to_regular():
    e = parse_line(line(user_tier="vip9000"), 1, CFG)
    assert e.user_tier == "regular" and "bad_tier" in e.flags
