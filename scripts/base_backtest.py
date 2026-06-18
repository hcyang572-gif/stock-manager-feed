#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
기본(48h) 전략 백테스트 — 거래비용(거래세+수수료+슬리피지) 반영, net %수익 기준.

목적: 스캘퍼(분봉)와 대비해, **48h 보유 기본 전략**이 비용 차감 후에도 +기댓값인지,
      그리고 손절·목표 배수 그리드에서 **net 기댓값을 최대화**하는 조합을 데이터로 찾는다.

★데이터 정합성★ 실측 일봉만 사용(backtest.py 의 _collect_samples 재사용). 날조 없음.

방식:
- 관심종목 과거 ~8개월 일봉에서 점수≥55 가상 진입 표본 수집(backtest._collect_samples).
- 각 표본을 (stop_mult, target1_mult) 그리드로 levels() 계산 → 이후 2거래일(48h) 고저로
  목표/손절/시간청산 판정(backtest._evaluate, 목표1 절반익절+본전스톱 관리정책 포함).
- _evaluate 의 R(위험단위 손익)을 **%수익 = R × (risk/entry)** 로 환산하고, 왕복 거래비용을
  빼 net%를 구한다. 비용은 보유기간 무관(매수+매도 1회씩) ≈ 0.33%.

비용(왕복): 매도 거래세 0.20% + 수수료 0.015%×2 + 슬리피지(편도 0.05% 기본/0.10% 보수).
한계: 일봉 종가 점수·2거래일 보유 근사, 미국증시 환경 보정 제외, 슬리피지 가정값.

실행: python base_backtest.py
"""
import datetime
import json
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from analyze_technical import levels, load_control
from backtest import _collect_samples, _evaluate

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "base_backtest_result.json"

SELL_TAX = 0.0020
COMMISSION_RT = 0.00015 * 2
SLIP_BASE_RT = 0.0005 * 2
SLIP_HIGH_RT = 0.0010 * 2
COST_BASE = SELL_TAX + COMMISSION_RT + SLIP_BASE_RT   # ≈ 0.33%
COST_HIGH = SELL_TAX + COMMISSION_RT + SLIP_HIGH_RT   # ≈ 0.43%

# 현 기본 배수: stop_mult=1.5, target1_mult=2.0, target2_mult=3.0
GRID_SM = (1.0, 1.2, 1.5, 1.8, 2.1, 2.5)
GRID_T1 = (1.2, 1.5, 2.0, 2.5, 3.0)


def _pct_return(price, ind, fwd, sm, t1):
    """표본을 (sm,t1) 배수로 평가 → gross %수익(체결 안 되면 None)."""
    lv = levels(price, ind, 48, sm, t1, t1 + 1.0)
    entry, stop = lv["entry"], lv["stop"]
    risk = entry - stop
    if risk <= 0 or entry <= 0:
        return None
    outcome, r = _evaluate(entry, stop, lv["target1"], lv["target2"],
                           lv["etype"], fwd)
    if outcome in ("invalid", "no_fill") or r is None or not math.isfinite(r):
        return None
    # R(위험단위) → 실제 %수익: r × (risk/entry).
    return r * (risk / entry)


def _agg(samples, sm, t1):
    gs = []
    for (price, ind, fwd, sc) in samples:
        g = _pct_return(price, ind, fwd, sm, t1)
        if g is not None:
            gs.append(g)
    n = len(gs)
    if n == 0:
        return None
    net = [g - COST_BASE for g in gs]
    neth = [g - COST_HIGH for g in gs]
    return {
        "stop_mult": sm,
        "target1_mult": t1,
        "n": n,
        "gross_mean_pct": round(sum(gs) / n * 100, 4),
        "net_mean_pct": round(sum(net) / n * 100, 4),
        "net_mean_pct_highslip": round(sum(neth) / n * 100, 4),
        "gross_winrate": round(sum(1 for g in gs if g > 0) / n * 100, 1),
        "net_winrate": round(sum(1 for g in net if g > 0) / n * 100, 1),
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    watchlist, *_ = load_control()
    if not watchlist:
        print("[base] watchlist 비어있음 — 중단")
        return 0
    print(f"[base] 표본 수집 중 — {len(watchlist)}종목 일봉(점수≥55, 48h 보유)...")
    samples = _collect_samples(watchlist)
    print(f"[base] 표본 {len(samples)}건 · 비용(왕복) base {COST_BASE*100:.2f}% / "
          f"high {COST_HIGH*100:.2f}%")
    if not samples:
        print("[base] 표본 0 — 결과 없음")
        return 0

    results = []
    for sm in GRID_SM:
        for t1 in GRID_T1:
            r = _agg(samples, sm, t1)
            if r:
                results.append(r)
    results.sort(key=lambda r: r["net_mean_pct"], reverse=True)
    default = next((r for r in results
                    if r["stop_mult"] == 1.5 and r["target1_mult"] == 2.0), None)
    positive = [r for r in results if r["net_mean_pct"] > 0]

    print("\n=== 손절·목표 배수 그리드 (net 평균%수익 내림차순) ===")
    print(f"{'손절×':>5} {'목표1×':>6} {'n':>5} {'gross%':>8} {'net%':>8} "
          f"{'net승률':>7}")
    for r in results:
        mark = "  ←기본" if (r["stop_mult"] == 1.5 and
                            r["target1_mult"] == 2.0) else ""
        print(f"{r['stop_mult']:>5} {r['target1_mult']:>6} {r['n']:>5} "
              f"{r['gross_mean_pct']:>8} {r['net_mean_pct']:>8} "
              f"{r['net_winrate']:>7}{mark}")

    print(f"\n[결론] 비용 차감 후 net 평균%수익 +인 조합: {len(positive)} / {len(results)}")
    if default:
        print(f"[현 기본 1.5×/2.0×] gross {default['gross_mean_pct']}% · "
              f"net {default['net_mean_pct']}% (고슬립 "
              f"{default['net_mean_pct_highslip']}%) · net승률 {default['net_winrate']}%")
    if results:
        b = results[0]
        print(f"[최적] 손절×{b['stop_mult']}·목표1×{b['target1_mult']} → "
              f"net {b['net_mean_pct']}% · net승률 {b['net_winrate']}% (n={b['n']})")

    OUT_PATH.write_text(json.dumps({
        "status": "ok",
        "generated_at": datetime.datetime.now(KST).replace(microsecond=0).isoformat(),
        "cost_roundtrip_base_pct": round(COST_BASE * 100, 3),
        "samples": len(samples),
        "positive_net_combos": len(positive),
        "total_combos": len(results),
        "default_combo": default,
        "best_combo": results[0] if results else None,
        "all_combos": results,
        "note": "48h 보유 기본 전략. R→%수익 환산(r×risk/entry) 후 왕복비용 차감. "
                "목표1 절반익절+본전스톱 관리정책 반영. 실측 일봉만.",
        "disclaimer": "과거 실측 기반 참고용 — 미래 수익을 보장하지 않음.",
    }, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"\n[base] 저장 → {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
