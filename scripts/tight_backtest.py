#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎯 타이트 밴드 전략 백테스트 — 분봉 기반·48h 보유·거래비용 반영.

타이트 = 진입가 ±밴드 근처서 즉시 매수 + **매수가 기준 고정 %목표/손절**, 보유캡 48h,
장중 점검(앱은 30초). 기본(48h·ATR 배수)과 달리 목표/손절이 고정 %라, 일봉이 아니라
**분봉으로 48h 구간**을 따라가야 정확히 검증된다.

★데이터 정합성★ yfinance 실측 5분봉(약 60일·정규장)만 사용. 날조 없음.

방식:
- 관심종목 일봉으로 점수(≥55)·진입가 E(levels)·etype 산출(전일 종가까지·룩어헤드 없음).
- 분봉을 종목별 시간순 평탄화. 신호일 D 개장 이후 48h 내에서 가격이 E±밴드에 처음 닿으면
  매수(체결가=E 근사). 이후 분봉을 48h까지 따라가며 고정 손절(-stop)→목표(+target)
  (보수적 손절 우선) → 48h 시간청산.
- gross %수익 = (청산가-E)/E. net = gross − 왕복비용(≈0.33%).

비용(왕복): 매도 거래세 0.20% + 수수료 0.015%×2 + 슬리피지(편도 0.05% 기본/0.10% 보수).
한계: 진입 점수=일봉 근사, 5분봉 granularity(짧은 꼬리 누락→낙관 가능), 정규장만(NXT 제외),
      슬리피지 가정값. 기본(base_backtest)과 동일 비용·동일 종목군이라 상대비교에 적합.

