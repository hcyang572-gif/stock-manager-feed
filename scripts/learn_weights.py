#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
점수 가중치 학습 — 과거 일봉에서 '지표 → 48h 승/패'를 모아 **로지스틱 회귀**로
각 차트 항목(정배열·거래량·돌파 등)의 가중치를 데이터가 정하게 한다(2단계).

데이터 정합성(필수): 실측 과거 일봉만 사용한다(가짜·날조 금지). 학습 결과는 '확률
추정'이며 사실이 아니다 — stats.json 에 **제안(베타)**으로만 싣고, 실제 적용은 앱
통계 탭에서 사용자가 승인(control.json engine.learned_weights)한다.

과최적화 방지: 표본을 시간순 train 70% / val 30% 로 나눠, **검증구간 AUC**(순위
정확도)가 현재 규칙보다 의미있게(+0.02 이상) 좋을 때만 보정안을 제안한다.

산출: stats.json 의 "learned_weights" 블록을 추가/갱신한다(backtest.py 다음에 실행).
의존: numpy(이미 설치) — scikit-learn 불필요(가벼움·투명성).
"""
import bisect
import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from analyze_technical import (
    _calc_indicators, chart_features, score_stock, load_control,
    levels, build_universe, fetch_supply_history_batch,
    combo_bucket, combo_key,
    DEFAULT_WEIGHTS, WEIGHT_LABELS, DEFAULT_TUNING,
)
from backtest import _fetch_daily, _evaluate, HOLD_BARS

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
STATS_PATH = REPO_ROOT / "stats.json"

# 학습 대상 항목(피처) — chart_features 키 중 학습할 것. align_down 은 align_up 과
# 완전 보완(상보)이라 제외(다중공선성 회피); 제안 시 align_down=0 으로 둔다.
FEATURES = [
    "align_up", "above_ma20", "vol_surge", "vol_ok", "vol_dry",
    "strong_close", "weak_close", "rsi_up", "rsi_hot", "rsi_oversold",
    "breakout", "mom_up",
    # 미시구조(Stage A) — 일봉에서 계산, full 이력.
    "gap_up", "gap_down", "vol_accel", "streak_up", "near_high",
    "range_exp", "ma20_up",
    # 수급(Stage B) — frgn 일별 외국인·기관 순매매 이력.
    "for_buy", "for_sell", "org_buy", "org_sell",
]

MIN_TRAIN = 150        # 학습 최소 표본(이보다 적으면 제안 안 함)
AUC_MARGIN = 0.02      # 검증 AUC 가 현재보다 이만큼 좋아야 제안(과최적화 방지)
POINT_MAX = 18         # 가중치 환산 시 가장 강한 항목을 ±이 점수로(현재 최대치와 동급)
# 데이터 정밀도↑: 종목 수(시장별 상위)·종목당 과거 기간을 크게 잡아 표본을 최대화한다.
TRAIN_TOP = 80         # 학습 유니버스: 시장별 거래대금 상위 N(관심종목과 합쳐 표본↑)
LEARN_PERIOD = "800d"  # 종목당 일봉 기간(약 2년+ — 한 번 받을 때 더 많은 표본 확보)
LEARN_LOOKBACK = 520   # 학습에 쓸 최근 일수(약 2년치 일봉)
SUPPLY_PAGES = 15      # 수급 이력 페이지 수(페이지당 ~20거래일 → 약 300거래일)
SUPPLY_DAYS = 5        # 수급 합산 창(최근 N거래일 외국인·기관 순매매 합)


def _supply_at(sup_sorted, sup_dates, date, days=SUPPLY_DAYS):
    """수급 정렬이력(날짜 오름차순)에서 date 이하 최근 days 거래일의 외국인·기관
    순매매 합 → {'for':..,'org':..}. 데이터 없으면 None(→ 수급 피처 0)."""
    if not sup_dates:
        return None
    j = bisect.bisect_right(sup_dates, date)  # date 이하 개수
    if j == 0:
        return None
    lo = max(0, j - days)
    fs = sum(sup_sorted[k][0] for k in range(lo, j))
    os_ = sum(sup_sorted[k][1] for k in range(lo, j))
    return {"for": fs, "org": os_}


def _collect(items, supply_map):
    """과거 일봉(+수급 이력)에서 (date, features, label, cur_score, bucket) 표본을 모은다.
    label = 1 if 48h 보유창에서 승(R>0) else 0. 미체결(no_fill)·무효는 제외.
    백테스트와 달리 **점수 컷오프 없이 전 구간**을 모은다(가중치 학습용)."""
    sm = DEFAULT_TUNING["stop_mult"]
    t1 = DEFAULT_TUNING["target1_mult"]
    t2 = DEFAULT_TUNING["target2_mult"]
    samples = []
    sup_used = 0
    for it in items:
        code = it.get("code", "")
        if not code:
            continue
        kq = it.get("_mk") == "KOSDAQ"
        first = code + (".KQ" if kq else ".KS")
        second = code + (".KS" if kq else ".KQ")
        bars = _fetch_daily(first, period=LEARN_PERIOD) or \
            _fetch_daily(second, period=LEARN_PERIOD)
        if len(bars) < 60:
            continue
        # 수급 이력 → 날짜 오름차순 정렬(샘플별 창 합산용).
        hist = supply_map.get(code) or {}
        sup_dates = sorted(hist.keys())
        sup_sorted = [hist[d] for d in sup_dates]
        n = len(bars)
        start = max(25, n - LEARN_LOOKBACK)
        for i in range(start, n - HOLD_BARS):
            window = bars[:i + 1]
            ind = _calc_indicators(
                [b[4] for b in window], [b[2] for b in window],
                [b[3] for b in window], [b[5] for b in window],
                [b[1] for b in window])
            if ind is None:
                continue
            price = bars[i][4]
            lv = levels(price, ind, 48, sm, t1, t2)
            fwd = [(b[1], b[2], b[3], b[4]) for b in bars[i + 1:i + 1 + HOLD_BARS]]
            outcome, r = _evaluate(lv["entry"], lv["stop"], lv["target1"],
                                   lv["target2"], lv["etype"], fwd)
            if outcome in ("invalid", "no_fill") or r is None:
                continue
            supply = _supply_at(sup_sorted, sup_dates, bars[i][0])
            if supply:
                sup_used += 1
            feats = chart_features(price, ind, supply)
            cur, _, _ = score_stock(price, ind)
            # 조합 버킷 — analyze 와 동일 정의(공용 combo_bucket). supply 없으면 net=0(중립).
            bucket = combo_bucket(cur, ind["vol_surge"],
                                  (supply or {}).get("for", 0),
                                  (supply or {}).get("org", 0))
            samples.append((bars[i][0], [feats[k] for k in FEATURES],
                            1 if r > 0 else 0, cur, bucket))
    samples.sort(key=lambda s: s[0])  # 시간순(train/val 분할용)
    print(f"[learn] 표본 {len(samples)}건 (수급 매칭 {sup_used}건)")
    return samples


def _fit_logreg(X, y, l2=1.0, lr=0.3, iters=4000):
    """L2 정규화 로지스틱 회귀(numpy 경사하강). 반환: (가중치[d], 절편)."""
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(X @ w + b)))
        g = p - y
        w -= lr * (X.T @ g / n + l2 * w / n)
        b -= lr * g.mean()
    return w, b


def _auc(scores, y):
    """AUC(Mann-Whitney U, 동점 평균순위). 한쪽 클래스 없으면 None."""
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y)
    npos, nneg = int(y.sum()), int((y == 0).sum())
    if npos == 0 or nneg == 0:
        return None
    order = np.argsort(scores, kind="mergesort")
    s_sorted = scores[order]
    ranks = np.empty(len(scores))
    i = 0
    while i < len(scores):
        j = i
        while j + 1 < len(scores) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0  # 1-based 평균순위
        i = j + 1
    return float((ranks[y == 1].sum() - npos * (npos + 1) / 2.0) / (npos * nneg))


def _to_points(coef):
    """로지스틱 계수(로그오즈) → 현재와 같은 점수 스케일로 환산.
    가장 강한 항목을 ±POINT_MAX 로 맞춰 막대 비교가 직관적이게 한다."""
    m = float(np.max(np.abs(coef))) or 1.0
    scale = POINT_MAX / m
    return {FEATURES[i]: int(round(coef[i] * scale)) for i in range(len(FEATURES))}


# ── 조합별 승률 분석(점수×거래량×외국인×기관) ───────────────────────────────
# 단일 수식(가중치) 대신 "여러 조건이 동시에 맞을 때 48h 승률이 어떻게 달라지나"를
# 본다. _collect 표본을 (점수구간 × 거래량상태 × 외국인 × 기관) 조합으로 묶어 승률·
# 표본수를 집계하고, 표본 최소치 미만 조합은 우연/과최적화 방지로 제외한다.
COMBO_MIN_SAMPLE = 120   # 조합 최소 표본(미만은 신뢰 낮아 제외)
COMBO_TOP = 10           # 상위 조합 노출 수


def combo_winrates(samples, now):
    """(점수구간 × 거래량 × 외국인 × 기관) 조합별 48h 승률을 집계한 블록(dict).
    버킷은 _collect 가 표본마다 저장한 공용 combo_bucket 결과(인덱스 4)를 그대로 쓴다 —
    analyze 의 신호 보정과 정의가 항상 일치한다. 표본 최소치(COMBO_MIN_SAMPLE) 미만
    조합은 제외. 전체 평균(base) 대비 우위(lift)도. best/worst 외에 **전체 조합 lookup
    테이블**(table: combo_key → {winrate,n,lift})도 싣는다(analyze 가 신호별 조회)."""
    agg = {}  # bucket(튜플) -> [wins, n]
    wins_total = 0
    for s in samples:
        bucket = s[4]
        a = agg.setdefault(bucket, [0, 0])
        a[0] += s[2]
        a[1] += 1
        wins_total += s[2]
    n_total = len(samples)
    base = (wins_total / n_total) if n_total else 0.0
    combos = []
    table = {}
    for (sb, vs, fd, idr), (wins, n) in agg.items():
        if n < COMBO_MIN_SAMPLE:
            continue
        wr = wins / n
        lift = round(wr - base, 3)
        combos.append({"score": sb, "volume": vs, "foreign": fd, "inst": idr,
                       "winrate": round(wr, 3), "n": n, "lift": lift})
        table[combo_key((sb, vs, fd, idr))] = {
            "winrate": round(wr, 3), "n": n, "lift": lift}
    best = sorted(combos, key=lambda c: c["winrate"], reverse=True)
    worst = sorted(combos, key=lambda c: c["winrate"])
    return {
        "generated_at": now.isoformat(),
        "base_winrate": round(base, 3),
        "n_total": n_total,
        "n_combos": len(combos),
        "min_sample": COMBO_MIN_SAMPLE,
        "method": ("과거 일봉의 (점수구간 × 거래량 × 외국인순매매 × 기관순매매) "
                   f"조합별 48h 승률. 표본 {COMBO_MIN_SAMPLE}건 미만 조합은 우연 방지로 제외."),
        "disclaimer": ("과거 데이터 기반 참고치이며 미래 수익을 보장하지 않습니다. "
                       "조합을 좁힐수록 표본이 줄어 신뢰가 낮아집니다. "
                       "호가 매수/매도 비율은 과거 기록이 없어 제외(향후 적재 예정)."),
        "best": best[:COMBO_TOP],
        "worst": worst[:3],
        # 전체 조합 lookup 테이블(표본 충분한 모든 조합) — analyze 신호 보정용.
        "table": table,
    }


def learn(samples):
    """학습 → 검증 → 제안 블록(dict) 반환(samples = _collect 결과)."""
    cur = {k: DEFAULT_WEIGHTS[k] for k in (["base"] + FEATURES + ["align_down"])}
    block = {
        "current": cur,
        "labels": {k: WEIGHT_LABELS.get(k, k) for k in FEATURES + ["align_down"]},
        "features": FEATURES,
        "suggestion": None,
        "method": "과거 일봉의 '지표→48h 승패'를 로지스틱 회귀로 학습(numpy). "
                  "train 70%/val 30% 시간분할 · 검증 AUC 가 현재 규칙보다 +0.02 이상 "
                  "좋을 때만 제안(과최적화 방지). 적용은 사용자 승인.",
        "disclaimer": "학습 가중치는 과거 데이터 기반 확률 추정(베타)이며 미래 수익을 "
                      "보장하지 않습니다.",
    }
    n = len(samples)
    if n < MIN_TRAIN + 30:
        block["note"] = f"표본이 적어 학습을 보류해요(현재 {n}건, {MIN_TRAIN + 30}건 이상 필요)."
        return block, n

    cut = int(n * 0.7)
    X = np.array([s[1] for s in samples], dtype=float)
    y = np.array([s[2] for s in samples], dtype=float)
    cur_score = np.array([s[3] for s in samples], dtype=float)
    Xtr, ytr = X[:cut], y[:cut]
    Xva, yva = X[cut:], y[cut:]
    cur_va = cur_score[cut:]

    w, b = _fit_logreg(Xtr, ytr)
    learned_va = Xva @ w + b
    learned_auc = _auc(learned_va, yva)
    current_auc = _auc(cur_va, yva)
    base_rate = round(float(y.mean()) * 100, 1)

    block["metrics"] = {
        "train_n": int(cut), "val_n": int(n - cut),
        "win_base_rate": base_rate,
        "current_val_auc": round(current_auc, 3) if current_auc is not None else None,
        "learned_val_auc": round(learned_auc, 3) if learned_auc is not None else None,
    }

    if learned_auc is None or current_auc is None:
        block["note"] = "검증 표본이 한쪽으로 치우쳐 비교 불가 — 다음 회차에 다시 시도해요."
        return block, n

    # 학습 가중치는 **항상** 보여준다(데이터가 뭐라는지 확인용). 다만 검증 성능이
    # 현재보다 의미있게(+AUC_MARGIN) 좋을 때만 '추천'으로 표시한다.
    pts = _to_points(w)
    pts["align_down"] = 0  # 역배열 별도 감점은 두지 않음(정배열 가점만 학습)
    recommended = learned_auc >= current_auc + AUC_MARGIN
    sug = {"base": 50, **pts,
           "val_auc": round(learned_auc, 3),
           "current_val_auc": round(current_auc, 3)}
    if recommended:
        sug["basis"] = (
            f"과거 데이터로 학습하니 검증구간 순위정확도(AUC)가 "
            f"{round(current_auc, 3)}→{round(learned_auc, 3)} 로 좋아졌어요. "
            f"데이터가 본 가장 중요한 항목 위주로 가중치를 다시 매겼습니다.")
    else:
        sug["basis"] = (
            f"학습은 했지만 검증 성능 개선이 작아요(AUC "
            f"{round(current_auc, 3)}→{round(learned_auc, 3)}). 참고용으로만 보고, "
            f"적용은 신중히 — 표본이 더 쌓이면 정확해져요.")
    block["suggestion"] = sug
    block["recommended"] = recommended
    return block, n


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    watchlist, _scope, mkts, _cap, _tuning, applied_weights = load_control()
    now = datetime.datetime.now(KST).replace(microsecond=0)

    # 학습 표본 최대화 — 관심종목 + 시장 거래대금 상위 유니버스(중복 제거).
    items = list(watchlist)
    seen = {str(w.get("code", "")).strip() for w in watchlist}
    uni = build_universe(mkts, top_per_market=TRAIN_TOP)
    for u in uni:
        c = str(u.get("code", "")).strip()
        if c and c not in seen:
            seen.add(c)
            items.append(u)
    print(f"[learn] 학습 유니버스: 관심종목 {len(watchlist)} + 유니버스 "
          f"{len(uni)} → 중복제거 {len(items)}종목 (markets={mkts}, "
          f"기간 {LEARN_PERIOD}/최근 {LEARN_LOOKBACK}일)")
    if not items:
        print("[learn] 대상 종목 없음 — 중단")
        return 0

    # 수급 이력(외국인·기관 일별 순매매) 병렬 prefetch — 학습 피처(Stage B)용.
    codes = [str(it.get("code", "")).strip() for it in items]
    supply_map = fetch_supply_history_batch(codes, SUPPLY_PAGES)

    samples = _collect(items, supply_map)
    block, n = learn(samples)
    block["generated_at"] = now.isoformat()
    block["sample_count"] = n
    block["applied"] = applied_weights is not None

    stats = {}
    if STATS_PATH.exists():
        try:
            stats = json.loads(STATS_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            stats = {}
    stats["learned_weights"] = block
    stats["winrate_combos"] = combo_winrates(samples, now)
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    m = block.get("metrics", {})
    print(f"[learn] 완료 @ {now.isoformat()} — 표본 {n} · "
          f"현재AUC {m.get('current_val_auc')} → 학습AUC {m.get('learned_val_auc')} · "
          f"제안 {'있음' if block['suggestion'] else '없음'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
