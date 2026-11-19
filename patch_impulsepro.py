#!/usr/bin/env python3
"""Impulse v2: wait for a pullback and a new break, then check flow, OI, and book depth."""
import os
import shutil
import py_compile

TARGET = "main.py"
BACKUP = "main.py.bak_impulsepro"

RELEASE = '''async def impulse_release_hold() -> None:
    """Do not buy the first print. A jump must pull back and break again, or run clean."""
    global _impulse_busy, _impulse_holding
    if _impulse_holding or not _impulse_hold:
        return
    _impulse_holding = True
    try:
        now = time.time()
        for sym, rec in list(_impulse_hold.items()):
            age = now - float(rec.get("ts") or 0)
            if age > 90.0:
                _impulse_hold.pop(sym, None)
                logger.info(f"🚫 IMPULSE no break {sym} | 90s")
                continue
            hist = _impulse_hist.get(sym)
            last = float(hist[-1][1]) if hist else 0.0
            pre = float(rec.get("pre") or 0)
            extreme = float(rec.get("px") or 0)
            side = rec.get("side")
            if last <= 0 or pre <= 0 or extreme <= 0 or side not in ("BUY", "SELL"):
                _impulse_hold.pop(sym, None)
                continue
            burst = (extreme - pre) / pre
            left = (last - pre) / pre
            if burst == 0:
                _impulse_hold.pop(sym, None)
                continue
            kept = left / burst
            if kept < 0.60:
                _impulse_hold.pop(sym, None)
                logger.info(f"🚫 IMPULSE hold drop {sym} | kept={kept:.0%}")
                continue
            off = (extreme - last) / extreme if side == "BUY" else (last - extreme) / extreme
            if off >= 0.0020:
                rec["pulled"] = True
            reclaimed = (side == "BUY" and last >= extreme) or (side == "SELL" and last <= extreme)
            runner = age >= 45.0 and kept >= 0.80 and (
                (side == "BUY" and last >= extreme * 1.0015)
                or (side == "SELL" and last <= extreme * 0.9985)
            )
            ready = age >= 20.0 and rec.get("pulled") and reclaimed
            if not ready and not runner:
                continue
            try:
                bars = await get_futures_klines_cached(symbol=sym, interval="1m", limit=2, closed_only=False)
            except Exception:
                bars = None
            if not bars:
                continue
            bar = bars[-1]
            vol = float(bar[5] or 0)
            buy = float(bar[9] or 0) if len(bar) > 9 else 0.0
            if vol <= 0:
                continue
            buy_frac = buy / vol
            if (side == "BUY" and buy_frac <= 0.50) or (side == "SELL" and buy_frac >= 0.50):
                _impulse_hold.pop(sym, None)
                logger.info(f"🚫 IMPULSE hold drop {sym} | taker buy={buy_frac:.0%}")
                continue
            oi_chg = None
            try:
                hist_oi = await client.futures_open_interest_hist(symbol=sym, period="5m", limit=3)
                vals = []
                for row in hist_oi or []:
                    raw = row.get("sumOpenInterestValue", row.get("sumOpenInterest", 0))
                    val = float(raw or 0)
                    if val > 0:
                        vals.append(val)
                if len(vals) >= 2 and vals[0] > 0:
                    oi_chg = (vals[-1] - vals[0]) / vals[0]
            except Exception:
                oi_chg = None
            if oi_chg is not None and oi_chg > 0.015:
                _impulse_hold.pop(sym, None)
                logger.info(f"🚫 IMPULSE oi chase {sym} | oi={oi_chg:+.2%}")
                continue
            if _impulse_busy or impulse_open_count() >= int(getattr(config, "IMPULSE_MAX_POSITIONS", 1)):
                continue
            if any(sym in trades for trades in active_trades.values()):
                _impulse_hold.pop(sym, None)
                continue
            why = "break" if ready else "runner"
            _impulse_hold.pop(sym, None)
            _impulse_busy = True
            _impulse_pending.add(sym)
            logger.info(
                f"✅ IMPULSE {why} {sym} | {side} kept={kept:.0%} taker={buy_frac:.0%}"
            )
            await impulse_enter(sym, side, last, float(rec.get("chg") or 0), float(rec.get("volx") or 0), pre)
            return
    finally:
        _impulse_holding = False

'''

