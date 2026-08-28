"""
Unit test for slot_focus template rendering.

Renders the template for all 4 slot ids with synthetic data and verifies:
1. No exception is raised
2. Output is non-empty markdown containing the slot label
3. For pre_open: 集合竞价 / 09:25 must appear
4. For post_auction: 开盘缺口 must appear (with the gap value)
5. For noon: 上午 / 13:00 must appear
6. For post_close: 全日 K 线 must appear
"""
import sys
from pathlib import Path
from unittest.mock import patch

# Make src importable
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "src"))

import minijinja

# Import config (TIME_SLOTS drives our 4 cases)
from src.config import TIME_SLOTS, get_slot


TEMPLATE_PATH = Path(__file__).parent / "src" / "templates" / "slot_focus.md.j2"
template_str = TEMPLATE_PATH.read_text(encoding="utf-8")


def make_context(slot_id: str) -> dict:
    """Build a synthetic but realistic context for the template."""
    return {
        "slot_id": slot_id,
        "slot_meta": get_slot(slot_id),
        "price": 30.5,
        "trend_word": "震荡偏多",
        "rsi12": 65.0,
        "rsi_judgment": "中性偏多",
        "kdj_j": 75.0,
        "kdj_judgment": "超买区",
        "macd_h": 0.1234,
        "macd_judgment": "金叉延续",
        "adx": 22.0,
        "adx_judgment": "趋势中等",
        "main_net_inflow": 1.5e7,
        "main_net_inflow_wan": 1500,
        "main_inflow_direction": "流入",
        "fund_data_missing": False,
        "roe": 14.2,
        "report_date": "2026-06-30",
        "industry": "煤炭",
        "ma20": 29.8,
        "ma60": 28.2,
        "gap_pct": 0.85,  # only used by post_auction
    }


def render(slot_id: str) -> str:
    env = minijinja.Environment()
    env.add_filter("fmt2", lambda x: f"{x:.2f}" if x is not None else "N/A")
    env.add_filter("fmt1", lambda x: f"{x:.1f}" if x is not None else "N/A")
    env.add_filter("fmt4", lambda x: f"{x:.4f}" if x is not None else "N/A")
    env.add_filter("fmt0", lambda x: f"{x:,.0f}" if x is not None else "N/A")
    env.add_filter("fmtpct", lambda x: f"{x:+.2f}%" if x is not None else "N/A")
    return env.render_str(template_str, **make_context(slot_id))


def test_all_slots_render():
    """Verify all 4 slot ids render without exception."""
    print(f"Testing {len(TIME_SLOTS)} slots: {[s['id'] for s in TIME_SLOTS]}")
    assert len(TIME_SLOTS) == 4, f"Expected 4 slots, got {len(TIME_SLOTS)}"

    for slot in TIME_SLOTS:
        slot_id = slot["id"]
        out = render(slot_id)
        assert out, f"{slot_id}: empty output"
        assert slot["label"] in out, f"{slot_id}: label '{slot['label']}' missing"
        print(f"  ✓ {slot_id} ({slot['label']}) -> {len(out)} chars")


def test_pre_open_mentions_auction():
    out = render("pre_open")
    assert "集合竞价" in out, "pre_open: missing 集合竞价"
    assert "09:25" in out, "pre_open: missing 09:25"
    assert "MA20" in out, "pre_open: missing MA20 reference"
    print("  ✓ pre_open section content correct")


def test_post_auction_mentions_gap():
    out = render("post_auction")
    assert "开盘缺口" in out, "post_auction: missing 开盘缺口"
    assert "+0.85%" in out, f"post_auction: missing gap pct in output"
    assert "09:30" in out, "post_auction: missing 09:30 reference"
    print("  ✓ post_auction section content correct")


def test_noon_mentions_morning_close():
    out = render("noon")
    assert "上午已走完" in out, "noon: missing 上午已走完"
    assert "13:00" in out, "noon: missing 13:00"
    print("  ✓ noon section content correct")


def test_post_close_mentions_close():
    out = render("post_close")
    assert "全日 K 线" in out, "post_close: missing 全日 K 线"
    print("  ✓ post_close section content correct")


def test_missing_data_paths():
    """Verify template handles None values gracefully (no crash, no 'None' literal)."""
    ctx = make_context("post_close")
    ctx.update({"rsi12": None, "kdj_j": None, "macd_h": None, "adx": None,
                "main_net_inflow": None, "fund_data_missing": True,
                "roe": None, "industry": None})
    env = minijinja.Environment()
    env.add_filter("fmt2", lambda x: "N/A" if x is None else f"{x:.2f}")
    env.add_filter("fmt1", lambda x: "N/A" if x is None else f"{x:.1f}")
    env.add_filter("fmt4", lambda x: "N/A" if x is None else f"{x:.4f}")
    env.add_filter("fmt0", lambda x: "N/A" if x is None else f"{x:,.0f}")
    env.add_filter("fmtpct", lambda x: "N/A" if x is None else f"{x:+.2f}%")
    out = env.render_str(template_str, **ctx)
    assert "None" not in out, f"Output contains literal 'None':\n{out[:500]}"
    assert "ℹ️" in out, "Missing fallback markers in partial-data case"
    print("  ✓ None handling OK")


if __name__ == "__main__":
    test_all_slots_render()
    test_pre_open_mentions_auction()
    test_post_auction_mentions_gap()
    test_noon_mentions_morning_close()
    test_post_close_mentions_close()
    test_missing_data_paths()
    print("\nAll 6 tests passed.")
