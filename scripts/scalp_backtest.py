#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
스캘퍼 분봉 백테스트 — 거래비용(거래세+수수료+슬리피지) 반영.

목적: 앱 자동매매 '스캘퍼 모드'(목표0.8%/손절0.5%/추적0.2%/시간손절10분0.3%/최대30분)가
      실제 한국 분봉에서 **비용 차감 후에도 +기댓값**을 내는지 정직하게 검증한다.

★데이터 정합성★ 모든 수치는 yfinance 실측 분봉/일봉에서만 산출(날조 금지). 미확보 종목 제외.

── 모사 충실도(앱 auto_trade_engine.dart _entryPass/_entryTriggered/_exitDecision 기준) ──
- 진입(전략 A): 'now' 형 신호만(돌파대기 제외), 일봉 점수 ≥ 70(scalpMinScore).
  진입가 E = levels(price,ind).entry. 당일 분봉 중 가격이 E±0.5% 밴드에 처음 닿는 봉에서 체결.
  ★앱은 체결가 기준으로 손절/목표를 재계산★ → 손절 = E*(1-stop), 목표 = E*(1+target).
- 청산 우선순위(앱과 동일): 고정손절(-stop) → 추적손절(고점*(1-0.2%)) →
  목표(+target, 본전가드 E*1.0028 통과) → 시간손절(경과≥10분 & 종가<E*1.003) → 최대보유.
  한 봉 내 손절을 목표보다 먼저 보는 보수적 판정(기존 backtest.py 와 동일 철학).

── 비용 모델(왕복) ──
  매도 거래세 0.20%(2026 현행) + 수수료 0.015%×2 + 슬리피지(편도 0.05% 기본 / 0.10% 보수).
  net 수익률 = gross − 왕복비용.

── 한계(결과에 반드시 함께 보고) ──
  ① 진입 점수 게이트는 일봉(전일 종가까지, 룩어헤드 없음)으로 근사 — 장중 실시간 점수와 다를 수 있음.
  ② 5분봉 granularity — 5분 안의 짧은 꼬리(손절/목표 터치)를 놓쳐 **결과가 낙관적일 수 있음**.
  ③ yfinance KR 분봉: 5m=최근 약 60일·정규장(09:00~15:30)만. NXT 연장·1m 7일은 교차검증용.
  ④ 슬리피지는 가정값 — 실제 체결 미끄러짐은 종목·유동성·시점에 따라 다름.