실행: python tight_backtest.py [interval=5m] [period=60d]
"""
import datetime
import json
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from analyze_technical import _calc_indicators, score_stock, levels, load_control, DEFAULT_TUNING
from scalp_backtest import _resolve_symbol, _fetch_intraday

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "tight_backtest_result.json"

SCORE_CUTOFF = 55
HOLD = datetime.timedelta(hours=48)

SELL_TAX = 0.0020
COMMISSION_RT = 0.00015 * 2
SLIP_BASE_RT = 0.0005 * 2
SLIP_HIGH_RT = 0.0010 * 2
COST_BASE = SELL_TAX + COMMISSION_RT + SLIP_BASE_RT
COST_HIGH = SELL_TAX + COMMISSION_RT + SLIP_HIGH_RT

BAND = 0.01  # 진입 밴드 ±1.0%(tightEntryTenthPct=10)
GRID_TARGET = (0.010, 0.015, 0.020, 0.025, 0.030)  # +1.0~3.0%
GRID_STOP = (0.005, 0.008, 0.010, 0.015)            # -0.5~1.5%


def _collect_fills(code, daily, by_day):
    """타이트 진입 가능한 (E, fill_ts, 이후 48h 분봉들) 표본 + 통계."""
    sm = DEFAULT_TUNING["stop_mult"]
    t1 = DEFAULT_TUNING["target1_mult"]
    t2 = DEFAULT_TUNING["target2_mult"]
    flat = []
    for day in sorted(by_day):
        flat.extend(by_day[day])
    flat.sort(key=lambda b: b[0])
    date_to_didx = {d[0]: i for i, d in enumerate(daily)}
    fills = []
    st = {"eligible_days": 0, "filled": 0, "no_fill": 0}
    for day in sorted(by_day):
        j = date_to_didx.get(day)
        if j is None or j < 30:
            continue
        window = daily[:j]
        ind = _calc_indicators(
            [b[4] for b in window], [b[2] for b in window],
            [b[3] for b in window], [b[5] for b in window],
            [b[1] for b in window])
        if ind is None:
            continue
        price = window[-1][4]
        sc, _, _ = score_stock(price, ind)
        if sc < SCORE_CUTOFF:
            continue
        st["eligible_days"] += 1
        E = levels(price, ind, 48, sm, t1, t2)["entry"]
        lo_b, hi_b = E * (1 - BAND), E * (1 + BAND)
        # 신호일 개장 이후 첫 분봉 인덱스
        start = next((k for k, b in enumerate(flat)
                      if b[0].strftime("%Y-%m-%d") >= day), None)
        if start is None:
            continue
        t0 = flat[start][0]
        fill_idx = None
        for k in range(start, len(flat)):
            if flat[k][0] - t0 > HOLD:
                break
            if flat[k][3] <= hi_b and flat[k][2] >= lo_b:  # 밴드 터치
                fill_idx = k
                break
        if fill_idx is None:
            st["no_fill"] += 1
            continue
        st["filled"] += 1
        fills.append((E, flat[fill_idx][0], flat[fill_idx + 1:]))
    return fills, st


def _simulate(E, fill_ts, after, target, stop):
    if not after:
        return 0.0, "no_bars"
    tgt, stp = E * (1 + target), E * (1 - stop)
    last_c = E
    for (ts, o, h, l, c) in after:
        if ts - fill_ts > HOLD:
            return (last_c - E) / E, "timecut"
        last_c = c
        if l <= stp:                      # 보수적 손절 우선
            return (stp - E) / E, "stop"
        if h >= tgt:
            return (tgt - E) / E, "target"
    return (last_c - E) / E, "eod"        # 분봉 소진(48h 전 데이터 끝)


def _agg(fills, target, stop):
    gs, oc = [], {}
    for (E, fts, after) in fills:
        g, o = _simulate(E, fts, after, target, stop)
        gs.append(g)
        oc[o] = oc.get(o, 0) + 1
    n = len(gs)
    if n == 0:
        return None
    net = [g - COST_BASE for g in gs]
    neth = [g - COST_HIGH for g in gs]
    return {
        "target_pct": round(target * 100, 2),
        "stop_pct": round(stop * 100, 2),
        "n": n,
        "gross_mean_pct": round(sum(gs) / n * 100, 4),
        "net_mean_pct": round(sum(net) / n * 100, 4),
        "net_mean_pct_highslip": round(sum(neth) / n * 100, 4),
        "net_winrate": round(sum(1 for g in net if g > 0) / n * 100, 1),
        "outcomes": oc,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    interval = sys.argv[1] if len(sys.argv) > 1 else "5m"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"
    watchlist, *_ = load_control()
    if not watchlist:
        print("[tight] watchlist 비어있음 — 중단")
        return 0
    print(f"[tight] 시작 — {len(watchlist)}종목 · {interval}/{period} · 진입밴드 ±{BAND*100}% · "
          f"보유 48h · 비용 {COST_BASE*100:.2f}%")
    all_fills, per = [], []
    for it in watchlist:
        code = it.get("code", "")
        if not code:
            continue
        sym, daily = _resolve_symbol(code)
        if not sym:
            continue
        by_day = _fetch_intraday(sym, interval, period)
        if not by_day:
            continue
        fills, st = _collect_fills(code, daily, by_day)
        per.append({"code": code, "name": it.get("name", code), **st})
        all_fills.extend(fills)
        print(f"  - {it.get('name', code)}({code}): 신호일 {st['eligible_days']} · "
              f"체결 {st['filled']} · 미체결 {st['no_fill']}")
    n = len(all_fills)
    print(f"\n[tight] 총 체결 표본 = {n}건")
    if n == 0:
        print("[tight] 표본 0 — 결과 없음")
        return 0

    results = []
    for tg in GRID_TARGET:
        for sp in GRID_STOP:
            r = _agg(all_fills, tg, sp)
            if r:
                results.append(r)
    results.sort(key=lambda r: r["net_mean_pct"], reverse=True)
    default = next((r for r in results
                    if r["target_pct"] == 2.0 and r["stop_pct"] == 1.0), None)
    positive = [r for r in results if r["net_mean_pct"] > 0]

    print("\n=== 목표·손절 그리드 (net 평균%수익 내림차순) ===")
    print(f"{'목표%':>5} {'손절%':>5} {'n':>4} {'gross%':>8} {'net%':>8} {'net승률':>7} {'목표달성':>6}")
    for r in results:
        mark = "  ←기본" if (r["target_pct"] == 2.0 and r["stop_pct"] == 1.0) else ""
        print(f"{r['target_pct']:>5} {r['stop_pct']:>5} {r['n']:>4} {r['gross_mean_pct']:>8} "
              f"{r['net_mean_pct']:>8} {r['net_winrate']:>7} {r['outcomes'].get('target', 0):>6}{mark}")

    print(f"\n[결론] net+ 조합: {len(positive)} / {len(results)}")
    if default:
        print(f"[현 기본 목표2.0%/손절1.0%] gross {default['gross_mean_pct']}% · "
              f"net {default['net_mean_pct']}% (고슬립 {default['net_mean_pct_highslip']}%) · "
              f"net승률 {default['net_winrate']}% · {default['outcomes']}")
    if results:
        b = results[0]
        print(f"[최적] 목표{b['target_pct']}%/손절{b['stop_pct']}% → net {b['net_mean_pct']}% · "
              f"net승률 {b['net_winrate']}% (n={b['n']})")

    OUT_PATH.write_text(json.dumps({
        "status": "ok",
        "generated_at": datetime.datetime.now(KST).replace(microsecond=0).isoformat(),
        "interval": interval, "period": period, "band_pct": BAND * 100,
        "cost_roundtrip_base_pct": round(COST_BASE * 100, 3),
        "total_fills": n,
        "positive_net_combos": len(positive),
        "total_combos": len(results),
        "default_combo": default,
        "best_combo": results[0] if results else None,
        "all_combos": results,
        "per_stock": per,
        "limitations": ["진입 점수=일봉 근사", "5분봉 granularity(낙관 가능)",
                        "정규장만(NXT 제외)", "슬리피지 가정값"],
        "disclaimer": "과거 실측 분봉 기반 참고용 — 미래 수익 보장 안 함.",
    }, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"\n[tight] 저장 → {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
