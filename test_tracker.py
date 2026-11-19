"""Tests for tracker.py.

Offline tests (default):   python3 -m pytest data/flexispot_price_tracker -q
Include the live page check: FLEXISPOT_LIVE=1 python3 -m pytest data/flexispot_price_tracker -q
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tracker as t  # noqa: E402

NOW = "2026-11-19T21:00:00"

# Trimmed copy of the real /sapi/mall-item/item/detail response (2026-11-19).
DETAIL = {
    "code": 200, "msg": "ok",
    "data": {
        "itemRenderTO": {
            "itemName": "Power Swivel Rocker Recliner with Waffle Back",
            "secKillFlag": True,
            "secKillFormalDescription": "Black Friday Early Bird Deals at the Lowest Prices of the Year!",
        },
        "shopSkuList": [{
            "salePrc": {"type": "global", "value": 419.990000},
            "originalPrc": {"type": "global", "value": 499.990000},
            "outOfStock": False,
            "name": "Power Swivel Rocker Recliner with Waffle Back",
        }],
    },
}


@pytest.mark.parametrize("text,expected", [
    ("£419.99", 419.99), ("£1,419.99 Including VAT", 1419.99), ("Discount £33", 33.0),
    ("", None), (None, None), ("no price", None),
])
def test_parse_money(text, expected):
    assert t.parse_money(text) == expected


def test_discount_pct():
    assert t.discount_pct(419.99, 499.99) == 16.0
    assert t.discount_pct(419.99, None) is None
    assert t.discount_pct(None, 499.99) is None


def test_parse_detail_json():
    s = t.parse_detail_json(DETAIL, "u", NOW)
    assert (s.sale_price, s.original_price, s.discount_pct) == (419.99, 499.99, 16.0)
    assert s.in_stock is True and s.source == "api"
    assert s.name.startswith("Power Swivel")
    assert "Black Friday" in s.promo_text


def test_parse_detail_json_empty():
    assert t.parse_detail_json({"code": 401, "msg": "Unauthorized"}, "u", NOW) is None


def test_parse_dom_prices():
    s = t.parse_dom_prices("£419.99", "£499.99", "Waffle", "u", NOW)
    assert (s.sale_price, s.original_price, s.source) == (419.99, 499.99, "dom")
    assert t.parse_dom_prices("", "£499.99", "Waffle", "u", NOW) is None


def test_history_and_change_detection(tmp_path):
    h = tmp_path / "hist.csv"
    first = t.parse_detail_json(DETAIL, "u", "2026-11-19T13:00:00")
    assert t.last_snapshot(h, "u") is None
    assert t.describe_change(None, first) == ("first check", None)
    t.append_snapshot(h, first)

    same = t.parse_detail_json(DETAIL, "u", NOW)
    msg, delta = t.describe_change(t.last_snapshot(h, "u"), same)
    assert delta == 0.0 and msg.startswith("unchanged")

    cheaper = t.parse_dom_prices("£379.99", "£499.99", "Waffle", "u", NOW)
    msg, delta = t.describe_change(t.last_snapshot(h, "u"), cheaper)
    assert delta == -40.0 and "DROPPED by £40.00" in msg
    t.append_snapshot(h, cheaper)

    # history keeps one header and is filtered per URL
    assert h.read_text().count("checked_at") == 1
    assert t.last_snapshot(h, "u")["sale_price"] == "379.99"
    assert t.last_snapshot(h, "other") is None


def test_main_end_to_end_offline(tmp_path, monkeypatch, capsys):
    snaps = iter([t.parse_detail_json(DETAIL, t.DEFAULT_URL, "2026-11-19T13:00:00"),
                  t.parse_dom_prices("£399.99", "£499.99", "Waffle", t.DEFAULT_URL, NOW)])
    monkeypatch.setattr(t, "fetch_snapshot", lambda url: next(snaps))
    notes = []
    monkeypatch.setattr(t, "notify", lambda title, msg, url="": notes.append(title) or ["test"])
    h = tmp_path / "h.csv"
    assert t.main(["--history", str(h)]) == 0
    assert t.main(["--history", str(h)]) == 0
    out = capsys.readouterr().out
    assert "first check" in out and "DROPPED by £20.00" in out
    assert notes == ["FlexiSpot: Price dropped £20.00 to £399.99"]


def _prev(price, in_stock="True", promo=""):
    return {"sale_price": str(price), "in_stock": in_stock, "promo_text": promo, "checked_at": "x"}


def _snap(price, in_stock=True, promo=""):
    s = t.parse_dom_prices(f"£{price}", "£499.99", "Waffle", "u", NOW)
    s.in_stock, s.promo_text = in_stock, promo
    return s


def test_alerts_for():
    # first check above target: nothing
    assert t.alerts_for(None, _snap(419.99), None, 380) == []
    # first check already under target: alert
    assert t.alerts_for(None, _snap(370), None, 380) == ["At or below your target £380.00: now £370.00"]
    # drop that crosses the target: both reasons
    r = t.alerts_for(_prev(419.99), _snap(379.99), -40.0, 380)
    assert len(r) == 2 and r[0].startswith("Price dropped £40.00")
    # still under target, unchanged: no repeat alert every 30 min
    assert t.alerts_for(_prev(379.99), _snap(379.99), 0.0, 380) == []
    # price rose: no alert
    assert t.alerts_for(_prev(399.99), _snap(419.99), 20.0, None) == []
    # stock and promo changes
    assert t.alerts_for(_prev(419.99, "False"), _snap(419.99, True), 0.0, None) == ["Back in stock"]
    assert t.alerts_for(_prev(419.99), _snap(419.99, False), 0.0, None) == ["Went OUT OF STOCK"]
    assert t.alerts_for(_prev(419.99, promo="A"), _snap(419.99, promo="B"), 0.0, None) == ["New promo: B"]


def test_notify_channels(monkeypatch):
    calls = []
    monkeypatch.setattr(t, "_post", lambda url, data, headers: calls.append((url, data, headers)))
    monkeypatch.setattr(t.sys, "platform", "linux")  # skip the mac desktop popup in tests
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC", "NTFY_SERVER"):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(t, k, "", raising=False)  # ignore values pasted into tracker.py
    assert t.notify("T", "M") == []

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    assert t.notify("FlexiSpot: drop", "Waffle £379.99", "https://x") == ["telegram", "ntfy"]
    tg, ntfy = calls
    assert tg[0] == "https://api.telegram.org/bot123:abc/sendMessage" and b"chat_id=42" in tg[1]
    assert ntfy[0] == "https://ntfy.sh/my-topic" and ntfy[1] == "Waffle £379.99".encode()
    assert ntfy[2]["Click"] == "https://x"


def test_notify_failure_does_not_crash(monkeypatch):
    def boom(*a):
        raise OSError("offline")
    monkeypatch.setattr(t, "_post", boom)
    monkeypatch.setattr(t.sys, "platform", "linux")
    monkeypatch.setenv("NTFY_TOPIC", "x")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(t, "TELEGRAM_BOT_TOKEN", "")
    assert t.notify("T", "M") == []


@pytest.mark.skipif(not os.environ.get("FLEXISPOT_LIVE"), reason="set FLEXISPOT_LIVE=1 to hit the real site")
def test_live_fetch():
    s = t.fetch_snapshot(t.DEFAULT_URL)
    assert s.sale_price and 50 < s.sale_price < 5000
    print(s)
