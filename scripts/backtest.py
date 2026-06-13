#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
신호 적중률 통계 — 백테스트 + 전향(forward) 추적을 합쳐 stats.json 을 만든다.

데이터 정합성(필수): 모든 통계는 **실측 일봉**에서만 산출한다(가짜 백테스트·날조 금지).
신호가 없었던 구간을 지어내지 않으며, 미확보 종목은 제외한다.

(1) 백테스트: 관심종목(control.watchlist)의 과거 ~8개월 일봉에 **현재 규칙**
    (analyze_technical.score_stock + build_signal)을 그대로 적용해, 점수≥55 인 날을
    가상 진입으로 보고 이후 보유창(48h≈2 거래일) 동안 목표/손절/시간청산 중 무엇에
    먼저 닿았는지 실제 고저로 판정한다. 보수적으로 한 봉 안에서 손절을 목표보다 먼저 본다.
(2) 전향 추적: analyze_technical 이 발행할 때 signals_log.json 에 쌓아 둔 실제 신호 중
    보유창이 지난 건을 같은 방식으로 평가한다(실거래 정확도, 시간이 지나며 누적).

산출 stats.json: 적중 분포·승률·평균 R(손익비 실현)·점수구간/진입유형별 + 보완점(진단).
GitHub Actions(backtest.yml) 일 1회 실행 또는 로컬 실행.
"""
import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from analyze_technical import (
    _calc_indicators, score_stock, load_control, levels, DEFAULT_TUNING,
)

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
STATS_PATH = REPO_ROOT / "stats.json"
LOG_PATH = REPO_ROOT / "signals_log.json"

HOLD_BARS = 2          # 48h ≈ 2 거래일(일봉 기준)
SCORE_CUTOFF = 55      # analyze_technical 신호 임계치와 동일
LOOKBACK_DAYS = 240    # 백테스트 대상 기간(약 8개월)


def _fetch_daily(yahoo, period="400d"):
    """yfinance 일봉 → [(date, open, high, low, close)] (과거→현재). 실패 시 []."""
    try:
        import yfinance as yf
        h = yf.Ticker(yahoo).history(period=period, auto_adjust=False)
        rows = []
        for idx, r in h.iterrows():
            try:
                rows.append((str(idx)[:10], float(r["Open"]), float(r["High"]),
                             float(r["Low"]), float(r["Close"]), float(r["Volume"])))
            except Exception:
                continue
        return rows
    except Exception:
        return []


def _evaluate(entry, stop, t1, t2, etype, fwd):
    """진입가/손절/목표와 이후 봉(fwd: [(o,h,l,c)...])으로 결과·R 판정.

    반환: (outcome, r) — outcome ∈ {no_fill, stop, target1, target2, timecut},
    r = (청산가-진입)/(진입-손절). 손절=-1.0 기준. 미발동(no_fill)은 r=None.
    """
    risk = entry - stop
    if risk <= 0:
        return ("invalid", None)
    filled = (etype == "now")
    last_close = None
    for (o, h, l, c) in fwd:
        last_close = c
        if not filled:
            # 돌파대기: 고가가 진입가 이상이면 그 봉에서 체결로 본다.
            if h >= entry:
                filled = True
            else:
                continue
        # 보수적: 한 봉 안에서 손절을 목표보다 먼저 본다.
        if l <= stop:
            return ("stop", -1.0)
        if h >= t2:
            return ("target2", round((t2 - entry) / risk, 2))
        if h >= t1:
            return ("target1", round((t1 - entry) / risk, 2))
    if not filled:
        return ("no_fill", None)
    # 보유창 종료 — 시간청산(마지막 종가 기준).
    return ("timecut", round((last_close - entry) / risk, 2) if last_close else 0.0)


def _bucket(score):
    if score >= 75:
        return "75+"
    if score >= 65:
        return "65-74"
    return "55-64"


def _blank_agg():
    return {"n": 0, "no_fill": 0, "stop": 0, "target1": 0, "target2": 0,
            "timecut": 0, "wins": 0, "r_sum": 0.0}


def _record(agg, outcome, r):
    agg["n"] += 1
    agg[outcome] = agg.get(outcome, 0) + 1
    if r is not None:
        agg["r_sum"] += r
        if r > 0:
            agg["wins"] += 1


def _finalize(agg):
    filled = agg["n"] - agg["no_fill"]
    return {
        "signals": agg["n"],
        "filled": filled,
        "no_fill": agg["no_fill"],
        "hit_target1": agg["target1"],
        "hit_target2": agg["target2"],
        "hit_stop": agg["stop"],
        "timecut": agg["timecut"],
        "win_rate": round(agg["wins"] / filled * 100, 1) if filled else None,
        "avg_r": round(agg["r_sum"] / filled, 2) if filled else None,
    }


def _diagnostics(agg, by_type):
    """실측 분포에서 보완점을 진단(데이터 기반·정직). 빈 통계면 빈 리스트."""
    out = []
    filled = agg["n"] - agg["no_fill"]
    if filled < 10:
        out.append("표본이 적어요(체결 10건 미만) — 통계가 쌓일수록 정확해집니다.")
        return out
    stop_rate = agg["stop"] / filled * 100
    tc_rate = agg["timecut"] / filled * 100
    t_rate = (agg["target1"] + agg["target2"]) / filled * 100
    nf = by_type.get("breakout", _blank_agg())
    nf_rate = nf["no_fill"] / nf["n"] * 100 if nf["n"] else 0
    if stop_rate >= 45:
        out.append(f"손절 도달이 {stop_rate:.0f}%로 높아요 — 손절폭이 타이트하거나 진입이 이른 신호가 많습니다.")
    if tc_rate >= 45:
        out.append(f"시간청산이 {tc_rate:.0f}%로 많아요 — 목표가가 멀거나 보유 기간이 짧을 수 있어요.")
    if nf_rate >= 50:
        out.append(f"‘돌파대기’ 신호의 미발동률이 {nf_rate:.0f}%예요 — 돌파 기준이 높아 기회를 놓칠 수 있어요.")
    if t_rate >= 50:
        out.append(f"목표 도달이 {t_rate:.0f}%로 양호해요 — 현재 규칙이 잘 맞는 편입니다.")
    if not out:
        out.append("뚜렷한 편향은 없어요 — 균형 잡힌 분포입니다.")
    return out


def _collect_samples(items):
    """과거 일봉에서 점수≥컷오프인 가상 진입 표본을 모은다.
    표본 = (price, ind, fwd, score) — 이후 임의 파라미터로 재평가(스윕)할 수 있게 raw 보관."""
    samples = []
    for it in items:
        code = it.get("code", "")
        if not code:
            continue
        bars = _fetch_daily(code + ".KS") or _fetch_daily(code + ".KQ")
        if len(bars) < 60:
            continue
        n = len(bars)
        start = max(25, n - LOOKBACK_DAYS)
        for i in range(start, n - HOLD_BARS):
            window = bars[:i + 1]
            ind = _calc_indicators(
                [b[4] for b in window], [b[2] for b in window],
                [b[3] for b in window], [b[5] for b in window],
                [b[1] for b in window])
            if ind is None:
                continue
            price = bars[i][4]
            sc, _ = score_stock(price, ind)
            if sc < SCORE_CUTOFF:
                continue
            fwd = [(b[1], b[2], b[3], b[4]) for b in bars[i + 1:i + 1 + HOLD_BARS]]
            samples.append((price, ind, fwd, sc))
    return samples


def _eval_sample(price, ind, fwd, sm, t1m, t2m):
    """표본을 주어진 손절·목표 배수로 평가 → (outcome, r, etype)."""
    lv = levels(price, ind, 48, sm, t1m, t2m)
    outcome, r = _evaluate(lv["entry"], lv["stop"], lv["target1"], lv["target2"],
                           lv["etype"], fwd)
    return outcome, r, lv["etype"]


def _aggregate(samples):
    """기본 파라미터(현 규칙)로 표본을 집계 → (전체, 점수버킷별, 진입유형별)."""
    agg = _blank_agg()
    by_bucket = {"55-64": _blank_agg(), "65-74": _blank_agg(), "75+": _blank_agg()}
    by_type = {"now": _blank_agg(), "breakout": _blank_agg()}
    sm = DEFAULT_TUNING["stop_mult"]
    t1 = DEFAULT_TUNING["target1_mult"]
    t2 = DEFAULT_TUNING["target2_mult"]
    for (price, ind, fwd, sc) in samples:
        outcome, r, et = _eval_sample(price, ind, fwd, sm, t1, t2)
        if outcome == "invalid":
            continue
        _record(agg, outcome, r)
        _record(by_bucket[_bucket(sc)], outcome, r)
        _record(by_type[et], outcome, r)
    return agg, by_bucket, by_type


def _avg_r(subset, sm, t1, t2):
    """subset 표본을 주어진 배수로 평가한 평균 R·체결 수."""
    rs = []
    for (price, ind, fwd, sc) in subset:
        _, r, _ = _eval_sample(price, ind, fwd, sm, t1, t2)
        if r is not None:
            rs.append(r)
    return (sum(rs) / len(rs), len(rs)) if rs else (None, 0)


def _tune(samples):
    """손절·목표 배수 그리드를 train(70%)/val(30%)로 검증해 보정안을 제안한다.
    **과최적화 방지**: val 평균 R 이 현재 대비 +0.10 이상 좋고 train 도 개선될 때만 제안.
    승인은 앱(통계 탭)에서 사용자가 한다(자동 적용 안 함)."""
    cur = {"stop_mult": DEFAULT_TUNING["stop_mult"],
           "target1_mult": DEFAULT_TUNING["target1_mult"],
           "target2_mult": DEFAULT_TUNING["target2_mult"],
           "score_cutoff": SCORE_CUTOFF}
    block = {"current": cur, "suggestion": None,
             "method": "train 70% / val 30% 시간 분할 · val 평균 R 최대 + 현재 대비 "
                       "+0.10R 이상일 때만 제안(과최적화 방지). 적용은 사용자 승인."}
    n = len(samples)
    if n < 40:
        block["note"] = "표본이 적어 보정안을 내지 않아요(40건 이상 필요)."
        return block
    cut = int(n * 0.7)
    train, val = samples[:cut], samples[cut:]
    cur_train, _ = _avg_r(train, cur["stop_mult"], cur["target1_mult"], cur["target2_mult"])
    cur_val, _ = _avg_r(val, cur["stop_mult"], cur["target1_mult"], cur["target2_mult"])
    if cur_train is None or cur_val is None:
        return block
    best = None
    for sm in (1.2, 1.5, 1.8, 2.1):
        for t1 in (1.5, 2.0, 2.5):
            t2 = t1 + 1.0
            tr, _ = _avg_r(train, sm, t1, t2)
            vl, nf = _avg_r(val, sm, t1, t2)
            if tr is None or vl is None:
                continue
            if best is None or vl > best["valid_avg_r"]:
                best = {"stop_mult": sm, "target1_mult": t1, "target2_mult": t2,
                        "score_cutoff": SCORE_CUTOFF,
                        "train_avg_r": round(tr, 2), "valid_avg_r": round(vl, 2)}
    if best and best["valid_avg_r"] >= cur_val + 0.10 and \
            best["train_avg_r"] >= round(cur_train, 2) and \
            (best["stop_mult"] != cur["stop_mult"] or
             best["target1_mult"] != cur["target1_mult"]):
        best["current_valid_avg_r"] = round(cur_val, 2)
        best["basis"] = (
            f"손절 배수 {cur['stop_mult']}→{best['stop_mult']}, 목표1 배수 "
            f"{cur['target1_mult']}→{best['target1_mult']} 로 바꾸면 검증구간 평균 R 이 "
            f"{round(cur_val, 2)}→{best['valid_avg_r']} 로 개선돼요.")
        block["suggestion"] = best
    else:
        block["note"] = "지금 설정이 검증구간에서 가장 무난해요 — 바꿀 만한 보정안이 없어요."
    return block


def _forward_section():
    """signals_log.json 의 실제 발행 신호 중 보유창이 지난 건을 평가."""
    if not LOG_PATH.exists():
        return {"status": "no_log", "agg": _finalize(_blank_agg())}
    try:
        log = json.loads(LOG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"status": "read_error", "agg": _finalize(_blank_agg())}
    entries = log if isinstance(log, list) else log.get("signals", [])
    now = datetime.datetime.now(KST)
    agg = _blank_agg()
    matured = 0
    for e in entries:
        try:
            issued = datetime.datetime.fromisoformat(e["issued_at"])
        except Exception:
            continue
        # 보유창(시간) 경과분만 평가.
        cap = float(e.get("hold_cap_hours", 48))
        if (now - issued).total_seconds() < cap * 3600:
            continue
        code = e.get("code", "")
        bars = _fetch_daily(code + ".KS", period="60d") or _fetch_daily(code + ".KQ", period="60d")
        d0 = str(issued)[:10]
        idx = next((j for j, b in enumerate(bars) if b[0] >= d0), None)
        if idx is None:
            continue
        fwd = [(b[1], b[2], b[3], b[4]) for b in bars[idx + 1:idx + 1 + HOLD_BARS]]
        if not fwd:
            continue
        outcome, r = _evaluate(e["entry"], e["stop"], e["target1"], e["target2"],
                               e.get("entry_type", "now"), fwd)
        if outcome == "invalid":
            continue
        matured += 1
        _record(agg, outcome, r)
    return {"status": "ok", "matured": matured, "logged": len(entries),
            "agg": _finalize(agg)}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    watchlist, scope, mkts, hold_cap, tuning = load_control()
    if not watchlist:
        print("[backtest] watchlist 비어있음 — 중단")
        return 0
    now = datetime.datetime.now(KST).replace(microsecond=0)
    samples = _collect_samples(watchlist)
    agg, by_bucket, by_type = _aggregate(samples)

    stats = {
        "schema_version": "1.0",
        "generated_at": now.isoformat(),
        "backtest": {
            "period_days": LOOKBACK_DAYS,
            "universe": "관심종목",
            "universe_count": len(watchlist),
            "hold_bars": HOLD_BARS,
            "score_cutoff": SCORE_CUTOFF,
            "overall": _finalize(agg),
            "by_score": {k: _finalize(v) for k, v in by_bucket.items()},
            "by_entry_type": {k: _finalize(v) for k, v in by_type.items()},
            "diagnostics": _diagnostics(agg, by_type),
            "note": "과거 일봉에 현 규칙을 적용한 백테스트(미국증시 환경 보정 제외). "
                    "한 봉 내 손절을 목표보다 먼저 보는 보수적 판정. 실측 가격만 사용.",
        },
        # 학습 보정안(승인제) — 손절·목표 배수 그리드를 train/val 로 검증해 제안.
        "tuning": _tune(samples),
        "applied_tuning": tuning,
        "forward": _forward_section(),
        "disclaimer": "통계는 과거·실거래 실측 기반 참고용이며 미래 수익을 보장하지 않습니다.",
    }
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    ov = stats["backtest"]["overall"]
    print(f"[backtest] 완료 @ {now.isoformat()} — 신호 {ov['signals']} · "
          f"승률 {ov['win_rate']} · 평균R {ov['avg_r']} · forward {stats['forward'].get('matured', 0)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