HELPERS = '''
_impulse_hold = {}
_impulse_hold_at = 0.0
_impulse_holding = False

def impulse_arm_hold(now: float) -> None:
    global _impulse_hold_at
    if not _impulse_hold or now < _impulse_hold_at:
        return
    _impulse_hold_at = now + 2.0
    asyncio.create_task(impulse_release_hold())

''' + RELEASE

OLD_SCAN = '''    volx = traded / expected
    _impulse_next[sym] = now + 90.0
    side = "BUY" if chg > 0 else "SELL"
    global _impulse_busy
    if _impulse_busy or impulse_open_count() >= int(getattr(config, "IMPULSE_MAX_POSITIONS", 1)):
        return
    _impulse_pending.add(sym)
    _impulse_busy = True
    asyncio.create_task(impulse_enter(sym, side, px, chg, volx, prev[1]))
'''

NEW_SCAN = '''    volx = traded / expected
    _impulse_next[sym] = now + 90.0
    side = "BUY" if chg > 0 else "SELL"
    if _impulse_busy or impulse_open_count() >= int(getattr(config, "IMPULSE_MAX_POSITIONS", 1)):
        return
    if sym in _impulse_hold or sym in _impulse_wait:
        return
    _impulse_hold[sym] = {
        "side": side,
        "px": float(px),
        "pre": float(prev[1]),
        "chg": float(chg),
        "volx": float(volx),
        "ts": now,
    }
    logger.info(f"⏳ IMPULSE hold {sym} | {side} {chg:+.2f}% watch")
'''

OLD_LOOP = '''                        impulse_arm_confirm(now)
                        touch_activity()
'''

NEW_LOOP = '''                        impulse_arm_confirm(now)
                        impulse_arm_hold(now)
                        touch_activity()
'''

OLD_BOOK = '''        if spread > float(getattr(config, "IMPULSE_MAX_SPREAD", 0.0020)):
            logger.info(f"🚫 IMPULSE spread {symbol} | {spread:.3%}")
            return
'''

NEW_BOOK = '''        if spread > float(getattr(config, "IMPULSE_MAX_SPREAD", 0.0020)):
            logger.info(f"🚫 IMPULSE spread {symbol} | {spread:.3%}")
            return
        rows = book["asks"] if side == "BUY" else book["bids"]
        depth = 0.0
        for px_s, qty_s in (rows or [])[:5]:
            depth += float(px_s) * float(qty_s)
        if depth < 250.0:
            logger.info(f"🚫 IMPULSE thin book {symbol} | ${depth:.0f}")
            return
'''


def fail(msg):
    shutil.copy2(BACKUP, TARGET)
    print(msg)
    print(f"restored from {BACKUP} — bot file was not changed")


def main():
    if not os.path.exists(TARGET):
        print(f"STOPPED missing {TARGET}")
        return
    shutil.copy2(TARGET, BACKUP)
    text = open(TARGET, encoding="utf-8").read()
    if "IMPULSE no break" in text or "IMPULSE thin book" in text:
        fail("STOPPED the stronger jump is already in this file")
        return
    if OLD_BOOK not in text:
        fail("STOPPED book check was not found")
        return
    text = text.replace(OLD_BOOK, NEW_BOOK, 1)
    if "async def impulse_release_hold" in text:
        start = text.find("async def impulse_release_hold")
        end = text.find("\ndef impulse_scan_row", start)
        if start < 0 or end < 0:
            fail("STOPPED hold function was not found")
            return
        text = text[:start] + RELEASE + text[end + 1:]
        text = text.replace("% 30s\")", "% watch\")", 1)
        mode = "upgraded hold"
    else:
        if "def impulse_scan_row" not in text or OLD_SCAN not in text or OLD_LOOP not in text:
            fail("STOPPED impulse entry shape was not found")
            return
        text = text.replace("def impulse_scan_row", HELPERS + "def impulse_scan_row", 1)
        text = text.replace(OLD_SCAN, NEW_SCAN, 1)
        text = text.replace(OLD_LOOP, NEW_LOOP, 1)
        mode = "installed hold"
    if "IMPULSE no break" not in text or "IMPULSE thin book" not in text:
        fail("STOPPED result check failed")
        return
    open(TARGET, "w", encoding="utf-8").write(text)
    try:
        py_compile.compile(TARGET, doraise=True)
    except Exception as e:
        fail(f"STOPPED compile: {e}")
        return
    print(f"DONE pieces=2 mode={mode}")
    print(f"backup={BACKUP}")


if __name__ == "__main__":
    main()