실행: python scalp_backtest.py [interval=5m] [period=60d]
"""
import datetime
import json
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from analyze_technical import (
    _calc_indicators, score_stock, levels, load_control, DEFAULT_TUNING,
)

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "scalp_backtest_result.json"

SCALP_MIN_SCORE = 70      # trade_config scalpMinScore
ENTRY_BAND = 0.005        # ±0.5% (scalpEntryTenthPct=5)
TRAIL = 0.002             # 고점 -0.2% (scalpTrailTenthPct=2)
TIMESTOP_MIN = 10         # 분 (scalpTimeStopMin)
TIMESTOP_TARGET = 0.003   # +0.3% (scalpTimeStopMinTargetTenthPct=3)

# 비용(왕복)
SELL_TAX = 0.0020
COMMISSION_RT = 0.00015 * 2
SLIP_BASE_RT = 0.0005 * 2     # 편도 0.05%
SLIP_HIGH_RT = 0.0010 * 2     # 편도 0.10%
COST_BASE = SELL_TAX + COMMISSION_RT + SLIP_BASE_RT   # ≈ 0.33%
COST_HIGH = SELL_TAX + COMMISSION_RT + SLIP_HIGH_RT   # ≈ 0.43%

# 그리드(타깃·손절·최대보유·시간손절·추적손절)
GRID_TARGET = (0.005, 0.008, 0.010, 0.015, 0.020)
GRID_STOP = (0.003, 0.005, 0.008, 0.010)
GRID_MAXHOLD = (10, 30, 60)
GRID_TIMESTOP = (False, True)
# ★추적손절 스윕★ 0=끄기(고정손절+목표만), 그 외=고점 대비 %. 0.2%(현행)가 목표를
# 못 먹게 만드는지, 느슨하게 풀면 비용을 이기는지 검증.
GRID_TRAIL = (0.0, 0.002, 0.005, 0.010)


def _fetch_daily(yahoo, period="400d"):
    """일봉 → [(date, o, h, l, c, v)] 과거→현재. 실패 시 []."""
    try:
        import yfinance as yf
        h = yf.Ticker(yahoo).history(period=period, auto_adjust=False)
        rows = []
        for idx, r in h.iterrows():
            try:
                o, hi, lo, c, v = (float(r["Open"]), float(r["High"]),
                                   float(r["Low"]), float(r["Close"]),
                                   float(r["Volume"]))
                if not all(math.isfinite(x) for x in (o, hi, lo, c)):
                    continue
                rows.append((str(idx)[:10], o, hi, lo, c, v))
            except Exception:
                continue
        return rows
    except Exception:
        return []


def _fetch_intraday(yahoo, interval="5m", period="60d"):
    """분봉 → {date_str: [(ts_kst, o, h, l, c)...]} (시간순). 실패 시 {}."""
    try:
        import yfinance as yf
        h = yf.Ticker(yahoo).history(period=period, interval=interval,
                                     auto_adjust=False)
        by_day = {}
        for idx, r in h.iterrows():
            try:
                ts = idx.to_pydatetime()
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.timezone.utc)
                ts = ts.astimezone(KST)
                o, hi, lo, c = (float(r["Open"]), float(r["High"]),
                                float(r["Low"]), float(r["Close"]))
                if not all(math.isfinite(x) for x in (o, hi, lo, c)):
                    continue
                by_day.setdefault(ts.strftime("%Y-%m-%d"), []).append(
                    (ts, o, hi, lo, c))
            except Exception:
                continue
        for d in by_day:
            by_day[d].sort(key=lambda b: b[0])
        return by_day
    except Exception:
        return {}


def _resolve_symbol(code):
    """.KS / .KQ 중 분봉이 나오는 심볼 반환(없으면 None)."""
    for suf in (".KS", ".KQ"):
        daily = _fetch_daily(code + suf)
        if len(daily) >= 60:
            return code + suf, daily
    return None, None


def _eligible_entries(code, daily, intraday):
    """전략 A 진입 가능한 (날짜, 진입가 E, fill_ts, 이후봉들) 표본 목록.
    점수≥70 & etype=='now' & 당일 분봉이 E±0.5% 밴드 터치 시 체결로 본다."""
    sm = DEFAULT_TUNING["stop_mult"]
    t1 = DEFAULT_TUNING["target1_mult"]
    t2 = DEFAULT_TUNING["target2_mult"]
    date_to_didx = {d[0]: i for i, d in enumerate(daily)}
    fills = []
    stats = {"eligible_days": 0, "now_type": 0, "filled": 0, "no_fill": 0}
    for day, bars in sorted(intraday.items()):
        j = date_to_didx.get(day)
        if j is None or j < 30:
            continue
        window = daily[:j]              # 전일 종가까지(룩어헤드 없음)
        ind = _calc_indicators(
            [b[4] for b in window], [b[2] for b in window],
            [b[3] for b in window], [b[5] for b in window],
            [b[1] for b in window])
        if ind is None:
            continue
        price = window[-1][4]           # 전일 종가
        sc, _, _ = score_stock(price, ind)
        if sc < SCALP_MIN_SCORE:
            continue
        stats["eligible_days"] += 1
        lv = levels(price, ind, 48, sm, t1, t2)
        if lv["etype"] != "now":        # 전략 A = 즉시형만
            continue
        stats["now_type"] += 1
        E = lv["entry"]
        lo_band, hi_band = E * (1 - ENTRY_BAND), E * (1 + ENTRY_BAND)
        fill_idx = None
        for k, (ts, o, h, l, c) in enumerate(bars):
            if l <= hi_band and h >= lo_band:   # 밴드 터치
                fill_idx = k
                break
        if fill_idx is None:
            stats["no_fill"] += 1
            continue
        stats["filled"] += 1
        fill_ts = bars[fill_idx][0]
        after = bars[fill_idx + 1:]     # 체결 봉 다음부터 보수적 시뮬
        fills.append((day, E, fill_ts, after))
    return fills, stats


def _simulate(E, fill_ts, after, target, stop, maxhold, timestop_on, trail=TRAIL):
    """단일 체결을 청산 규칙으로 시뮬 → (gross_return, outcome)."""
    if not after:
        return 0.0, "no_bars"
    tgt = E * (1 + target)
    stp = E * (1 - stop)
    cur_high = E
    last_c = E
    for (ts, o, h, l, c) in after:
        last_c = c
        elapsed = (ts - fill_ts).total_seconds() / 60.0
        cur_high = max(cur_high, h)
        # 1) 고정손절(보수적: 한 봉 내 손절 먼저)
        if l <= stp:
            return (stp - E) / E, "stop"
        # 2) 추적손절(고점 대비) — trail<=0 이면 비활성
        if trail > 0 and cur_high > E and l <= cur_high * (1 - trail):
            exit_p = cur_high * (1 - trail)
            return (exit_p - E) / E, "trail"
        # 3) 목표(본전가드 통과)
        if h >= tgt:
            return (tgt - E) / E, "target"
        # 4) 시간손절(경과≥10분 & 종가 < E*1.003)
        if timestop_on and elapsed >= TIMESTOP_MIN and c < E * (1 + TIMESTOP_TARGET):
            return (c - E) / E, "timestop"
        # 5) 최대보유
        if elapsed >= maxhold:
            return (c - E) / E, "maxhold"
    return (last_c - E) / E, "eod"


def _agg_combo(fills, target, stop, maxhold, timestop_on, trail):
    grosses = []
    outcomes = {}
    for (day, E, fill_ts, after) in fills:
        g, oc = _simulate(E, fill_ts, after, target, stop, maxhold, timestop_on, trail)
        grosses.append(g)
        outcomes[oc] = outcomes.get(oc, 0) + 1
    n = len(grosses)
    if n == 0:
        return None
    gross_mean = sum(grosses) / n
    net_base = [g - COST_BASE for g in grosses]
    net_high = [g - COST_HIGH for g in grosses]
    return {
        "target_pct": round(target * 100, 2),
        "stop_pct": round(stop * 100, 2),
        "maxhold_min": maxhold,
        "timestop": timestop_on,
        "trail_pct": round(trail * 100, 2),
        "n": n,
        "gross_mean_pct": round(gross_mean * 100, 4),
        "net_mean_pct": round(sum(net_base) / n * 100, 4),
        "net_mean_pct_highslip": round(sum(net_high) / n * 100, 4),
        "gross_winrate": round(sum(1 for g in grosses if g > 0) / n * 100, 1),
        "net_winrate": round(sum(1 for g in net_base if g > 0) / n * 100, 1),
        "outcomes": outcomes,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    interval = sys.argv[1] if len(sys.argv) > 1 else "5m"
    period = sys.argv[2] if len(sys.argv) > 2 else "60d"

    watchlist, scope, mkts, hold_cap, tuning, _w, _oh = load_control()
    if not watchlist:
        print("[scalp] watchlist 비어있음 — 중단")
        return 0
    print(f"[scalp] 백테스트 시작 — {len(watchlist)}종목 · {interval}/{period} · "
          f"비용(왕복) base {COST_BASE*100:.2f}% / high {COST_HIGH*100:.2f}%")

    all_fills = []
    per_stock = []
    for it in watchlist:
        code = it.get("code", "")
        name = it.get("name", code)
        if not code:
            continue
        sym, daily = _resolve_symbol(code)
        if not sym:
            print(f"  - {name}({code}): 일봉 미확보 — 제외")
            continue
        intraday = _fetch_intraday(sym, interval, period)
        if not intraday:
            print(f"  - {name}({code}): {interval} 분봉 미확보 — 제외")
            continue
        fills, st = _eligible_entries(code, daily, intraday)
        per_stock.append({"code": code, "name": name, "symbol": sym,
                          "intraday_days": len(intraday), **st})
        all_fills.extend(fills)
        print(f"  - {name}({code}): 분봉 {len(intraday)}일 · 점수≥70&now {st['now_type']} "
              f"· 체결 {st['filled']} · 미체결 {st['no_fill']}")

    total_fills = len(all_fills)
    print(f"\n[scalp] 총 체결 표본 = {total_fills}건")
    if total_fills == 0:
        print("[scalp] 표본 0 — 결과 없음(점수≥70&now 진입이 분봉 구간에 없음)")
        OUT_PATH.write_text(json.dumps(
            {"status": "no_samples", "per_stock": per_stock},
            ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return 0

    # 그리드 스윕(추적손절 포함)
    results = []
    for tg in GRID_TARGET:
        for st_ in GRID_STOP:
            for mh in GRID_MAXHOLD:
                for ts_on in GRID_TIMESTOP:
                    for tr in GRID_TRAIL:
                        r = _agg_combo(all_fills, tg, st_, mh, ts_on, tr)
                        if r:
                            results.append(r)
    results.sort(key=lambda r: r["net_mean_pct"], reverse=True)

    # 기본값 조합(0.8/0.5/30분/시간손절ON/추적0.2%)
    default = next((r for r in results if r["target_pct"] == 0.8 and
                    r["stop_pct"] == 0.5 and r["maxhold_min"] == 30 and
                    r["timestop"] and r["trail_pct"] == 0.2), None)
    positive_net = [r for r in results if r["net_mean_pct"] > 0]

    print("\n=== 상위 12 조합 (net 평균수익률 내림차순) ===")
    print(f"{'목표%':>5} {'손절%':>5} {'추적%':>5} {'보유':>4} {'시손':>5} {'n':>4} "
          f"{'gross%':>8} {'net%':>8} {'net승률':>7} {'목표달성':>6}")
    for r in results[:12]:
        tgt_hit = r["outcomes"].get("target", 0)
        print(f"{r['target_pct']:>5} {r['stop_pct']:>5} {r['trail_pct']:>5} "
              f"{r['maxhold_min']:>4} {str(r['timestop']):>5} {r['n']:>4} "
              f"{r['gross_mean_pct']:>8} {r['net_mean_pct']:>8} "
              f"{r['net_winrate']:>7} {tgt_hit:>6}")

    print(f"\n[결론] 비용 차감 후 net 평균수익률 +인 조합: {len(positive_net)} / {len(results)}")
    if default:
        print(f"[기본값 0.8/0.5/30분/시간손절] gross {default['gross_mean_pct']}% · "
              f"net {default['net_mean_pct']}% (고슬립 {default['net_mean_pct_highslip']}%) · "
              f"net승률 {default['net_winrate']}% · outcomes {default['outcomes']}")

    OUT_PATH.write_text(json.dumps({
        "status": "ok",
        "generated_at": datetime.datetime.now(KST).replace(microsecond=0).isoformat(),
        "interval": interval, "period": period,
        "cost_roundtrip_base_pct": round(COST_BASE * 100, 3),
        "cost_roundtrip_high_pct": round(COST_HIGH * 100, 3),
        "total_fills": total_fills,
        "positive_net_combos": len(positive_net),
        "total_combos": len(results),
        "default_combo": default,
        "top_combos": results[:15],
        "per_stock": per_stock,
        "limitations": [
            "진입 점수게이트는 일봉(전일 종가) 근사 — 장중 실시간 점수와 다를 수 있음",
            "5분봉 granularity — 5분 내 짧은 손절/목표 꼬리 누락 가능(낙관 편향)",
            "yfinance 5m=약60일·정규장만(NXT 제외)",
            "슬리피지는 가정값(편도 0.05%/0.10%)",
        ],
        "disclaimer": "과거 실측 분봉 기반 참고용 — 미래 수익을 보장하지 않음.",
    }, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"\n[scalp] 저장 → {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
