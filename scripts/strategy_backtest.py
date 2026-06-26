#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
전략 백테스트 리포트 — 프리셋(점수·조합) × 기간(30/60/90일) 매트릭스.

재사용(중복 구현 금지):
  - backtest._evaluate, backtest._fetch_daily  : 48h 보유 결과·일봉 수집
  - scalp_backtest.COST_BASE, COST_HIGH        : 왕복 비용 상수(거래세+수수료+슬리피지)
  - analyze_technical._calc_indicators, score_stock, levels, load_control, DEFAULT_TUNING

1차 표본: signals_log.json 실발행 신호(보유창 경과분)
2차 표본: 관심종목 일봉 가상진입(점수≥55, 중복 제거 후 병합)

산출: strategy_backtest_result.json
  - 프리셋×기간 매트릭스 (누적순수익률·equity_curve·MDD·승률·평균R·손익비·avg_hold_hours·outcomes)
  - sample_ok=false 표기(표본 부족 시)
  - disclaimer 포함

데이터 정합성(필수): 실측 일봉만 사용. 날조 금지. 표본 부족 시 sample_ok=false 정직 표기.
"""
import datetime
import json
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH   = REPO_ROOT / "signals_log.json"
OUT_PATH   = REPO_ROOT / "strategy_backtest_result.json"

# ── 재사용 import ──────────────────────────────────────────────────────────
from backtest import _evaluate, _fetch_daily, HOLD_BARS          # noqa: E402
from scalp_backtest import COST_BASE, COST_HIGH                  # noqa: E402
from analyze_technical import (                                   # noqa: E402
    _calc_indicators, score_stock, levels, load_control, DEFAULT_TUNING,
)

# ── 상수 ──────────────────────────────────────────────────────────────────
MIN_SAMPLE      = 20          # 이 미만이면 sample_ok=false
SCORE_CUTOFF    = 55          # 가상진입 하한 (analyze_technical 기준과 동일)
PERIODS         = [30, 60, 90]
RISK_PER_TRADE  = 0.01        # 동일위험 비중: 손절 시 자본의 1% 위험(1R=1%).
                               # 포지션 비중 = RISK_PER_TRADE / risk_frac 으로 역산.
                               # → 손절 폭이 크면 작은 비중, 작으면 큰 비중 자동 조정.

# 프리셋 정의 (key·label·score_min·combo)
# combo: None=전체 / "vol_surge"=거래량급증(≥1.5x) / "foreign_buy"=외국인순매수
PRESETS = [
    {"key": "s55_all", "label": "점수55+ 전체",        "score_min": 55, "combo": None},
    {"key": "s65_all", "label": "점수65+",             "score_min": 65, "combo": None},
    {"key": "s75_all", "label": "점수75+",             "score_min": 75, "combo": None},
    {"key": "s65_for", "label": "점수65+·외인매수",    "score_min": 65, "combo": "foreign_buy"},
    {"key": "s65_vol", "label": "점수65+·거래량돌파",  "score_min": 65, "combo": "vol_surge"},
]


# ── 청산봉 추적 래퍼 ─────────────────────────────────────────────────────
# _evaluate는 (outcome, r)만 반환 — avg_hold_hours 계산을 위해 hold_bars 추가.
# 로직은 backtest._evaluate 와 완전히 동일하게 유지(수치 일관성).
def _evaluate_with_hold(entry, stop, t1, t2, etype, fwd, partial=0.5, breakeven=True):
    """backtest._evaluate 와 동일 판정 로직 + 청산 봉 수(hold_bars) 반환.
    반환: (outcome, r, hold_bars)  — no_fill/invalid 는 hold_bars=None."""
    risk = entry - stop
    if risk <= 0:
        return "invalid", None, None
    filled   = (etype == "now")
    rem      = 1.0
    realized = 0.0
    t1_hit   = False
    t2_hit   = False
    stopped  = False
    cur_stop = stop
    last_close = None
    hold_idx   = None

    for i, (o, h, l, c) in enumerate(fwd):
        last_close = c
        if not filled:
            if h >= entry:
                filled = True
            else:
                continue
        # 보수적: 한 봉 안에서 손절을 목표보다 먼저 본다.
        if l <= cur_stop:
            realized += rem * (cur_stop - entry) / risk
            rem = 0.0
            stopped = True
            hold_idx = i
            break
        if not t1_hit and h >= t1:
            realized += partial * (t1 - entry) / risk
            rem -= partial
            t1_hit = True
            if breakeven:
                cur_stop = entry
            if rem > 1e-9 and h >= t2:
                realized += rem * (t2 - entry) / risk
                rem = 0.0
                t2_hit = True
                hold_idx = i
                break
        elif t1_hit and h >= t2:
            realized += rem * (t2 - entry) / risk
            rem = 0.0
            t2_hit = True
            hold_idx = i
            break

    if not filled:
        return "no_fill", None, None
    if rem > 1e-9:
        lc_ok = last_close is not None and math.isfinite(last_close)
        realized += rem * ((last_close - entry) / risk if lc_ok else 0.0)
        hold_idx = len(fwd) - 1   # 시간청산: 마지막 봉

    r  = round(realized, 2) if math.isfinite(realized) else None
    hb = (hold_idx + 1) if hold_idx is not None else len(fwd)

    if t2_hit:   return "target2", r, hb
    if t1_hit:   return "target1", r, hb
    if stopped:  return "stop",    r, hb
    return "timecut", r, hb


# ── 관심종목 일봉 가상진입 표본 수집 ──────────────────────────────────────
def _collect_bt_samples(watchlist, lookback_days):
    """관심종목의 일봉에서 점수≥55인 날을 가상진입 표본으로 수집.
    수급(_sf)이 없으므로 for_sum=None(foreign_buy 프리셋에는 미포함).
    반환: list of sample dict."""
    sm  = DEFAULT_TUNING["stop_mult"]
    t1m = DEFAULT_TUNING["target1_mult"]
    t2m = DEFAULT_TUNING["target2_mult"]
    samples = []
    for it in watchlist:
        code = it.get("code", "")
        if not code:
            continue
        bars = _fetch_daily(code + ".KS") or _fetch_daily(code + ".KQ")
        if len(bars) < 60:
            continue
        n = len(bars)
        # lookback 기간만 탐색(+20 버퍼: 지표 안정화 마진)
        start = max(25, n - lookback_days - 20)
        for i in range(start, n - HOLD_BARS):
            window = bars[:i + 1]
            ind = _calc_indicators(
                [b[4] for b in window], [b[2] for b in window],
                [b[3] for b in window], [b[5] for b in window],
                [b[1] for b in window])
            if ind is None:
                continue
            price = bars[i][4]
            sc, _, _ = score_stock(price, ind)
            if sc < SCORE_CUTOFF:
                continue
            lv  = levels(price, ind, 48, sm, t1m, t2m)
            fwd = [(b[1], b[2], b[3], b[4]) for b in bars[i + 1:i + 1 + HOLD_BARS]]
            if not fwd:
                continue
            samples.append({
                "date":           bars[i][0],
                "code":           code,
                "score":          sc,
                "vol_surge_mult": ind.get("vol_surge", 0.0),
                "for_sum":        None,   # 일봉 가상진입 — 수급 미확보
                "etype":          lv["etype"],
                "entry":          lv["entry"],
                "stop":           lv["stop"],
                "t1":             lv["target1"],
                "t2":             lv["target2"],
                "fwd":            fwd,
                "source":         "daily_backtest",
            })
    return samples


# ── signals_log 실발행 표본 수집 ──────────────────────────────────────────
def _collect_log_samples():
    """signals_log.json의 실발행 신호(보유창 경과분)를 표본으로 수집.
    점수는 해당 날짜 일봉으로 재계산. 진입/손절/목표는 실발행값 사용.
    수급 없으므로 for_sum=None."""
    if not LOG_PATH.exists():
        print("[strategy_backtest] signals_log.json 없음 — 실발행 표본 건너뜀")
        return []
    try:
        raw = json.loads(LOG_PATH.read_text(encoding="utf-8-sig"))
    except Exception as ex:
        print(f"[strategy_backtest] signals_log 읽기 실패: {ex}")
        return []

    entries = raw if isinstance(raw, list) else raw.get("signals", [])
    now     = datetime.datetime.now(KST)
    samples = []
    skip    = 0

    for e in entries:
        try:
            issued = datetime.datetime.fromisoformat(e["issued_at"])
        except Exception:
            skip += 1
            continue
        cap = float(e.get("hold_cap_hours", 48))
        if (now - issued).total_seconds() < cap * 3600:
            continue   # 아직 보유창 미경과

        code = e.get("code", "")
        if not code:
            skip += 1
            continue

        d0   = str(issued)[:10]
        bars = (_fetch_daily(code + ".KS", period="200d") or
                _fetch_daily(code + ".KQ", period="200d"))
        if len(bars) < 30:
            skip += 1
            continue

        # 해당 날짜 일봉 인덱스
        idx = next((j for j, b in enumerate(bars) if b[0] >= d0), None)
        if idx is None or idx < 25:
            skip += 1
            continue

        window = bars[:idx + 1]
        ind = _calc_indicators(
            [b[4] for b in window], [b[2] for b in window],
            [b[3] for b in window], [b[5] for b in window],
            [b[1] for b in window])
        if ind is None:
            skip += 1
            continue

        price = bars[idx][4]
        sc, _, _ = score_stock(price, ind)

        # 실발행 진입/손절/목표값 사용(재계산 아님)
        try:
            entry = float(e["entry"])
            stop  = float(e["stop"])
            t1    = float(e["target1"])
            t2    = float(e["target2"])
        except Exception:
            skip += 1
            continue
        if entry <= 0 or stop <= 0 or t1 <= 0 or t2 <= 0:
            skip += 1
            continue

        fwd = [(b[1], b[2], b[3], b[4]) for b in bars[idx + 1:idx + 1 + HOLD_BARS]]
        if not fwd:
            skip += 1
            continue

        samples.append({
            "date":           d0,
            "code":           code,
            "score":          sc,
            "vol_surge_mult": ind.get("vol_surge", 0.0),
            "for_sum":        None,   # 수급 미확보
            "etype":          e.get("entry_type", "now"),
            "entry":          entry,
            "stop":           stop,
            "t1":             t1,
            "t2":             t2,
            "fwd":            fwd,
            "source":         "signals_log",
        })

    print(f"    signals_log: {len(entries)}건 중 {len(samples)}건 수집, {skip}건 건너뜀")
    return samples


# ── 프리셋 필터 ─────────────────────────────────────────────────────────────
# combo 버킷 정의는 analyze_technical.combo_bucket 과 동일 기준을 직접 적용.
#   foreign_buy: for_sum > 0  (순매수)
#   vol_surge:   vol_surge_mult ≥ 1.5  (combo_bucket 기준 '급증')
def _matches_preset(s, preset):
    if s["score"] < preset["score_min"]:
        return False
    combo = preset.get("combo")
    if combo == "foreign_buy":
        if s["for_sum"] is None:
            return False   # 수급 미확보 → 이 프리셋에 미포함
        return s["for_sum"] > 0
    if combo == "vol_surge":
        return s["vol_surge_mult"] >= 1.5
    return True


# ── 표본 기간 필터 ─────────────────────────────────────────────────────────
def _filter_period(samples, period_days, today_date):
    cutoff_str = (today_date - datetime.timedelta(days=period_days)).isoformat()
    return [s for s in samples if s["date"] >= cutoff_str]


# ── 단일 리포트 계산 ─────────────────────────────────────────────────────────
def _calc_report(samples, preset_key, period_days):
    """표본(날짜순 정렬 후)으로 단일 리포트 계산.
    - 누적 순수익률: 동일위험 비중(1R=자본1%) 가정, 시간순 복리 ∏(1+net_i)−1
    - equity_curve : 날짜별 equity 점열
    - MDD          : min_k(equity_k / running_max − 1)
    - 승률(net>0) · avg_r · payoff_ratio · profit_factor
    - avg_hold_hours: 청산봉 수 × 6.5h 근사(일봉 → 분 미해상, "추정" 표기)
    """
    slist = sorted(samples, key=lambda x: x["date"])
    n = len(slist)
    sample_ok = n >= MIN_SAMPLE

    outcomes_cnt = {"target2": 0, "target1": 0, "timecut": 0, "stop": 0, "no_fill": 0}
    trades = []   # 체결된 거래 레코드

    for s in slist:
        outcome, r, hb = _evaluate_with_hold(
            s["entry"], s["stop"], s["t1"], s["t2"], s["etype"], s["fwd"])
        if outcome == "invalid":
            continue
        oc_key = outcome if outcome in outcomes_cnt else "no_fill"
        outcomes_cnt[oc_key] += 1
        if outcome == "no_fill":
            continue

        # ── 동일위험 비중(1R = 자본의 1%) 기준 손익 계산 ──────────────────
        # risk_frac: 진입가 대비 손절 거리 비율
        # 포지션 비중 = RISK_PER_TRADE / risk_frac (손절 폭이 클수록 적게 투자)
        # gross_capital = R × RISK_PER_TRADE  (자본 대비 수익률)
        # cost_capital  = COST_BASE × 포지션비중  (투자금 대비 비용 → 자본 대비 환산)
        risk_frac = (s["entry"] - s["stop"]) / s["entry"] if s["entry"] > 0 else 0.0
        if risk_frac < 1e-6:
            continue   # 비정상 손절거리 — 제외
        pos_weight = RISK_PER_TRADE / risk_frac

        gross = (r * RISK_PER_TRADE) if (r is not None and math.isfinite(r)) else 0.0
        if not math.isfinite(gross):
            gross = 0.0

        trades.append({
            "date":     s["date"],
            "net_base": gross - COST_BASE * pos_weight,
            "net_high": gross - COST_HIGH * pos_weight,
            "r":        r,
            "hb":       hb,
        })

    filled = len(trades)
    r_list        = [t["r"] for t in trades if t["r"] is not None]
    rn            = len(r_list)
    hb_list       = [t["hb"] for t in trades if t["hb"] is not None]

    # ── 복리 equity_curve ──────────────────────────────────────────────
    equity       = 1.0
    equity_curve = []
    for t in trades:
        equity *= (1.0 + t["net_base"])
        equity_curve.append({"date": t["date"], "equity": round(equity, 6)})
    net_return_pct = round((equity - 1.0) * 100, 2) if trades else None

    # high-slip 변형
    eq_high = 1.0
    for t in trades:
        eq_high *= (1.0 + t["net_high"])
    net_return_pct_high = round((eq_high - 1.0) * 100, 2) if trades else None

    # ── MDD ────────────────────────────────────────────────────────────
    mdd  = 0.0
    peak = 1.0
    eq   = 1.0
    for t in trades:
        eq   *= (1.0 + t["net_base"])
        peak  = max(peak, eq)
        mdd   = min(mdd, (eq / peak) - 1.0)
    mdd_pct = round(mdd * 100, 2) if trades else None

    # ── 승률 · avg_r ────────────────────────────────────────────────────
    wins    = sum(1 for t in trades if t["net_base"] > 0)
    winrate = round(wins / filled * 100, 1) if filled else None
    avg_r   = round(sum(r_list) / rn, 2) if rn else None

    # ── payoff_ratio · profit_factor ───────────────────────────────────
    wins_r  = [r for r in r_list if r > 0]
    loss_r  = [r for r in r_list if r < 0]
    payoff_ratio  = None
    profit_factor = None
    if wins_r and loss_r:
        payoff_ratio  = round(
            (sum(wins_r) / len(wins_r)) / abs(sum(loss_r) / len(loss_r)), 2)
        profit_factor = round(sum(wins_r) / abs(sum(loss_r)), 2)

    # ── avg_hold_hours ─────────────────────────────────────────────────
    # 일봉 granularity → 분 미해상: 봉 수 × 6.5h 근사 추정.
    avg_hold_hours = round(sum(hb_list) / len(hb_list) * 6.5, 1) if hb_list else None

    return {
        "preset_key":             preset_key,
        "period_days":            period_days,
        "n":                      n,
        "sample_ok":              sample_ok,
        "winrate":                winrate,
        "avg_r":                  avg_r,
        "net_return_pct":         net_return_pct,
        "net_return_pct_highslip": net_return_pct_high,
        "mdd_pct":                mdd_pct,
        "profit_factor":          profit_factor,
        "payoff_ratio":           payoff_ratio,
        "avg_hold_hours":         avg_hold_hours,
        "outcomes":               outcomes_cnt,
        "equity_curve":           equity_curve,
    }


# ── 메인 ──────────────────────────────────────────────────────────────────
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("[strategy_backtest] 시작")
    print(f"  비용(왕복) base {COST_BASE*100:.3f}% / high {COST_HIGH*100:.3f}%")

    watchlist, scope, mkts, hold_cap, tuning, _w, _oh = load_control()
    if not watchlist:
        print("[strategy_backtest] watchlist 비어있음 — 중단")
        return 1

    today        = datetime.date.today()
    lookback_max = max(PERIODS)    # 최대 기간(90일)만큼의 표본 수집

    # 1) 관심종목 일봉 가상진입 표본
    print(f"\n[1] 관심종목 일봉 가상진입 표본 수집 ({len(watchlist)}종목, 최대{lookback_max}일)")
    bt_samples = _collect_bt_samples(watchlist, lookback_max)
    print(f"    → {len(bt_samples)}건")

    # 2) signals_log 실발행 표본
    print(f"\n[2] signals_log 실발행 표본 수집")
    log_samples = _collect_log_samples()
    print(f"    → {len(log_samples)}건")

    # 3) 합치기 — (code+date) 기준 중복 제거, signals_log 우선
    seen        = set()
    all_samples = []
    for s in log_samples:
        key = (s["code"], s["date"])
        if key not in seen:
            seen.add(key)
            all_samples.append(s)
    for s in bt_samples:
        key = (s["code"], s["date"])
        if key not in seen:
            seen.add(key)
            all_samples.append(s)

    universe_count = len(set(s["code"] for s in all_samples))
    print(f"\n[3] 합산 표본: {len(all_samples)}건 / {universe_count}종목 (signals_log 우선 병합)")

    # 4) 프리셋 × 기간 매트릭스
    print("\n[4] 프리셋 × 기간 매트릭스 계산")
    print(f"    {'프리셋':<20} {'기간':>5} {'n':>5} {'ok':>5} {'승률':>6} {'net%':>7} {'MDD%':>7}")
    reports = []
    for preset in PRESETS:
        for period_days in PERIODS:
            period_samp  = _filter_period(all_samples, period_days, today)
            preset_samp  = [s for s in period_samp if _matches_preset(s, preset)]
            rep          = _calc_report(preset_samp, preset["key"], period_days)
            reports.append(rep)
            print(f"    {preset['key']:<20} {period_days:>4}일 "
                  f"{rep['n']:>5} {str(rep['sample_ok']):>5} "
                  f"{str(rep['winrate']):>6} {str(rep['net_return_pct']):>7} "
                  f"{str(rep['mdd_pct']):>7}")

    # 5) JSON 출력
    result = {
        "status":                    "ok",
        "schema_version":            "1.0",
        "generated_at":              datetime.datetime.now(KST).replace(microsecond=0).isoformat(),
        "universe_count":            universe_count,
        "source":                    "signals_log+daily_backtest",
        "cost_roundtrip_base_pct":   round(COST_BASE * 100, 3),
        "cost_roundtrip_high_pct":   round(COST_HIGH * 100, 3),
        "min_sample":                MIN_SAMPLE,
        "periods":                   PERIODS,
        "presets":                   PRESETS,
        "reports":                   reports,
        "limitations": [
            "동일위험 비중(1R=자본1%) 가정 — 손절 시 자본의 1%를 잃도록 포지션 역산. 실제 비중과 다를 수 있음",
            "일봉(1일봉) 기반 — 봉 내 세밀한 손절/목표 터치 미해상(낙관 편향 가능)",
            "avg_hold_hours는 거래일 봉 수 × 6.5h 근사(분 단위 해상도 아님, 추정치)",
            "foreign_buy 프리셋: 수급 데이터(외국인 순매매) 미확보 — sample_ok=false",
            "signals_log 점수는 발행 당시가 아닌 해당 날짜 일봉 재계산값(근사)",
            "yfinance 일봉: 분할 조정 지연·KR 시간외 포함 가능성 있음",
        ],
        "disclaimer": "과거 가정 시뮬 — 미래 수익 보장 아님.",
    }

    OUT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8")
    print(f"\n[strategy_backtest] 완료 → {OUT_PATH}")
    print(f"  reports: {len(reports)}건 / sample_ok: {sum(1 for r in reports if r['sample_ok'])}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
