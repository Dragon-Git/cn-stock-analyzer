"""
神华分析系统 - 数据缓存
======================
轻量级 SQLite 缓存，避免重复请求被封的 akshare/东财接口。
设计原则：
- 实时行情不缓存（每个时段必须拿）
- 历史 K 线按天缓存（一天请求一次即可）
- 板块指数按小时缓存
- 基本面/财务数据按周缓存
- 北向资金按日缓存
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认 cache 路径: 仓库根目录下的 data_cache.sqlite
DEFAULT_DB_PATH = Path(
    os.environ.get("SHENHUA_CACHE_DB", "/var/minis/workspace/shenhua-analyzer/data_cache.sqlite")
)


@contextmanager
def _conn(db_path: Path = DEFAULT_DB_PATH):
    """获取 sqlite 连接，自动建表"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path), timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,       -- cache key, e.g. "kline:601088:60d"
            data TEXT NOT NULL,         -- JSON string
            updated_at REAL NOT NULL,   -- unix timestamp
            ttl INTEGER NOT NULL        -- 秒级 TTL
        );
        CREATE INDEX IF NOT EXISTS idx_cache_updated ON cache(updated_at);
        """)
        c.commit()
        yield c
    finally:
        c.close()


def get(key: str) -> Optional[Any]:
    """读取缓存，过期返回 None"""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT data, updated_at, ttl FROM cache WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            age = time.time() - row["updated_at"]
            if age > row["ttl"]:
                logger.debug("cache miss (expired) key=%s age=%.0fs ttl=%ds",
                             key, age, row["ttl"])
                return None
            return json.loads(row["data"])
    except Exception as e:  # noqa: BLE001
        logger.warning("cache read failed key=%s: %s", key, e)
        return None


def set_(key: str, value: Any, ttl: int) -> None:
    """写入缓存（命名为 set_ 避免和内置 set 冲突）"""
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO cache(key, data, updated_at, ttl) VALUES(?, ?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False, default=str),
                 time.time(), ttl),
            )
            c.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("cache write failed key=%s: %s", key, e)


def cleanup_expired() -> int:
    """清理过期缓存，返回删除行数"""
    try:
        with _conn() as c:
            cur = c.execute(
                "DELETE FROM cache WHERE (updated_at + ttl) < ?", (time.time(),)
            )
            c.commit()
            return cur.rowcount
    except Exception as e:  # noqa: BLE001
        logger.warning("cache cleanup failed: %s", e)
        return 0


# ===== 业务级 helper =====

# 缓存 key 命名规范: "<type>:<symbol>:<tag>"
# TTL 秒数
TTL_HISTORY_KLINE = 6 * 3600           # 历史 K 线: 6 小时 (一天抓几次就够)
TTL_INDUSTRY_INDEX = 30 * 60           # 板块指数: 30 分钟
TTL_INDIVIDUAL_INFO = 7 * 24 * 3600    # 个股基本信息: 1 周
TTL_FINANCIAL = 7 * 24 * 3600          # 财务数据: 1 周
TTL_FUND_FLOW = 6 * 3600               # 资金流: 6 小时 (收盘后基本不变)


def kline_key(symbol: str, days: int, adjust: str) -> str:
    return f"kline:{symbol}:{days}:{adjust}"


def industry_key(sector_name: str) -> str:
    return f"industry:{sector_name}"


def info_key(symbol: str) -> str:
    return f"info:{symbol}"


def fund_flow_key(symbol: str) -> str:
    return f"fundflow:{symbol}"


def cache_stats() -> Dict[str, Any]:
    """查看缓存统计（debug 用）"""
    try:
        with _conn() as c:
            total = c.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            valid = c.execute(
                "SELECT COUNT(*) FROM cache WHERE (updated_at + ttl) >= ?",
                (time.time(),)
            ).fetchone()[0]
            size = Path(DEFAULT_DB_PATH).stat().st_size if DEFAULT_DB_PATH.exists() else 0
            return {"total": total, "valid": valid, "size_bytes": size,
                    "path": str(DEFAULT_DB_PATH)}
    except Exception as e:  # noqa: BLE001
        logger.warning("cache_stats failed: %s", e)
        return {}
