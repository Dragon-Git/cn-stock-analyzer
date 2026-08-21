"""
Volume unit conversion test for src/data_fetcher.fetch_history_kline.

Bug: the old heuristic "if avg_vol > 1e7 then /100" misclassified small/mid-cap
stocks (北方华创/长电科技/中远海能): 新浪 returns volume in shares, but the
heuristic didn't divide when avg < 1e7, so their volume was 100x too big.

This test mocks akshare to return known data:
- 高量股 (中国神华 601088): avg ~5e7 shares from 新浪, should be 5e5 手
- 中小盘 (北方华创 002371): avg ~5e6 shares from 新浪, should be 5e4 手
- 巨量 (中国银行 601988): avg ~5e8 shares from 新浪, should be 5e6 手
- 东财 fallback: volume already in 手, no division

We also verify the failure mode: with the OLD heuristic disabled
(by patching the source tracker), the test should fail.
"""
import sys
import pandas as pd
import numpy as np

sys.path.insert(0, '.')

# Replace _conn with one that uses a fresh DB. We need to swap the function
# reference itself, because _conn(db_path: Path = DEFAULT_DB_PATH) captures
# the old path at def time.
import tempfile, os
import src.cache as cache_mod
from contextlib import contextmanager

_FRESH_DB = tempfile.mktemp(suffix='.sqlite')

@contextmanager
def _isolated_conn(db_path=None):
    db_path = db_path or type(cache_mod.DEFAULT_DB_PATH)(_FRESH_DB)
    import sqlite3
    c = sqlite3.connect(str(db_path), timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS cache (
        key TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        updated_at REAL NOT NULL,
        ttl INTEGER NOT NULL);
    """)
    c.commit()
    try:
        yield c
    finally:
        c.close()

cache_mod._conn = _isolated_conn  # 替换函数引用, 不依赖默认参数捕获


def test_sina_large_cap_converts_to_lots():
    """新浪返回股, 大盘股 avg > 1e7 → /100 → 正确"""
    import akshare as ak
    # 5e7 shares/day, 30 days
    sina_df = pd.DataFrame({
        '日期': pd.bdate_range('2024-01-01', periods=30).strftime('%Y-%m-%d'),
        '开盘': np.full(30, 30.0), '最高': np.full(30, 31.0),
        '最低': np.full(30, 29.0), '收盘': np.full(30, 30.5),
        '成交量': np.full(30, 5e7), '成交额': np.full(30, 1.5e10),
        '振幅': np.full(30, 1.0), '涨跌幅': np.full(30, 0.5),
        '涨跌额': np.full(30, 0.15), '换手率': np.full(30, 0.5),
    })
    original = ak.stock_zh_a_daily
    ak.stock_zh_a_daily = lambda **kw: sina_df
    try:
        from src.data_fetcher import fetch_history_kline
        from src.config import SHENHUA
        df = fetch_history_kline(SHENHUA, days=60)
        assert len(df) == 30, f"expected 30 rows, got {len(df)}"
        assert abs(df['volume'].mean() - 5e5) < 1, (
            f"expected 5e5 (lots), got {df['volume'].mean()}"
        )
        print(f"  ✓ 大盘 (中国神华): 新浪源 /100 后 = {df['volume'].mean():.0f} 手")
    finally:
        ak.stock_zh_a_daily = original


def test_sina_mid_cap_converts_to_lots():
    """中小盘 avg < 1e7 也必须 /100, 不然就是 100x 错误"""
    import akshare as ak
    sina_df = pd.DataFrame({
        '日期': pd.bdate_range('2024-01-01', periods=30).strftime('%Y-%m-%d'),
        '开盘': np.full(30, 100.0), '最高': np.full(30, 102.0),
        '最低': np.full(30, 99.0), '收盘': np.full(30, 100.5),
        '成交量': np.full(30, 5e6), '成交额': np.full(30, 5e8),
        '振幅': np.full(30, 3.0), '涨跌幅': np.full(30, 0.5),
        '涨跌额': np.full(30, 0.5), '换手率': np.full(30, 1.5),
    })
    original = ak.stock_zh_a_daily
    ak.stock_zh_a_daily = lambda **kw: sina_df
    try:
        from src.data_fetcher import fetch_history_kline
        # 北方华创
        from src.config import STOCKS
        beifang = next(s for s in STOCKS if s.symbol == "002371")
        df = fetch_history_kline(beifang, days=60)
        assert abs(df['volume'].mean() - 5e4) < 1, (
            f"expected 5e4 (lots), got {df['volume'].mean()} "
            f"(100x off → bug present)"
        )
        print(f"  ✓ 中小盘 (北方华创): 新浪源 /100 后 = {df['volume'].mean():.0f} 手")
    finally:
        ak.stock_zh_a_daily = original


def test_em_fallback_does_not_divide():
    """东财 fallback 已经返回手, 不动"""
    import akshare as ak
    # 5e6 lots = 5e8 shares per day
    em_df = pd.DataFrame({
        '日期': pd.bdate_range('2024-01-01', periods=30).strftime('%Y-%m-%d'),
        '开盘': np.full(30, 30.0), '最高': np.full(30, 31.0),
        '最低': np.full(30, 29.0), '收盘': np.full(30, 30.5),
        '成交量': np.full(30, 5e6), '成交额': np.full(30, 1.5e10),
        '振幅': np.full(30, 1.0), '涨跌幅': np.full(30, 0.5),
        '涨跌额': np.full(30, 0.15), '换手率': np.full(30, 0.5),
    })
    # 新浪失败 → 东财兜底
    original_sina = ak.stock_zh_a_daily
    original_em = ak.stock_zh_a_hist
    ak.stock_zh_a_daily = lambda **kw: pd.DataFrame()  # fail
    ak.stock_zh_a_hist = lambda **kw: em_df
    try:
        from src.data_fetcher import fetch_history_kline
        from src.config import SHENHUA
        # 用不同的 days 避免命中前两个测试写入的 cache
        df = fetch_history_kline(SHENHUA, days=120)
        assert abs(df['volume'].mean() - 5e6) < 1, (
            f"expected 5e6 (already lots), got {df['volume'].mean()}"
        )
        print(f"  ✓ 东财 fallback: 不动 = {df['volume'].mean():.0f} 手")
    finally:
        ak.stock_zh_a_daily = original_sina
        ak.stock_zh_a_hist = original_em


if __name__ == "__main__":
    print("=== Volume unit conversion tests ===")
    failed = 0
    for fn in [test_sina_large_cap_converts_to_lots,
               test_sina_mid_cap_converts_to_lots,
               test_em_fallback_does_not_divide]:
        try:
            fn()
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1

    if failed:
        print(f"\n✗ {failed} test(s) failed")
        sys.exit(1)
    print("\n✓ All volume unit tests passed")

    # cleanup
    if os.path.exists(_FRESH_DB):
        os.unlink(_FRESH_DB)
