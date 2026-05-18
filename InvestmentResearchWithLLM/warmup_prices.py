"""预热价格缓存：把所有持仓 ticker + FF 因子 ETF 的日线数据拉到本地 SQLite

用法：
  .venv/bin/python warmup_prices.py

建议：每天 UTC 23:00 cron 执行一次（美股收盘后 1 小时），保证用户访问时缓存命中。
免费 AV 每次 13s 间隔 → 20 个 ticker ≈ 5 分钟。
"""
import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 加载 .env
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

import data_fetcher  # noqa: E402


HOLDER_DB = os.getenv(
    "HOLDER_DB_PATH",
    "/opt/holder-action/data/trading.db",
)

# Fama-French 代理 ETF（固定）
FF_ETFS = ["SPY", "IWM", "IWD", "IWF", "MTUM"]


def _read_portfolio_tickers() -> list[str]:
    if not os.path.exists(HOLDER_DB):
        print(f"[warn] holder db not found: {HOLDER_DB}")
        return []
    conn = sqlite3.connect(HOLDER_DB)
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM positions WHERE status='open'"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


async def main():
    portfolio = _read_portfolio_tickers()
    # 去重 + 只保留 FMP 可解析的 ticker
    all_syms = set(FF_ETFS)
    for t in portfolio:
        fmp = data_fetcher._fmp_ticker(t)
        if fmp:
            all_syms.add(fmp)

    print(f"warming up {len(all_syms)} symbols: {sorted(all_syms)}")

    done = 0
    for sym in sorted(all_syms):
        cached = data_fetcher._price_cache_get(sym)
        if cached is not None:
            done += 1
            print(f"  [{done}/{len(all_syms)}] {sym} (cache hit, {len(cached)} days)")
            continue
        try:
            series = await data_fetcher._get_av_daily(sym)
            done += 1
            print(f"  [{done}/{len(all_syms)}] {sym} ({len(series)} days)")
        except Exception as e:
            done += 1
            print(f"  [{done}/{len(all_syms)}] {sym} FAILED: {e}")

    print("done.")


if __name__ == "__main__":
    asyncio.run(main())
