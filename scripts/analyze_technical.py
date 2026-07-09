#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
온디맨드 기술 분석 — 뉴스/촉매 없이 차트·거래량·변동성 지표만으로 관심종목을 재분석해
feed.json 의 signals/observations 를 갱신한다(무료, LLM 미사용).

- control.json(watchlist·analysis_scope·market_targets·hold_cap_hours)을 읽어 대상 결정.
- 일봉(yfinance) + 현재가/등락률(KIS 통합 UN) 실측으로 지표 산출(날조 없음).
- 점수화 → 상위 종목을 signals(진입/손절/목표/RR/비중/보유캡), 나머지는 observations.
- catalyst_verified=false(뉴스 미확인), evidence=기술 근거. us_context/kr_context/positions 는
  기존 feed 값 보존. 시세 미확보 종목은 제외(관망).

GitHub Actions(analyze-now.yml)의 workflow_dispatch 로 호출되거나 로컬 `--force` 로 실행.
"""
import datetime
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent


# ── JSON 직렬화 안전 정제 ────────────────────────────────────────────────────
def _json_safe(obj):
    """feed dict 를 재귀로 순회해 JSON 직렬화 불가 float(nan/inf/-inf)를 None 으로
    교체한다. allow_nan=False 직렬화 직전에 반드시 통과시킨다.
    - float: math.isfinite 아니면 → None (날조 없음 — 값 없음=null)
    - dict/list: 재귀
    - 나머지: 그대로"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj
FEED_PATH = REPO_ROOT / "feed.json"
CONTROL_PATH = REPO_ROOT / "control.json"
STATS_PATH = REPO_ROOT / "stats.json"
KIS_BASE = "https://openapi.koreainvestment.com:9443"
KIS_TOKEN_PATH = REPO_ROOT / "config" / ".kis_token.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ── 조합 버킷(공용) — 점수×거래량×외국인×기관 조합 키 ─────────────────────────
# learn_weights.py 의 조합별 승률(winrate_combos)과 analyze 의 신호 보정이 **정확히
# 같은 버킷 정의**를 쓰도록 여기 한 곳에 모은다(중복·불일치 방지). 기존 learn_weights 의
# _score_bucket/_vol_state/_supply_dir 정의와 1:1 동일.
def combo_bucket(score, vol_surge_mult, for_net, org_net):
    """(점수구간, 거래량상태, 외국인방향, 기관방향) 튜플(문자열) 반환.
    - score_bucket: ≥75 '75+' / ≥65 '65-74' / ≥55 '55-64' / else '<55'
    - vol_state: ≥1.5 '급증(≥1.5x)' / 1~1.5 '양호(1~1.5x)' / 0<vs<0.6 '위축(<0.6x)' / else '보통(0.6~1x)'
    - foreign_dir/inst_dir: net>0 '순매수' / net<0 '순매도' / else '중립'
    """
    s = score
    if s >= 75:
        sb = "75+"
    elif s >= 65:
        sb = "65-74"
    elif s >= 55:
        sb = "55-64"
    else:
        sb = "<55"
    vs = vol_surge_mult or 0
    if vs >= 1.5:
        vstate = "급증(≥1.5x)"
    elif 1.0 <= vs < 1.5:
        vstate = "양호(1~1.5x)"
    elif 0 < vs < 0.6:
        vstate = "위축(<0.6x)"
    else:
        vstate = "보통(0.6~1x)"

    def _dir(net):
        net = net or 0
        if net > 0:
            return "순매수"
        if net < 0:
            return "순매도"
        return "중립"

    return (sb, vstate, _dir(for_net), _dir(org_net))


def combo_key(parts):
    """버킷 튜플 → 'score|volume|foreign|inst' 문자열(테이블 lookup 키)."""
    return "|".join(parts)


def load_combo_table():
    """stats.json 의 winrate_combos.table(조합별 과거 승률 lookup)을 1회 로드한다.
    learn_weights.py 가 만든 실측 테이블만 쓴다(없으면 빈 dict → graceful 무보정)."""
    if not STATS_PATH.exists():
        return {}
    try:
        stats = json.loads(STATS_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    wc = stats.get("winrate_combos")
    if not isinstance(wc, dict):
        return {}
    table = wc.get("table")
    return table if isinstance(table, dict) else {}


# 조합 보정 상한(과최적화 방지) — lift × 30 을 ±이 점수로 클램프.
COMBO_ADJ_CAP = 4


def apply_combo(score, ind, combo_table):
    """조합별 과거 승률(combo_table)을 점수에 소프트 반영한다.
    - (점수, 거래량배수, 외국인순매매, 기관순매매) → combo_bucket → combo_key 로 조회.
    - 표본 충분(테이블에 있음)하면: combo_adj = clamp(round(lift*30), ±4) 를 점수에 가산하고
      breakdown 에 '조합 보정' 항목을 남긴다(투명성). ind 에 combo_winrate/_sample/_lift/_adj 부착.
    - 테이블 없거나 표본 부족(미존재)이면: 무보정(adj=0)·미부착(graceful).
    반환: 보정된 점수(0~100 클램프). breakdown(ind['_breakdown'])도 갱신."""
    if not combo_table:
        return score
    sf = ind.get("_sf") or {}
    for_net = sf.get("for_sum")
    org_net = sf.get("org_sum")
    # ★결측≠중립(P1)★ 수급 데이터가 없으면(None) 조합 보정·표시를 하지 않는다.
    # 예전엔 결측을 net=0('중립' 버킷)으로 조회해 '데이터 없음'을 감점(승률 낮은
    # 중립버킷)했다 — 수급 못 구한 종목을 벌하던 train/serve 분포 불일치.
    if for_net is None or org_net is None:
        return score
    key = combo_key(combo_bucket(score, ind.get("vol_surge", 0), for_net, org_net))
    rec = combo_table.get(key)
    if not isinstance(rec, dict):
        return score  # 표본 부족 — 미부착·무보정.
    lift = rec.get("lift", 0) or 0
    adj = max(-COMBO_ADJ_CAP, min(COMBO_ADJ_CAP, round(lift * 30)))
    ind["combo_winrate"] = round(float(rec.get("winrate", 0)), 3)
    ind["combo_sample"] = int(rec.get("n", 0))
    ind["combo_lift"] = round(float(lift), 3)
    ind["combo_adj"] = int(adj)
    if adj:
        bd = ind.setdefault("_breakdown", [])
        wr_pct = round(float(rec.get("winrate", 0)) * 100, 1)
        bd.append(f"조합 보정(과거 승률 {wr_pct}%·표본 {int(rec.get('n', 0))}) {adj:+g}")
    # ★클램프★ 조합 보정(±4) 후에도 상한 확보하되, apply_supply_finance 의 매수
    # 전환/가속 '압도적 보너스'로 이미 100 을 넘긴 점수는 그 헤드룸을 보존한다
    # (상한 = max(100, 입력점수)) — 조합 보정이 압도 보너스를 100 으로 깎지 않게.
    return max(0, min(max(100, round(score)), round(score + adj)))


# ── KIS (현재가·등락률) ──────────────────────────────────────────────────────
def _kis_cfg():
    ak = os.environ.get("KIS_APP_KEY")
    sk = os.environ.get("KIS_APP_SECRET")
    cfgp = REPO_ROOT / "config" / "kis_config.json"
    if (not ak or not sk) and cfgp.exists():
        c = json.loads(cfgp.read_text(encoding="utf-8"))
        ak = ak or c.get("app_key")
        sk = sk or c.get("app_secret")
    if not ak or not sk:
        return None
    return {"app_key": ak, "app_secret": sk}


def _kis_token(cfg):
    now = datetime.datetime.now(KST).timestamp()
    if KIS_TOKEN_PATH.exists():
        try:
            c = json.loads(KIS_TOKEN_PATH.read_text(encoding="utf-8"))
            if c.get("expires_at", 0) > now + 300:
                return c["access_token"]
        except Exception:
            pass
    # ★발급은 전용 워크플로(KIS_TOKEN_ISSUE=1)에서만★ 그 외엔 캐시 없으면 발급 안 함
    # (네이버 폴백). 서버 cron마다 토큰 재발급 = KIS 발급 SMS 폭탄 방지.
    if os.environ.get("KIS_TOKEN_ISSUE") != "1":
        return None
    body = json.dumps({"grant_type": "client_credentials",
                       "appkey": cfg["app_key"], "appsecret": cfg["app_secret"]}).encode()
    req = urllib.request.Request(KIS_BASE + "/oauth2/tokenP", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.load(r)
    tok = resp.get("access_token")
    if tok:
        KIS_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        KIS_TOKEN_PATH.write_text(json.dumps(
            {"access_token": tok, "expires_at": now + int(resp.get("expires_in", 86400))}),
            encoding="utf-8")
    return tok


def kis_quote(code, cfg, token, mrkt="UN"):
    """(현재가, 전일대비율) 또는 (None, None). 실전/모의 tr_id 분기 + 1회 재시도
    (P2/P4 — rate limit·일시 오류로 종목이 조용히 누락되던 것 방지)."""
    import time
    # 실전(real)=FHKST·모의(paper)=VHKST. account_type 미지정이면 실전 기본(안전).
    tr_id = "VHKST01010100" if cfg.get("account_type") == "paper" else "FHKST01010100"
    params = urllib.parse.urlencode({"FID_COND_MRKT_DIV_CODE": mrkt, "FID_INPUT_ISCD": code})
    req = urllib.request.Request(
        f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price?{params}",
        headers={"Authorization": f"Bearer {token}", "appkey": cfg["app_key"],
                 "appsecret": cfg["app_secret"], "tr_id": tr_id, "custtype": "P"})
    for _ in range(2):
        time.sleep(0.25)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                o = json.load(r).get("output", {})
            p = str(o.get("stck_prpr", "")).replace(",", "")
            c = str(o.get("prdy_ctrt", "")).replace(",", "")
            if p:
                return (float(p), float(c) if c not in ("", "None") else None)
        except Exception:
            pass
    return (None, None)


# ── KR 호가단위(틱) 반올림 ───────────────────────────────────────────────────
def round_tick(p):
    if p is None:
        return None
    p = float(p)
    if p < 2000:
        t = 1
    elif p < 5000:
        t = 5
    elif p < 20000:
        t = 10
    elif p < 50000:
        t = 50
    elif p < 200000:
        t = 100
    elif p < 500000:
        t = 500
    else:
        t = 1000
    return int(round(p / t) * t)


# ── 일봉 지표(yfinance) ─────────────────────────────────────────────────────
def _calc_indicators(closes, highs, lows, vols, opens):
    """OHLCV 시계열(과거→현재)로 기술 지표 dict. 데이터 부족 시 None.
    yfinance 가 반환하는 nan/inf float 를 0 으로 방어(nan 이 아래 산술에 전파되면
    모든 지표가 nan 이 되고 직렬화 단계에서 ValueError 로 분석 전체가 터짐)."""
    # nan/inf 입력 방어 — yfinance 가 결측 행에 nan 을 넣을 수 있다.
    def _safe(v, default=0.0):
        return v if (isinstance(v, float) and math.isfinite(v)) else default
    closes = [_safe(v) for v in closes]
    highs  = [_safe(v) for v in highs]
    lows   = [_safe(v) for v in lows]
    vols   = [_safe(v) for v in vols]
    opens  = [_safe(v) for v in opens]
    n = len(closes)
    if n < 25:
        return None
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    # ATR14
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    atr = sum(trs[-14:]) / 14
    atr_pct = atr / closes[-1] * 100 if closes[-1] else 0
    vol_avg20 = sum(vols[-20:]) / 20 if sum(vols[-20:]) else 0
    vol_surge = (vols[-1] / vol_avg20) if vol_avg20 else 0
    # RSI14 (Wilder 근사)
    gains = []
    losses = []
    for i in range(n - 14, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains) / 14
    al = sum(losses) / 14
    rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    day_hi, day_lo, day_op = highs[-1], lows[-1], opens[-1]
    close_pos = ((closes[-1] - day_lo) / (day_hi - day_lo) * 100) if day_hi > day_lo else 50
    # 변동성 돌파(당일 시가 + 0.5*(전일 고-저))
    breakout = day_op + 0.5 * (highs[-2] - lows[-2])
    mom5 = (closes[-1] / closes[-6] - 1) * 100 if n >= 6 else 0
    recent_low5 = min(lows[-5:])
    prev = closes[-2] if n >= 2 else closes[-1]
    change = round((closes[-1] / prev - 1) * 100, 2) if prev else None
    # ── 미시구조 지표(요소 발굴용) — 모두 일봉에서 계산, 추가 데이터 불필요 ──
    # 시가 갭: 오늘 시가 vs 어제 종가(%). 갭 방향은 단기 강력 신호.
    gap_pct = (day_op / prev - 1) * 100 if prev else 0
    # 거래대금/거래량 가속: 최근 5일 평균 vs 20일 평균(>1 이면 거래 활발해지는 중).
    vol_avg5 = sum(vols[-5:]) / 5 if sum(vols[-5:]) else 0
    vol_accel = (vol_avg5 / vol_avg20) if vol_avg20 else 0
    # 연속 양봉(오늘부터 거슬러 종가가 오른 날의 연속 개수).
    up_streak = 0
    for i in range(n - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            up_streak += 1
        else:
            break
    # 20일 전고점 근접도(현재가 / 최근 20일 최고가). 1 에 가까울수록 전고 돌파 임박.
    high20 = max(highs[-20:])
    near_high20 = (closes[-1] / high20) if high20 else 0
    # 장대봉(오늘 범위 / ATR). 1.5↑ 면 변동성 확장(에너지 분출).
    range_exp = ((day_hi - day_lo) / atr) if atr else 0
    # MA20 기울기: 지금 MA20 vs 5일 전 MA20(>0 이면 추세 상승).
    ma20_prev = sum(closes[-25:-5]) / 20
    ma20_slope = ma20 - ma20_prev
    # 고점추격 방어 — MA20 이격률(현재가가 20일선 대비 몇 % 위인가). 이미 있는 값만 사용.
    ma20_disparity = (closes[-1] / ma20 - 1) * 100 if ma20 else 0
    return {
        "ma5": ma5, "ma20": ma20, "atr": atr, "atr_pct": round(atr_pct, 2),
        "vol_surge": round(vol_surge, 2), "rsi": round(rsi, 1),
        "close_pos": round(close_pos, 1), "breakout": breakout,
        "mom5": round(mom5, 2), "day_high": day_hi, "recent_low5": recent_low5,
        "yf_close": closes[-1], "change": change,
        "gap_pct": round(gap_pct, 2), "vol_accel": round(vol_accel, 2),
        "up_streak": up_streak, "near_high20": round(near_high20, 4),
        "range_exp": round(range_exp, 2), "ma20_slope": ma20_slope,
        "ma20_disparity": round(ma20_disparity, 2),
    }


def daily_indicators(yahoo):
    """yfinance 일봉(개별 호출)으로 지표 dict + 5분봉 시계열(앱 차트용). 실패 시 None."""
    try:
        import yfinance as yf
        # 100d ≈ 71거래일: daily_7d(7봉)·daily_30d(22봉)·daily_90d(65봉) 모두 커버.
        h = yf.Ticker(yahoo).history(period="100d", auto_adjust=False)
        n_bars = len(h)
        if n_bars < 25:
            return None
        # ★데이터 충분성 경고(P1-신호품질)★ 30 미만이면 ATR14·MA20 기반 지표가 불안정.
        # 표본 수가 극히 적은 종목(신상장·거래정지 복귀 등)의 과대점수를 방지하기 위해
        # 표본 수를 ind 에 기록한다(score_watchlist/score_universe 가 data_quality 표기에 사용).
        if n_bars < 30:
            print(f"[analyze] ⚠️ 일봉 표본 부족 {yahoo}: {n_bars}봉 (권장 30+)")
        ind = _calc_indicators(
            [float(x) for x in h["Close"]], [float(x) for x in h["High"]],
            [float(x) for x in h["Low"]], [float(x) for x in h["Volume"]],
            [float(x) for x in h["Open"]])
        if ind is None:
            return None
        # 표본 수 기록 — 신호·관찰 발행 시 data_quality 판단에 사용.
        ind["_n_bars"] = n_bars
        # ── 5분봉 시계열(최근 24거래시간, 앱 '최근 24시간 차트'용) ─────────────
        # KR: tail 160봉(정규장 78봉/일 × 2일). US: tail 300봉(~3.7거래일).
        # ★KR NXT 연장(08:00~09:00·15:30~20:00)은 yfinance prepost=False 미제공 →
        #   정규장(09:00~15:30) 기준. ★정합성★ 빈 DF면 빈 리스트([])(null 금지).
        is_kr = yahoo.endswith(".KS") or yahoo.endswith(".KQ")
        i5_tail = 160 if is_kr else 300
        try:
            df5 = yf.Ticker(yahoo).history(period="5d", interval="5m",
                                           prepost=False, auto_adjust=False)
            rows5 = []
            if df5 is not None and not df5.empty:
                df5 = df5.tail(i5_tail)

                def _sf(v):
                    try:
                        f = float(v)
                        return None if f != f else round(f, 2)
                    except Exception:
                        return None

                for idx, row in df5.iterrows():
                    ts = idx
                    if getattr(ts, "tzinfo", None) is None:
                        try:
                            ts = ts.tz_localize("UTC")
                        except Exception:
                            pass
                    rows5.append([
                        ts.isoformat(),
                        _sf(row.get("Open")), _sf(row.get("High")),
                        _sf(row.get("Low")), _sf(row.get("Close")),
                        int(row.get("Volume", 0) or 0),
                    ])
            ind["intraday_5m"] = rows5
        except Exception as ex:
            print(f"[analyze] 5분봉 수집 실패({yahoo}): {ex}")
            ind["intraday_5m"] = []
        # 최근 7/30/90 거래일 일봉 — h(100d) 재사용, 추가 API 호출 없음.
        # 형식: [iso_ts, open, high, low, close, volume] (daily_7d 와 동일 6-튜플).
        # daily_30d = 약 22거래일(1달), daily_90d = 약 65거래일(3달).
        try:
            def _d(v):
                try:
                    f = float(v)
                    return None if f != f else round(f, 2)
                except Exception:
                    return None

            def _daily_rows(df_slice):
                rows = []
                for idx, row in df_slice.iterrows():
                    ts = idx
                    if getattr(ts, "tzinfo", None) is None:
                        try:
                            ts = ts.tz_localize("UTC")
                        except Exception:
                            pass
                    rows.append([ts.isoformat(), _d(row.get("Open")),
                                 _d(row.get("High")), _d(row.get("Low")),
                                 _d(row.get("Close")), int(row.get("Volume", 0) or 0)])
                return rows

            ind["daily_7d"] = _daily_rows(h.tail(7))
            ind["daily_30d"] = _daily_rows(h.tail(30))
            ind["daily_90d"] = _daily_rows(h.tail(90))
        except Exception:
            ind["daily_7d"] = []
            ind["daily_30d"] = []
            ind["daily_90d"] = []
        return ind
    except Exception:
        return None


def daily_indicators_batch(symbols):
    """야후 심볼 리스트 → {symbol: ind}. 한 번의 yf.download 로 일괄 수집(전체종목 스캔용).
    개별 Ticker 호출(수백 회)보다 빠르고 레이트리밋에 강하다. 실패는 흡수(빈 dict)."""
    out = {}
    syms = [s for s in symbols if s]
    if not syms:
        return out
    try:
        import yfinance as yf
        # 100d ≈ 71거래일: daily_7d·daily_30d·daily_90d 모두 커버.
        data = yf.download(syms, period="100d", auto_adjust=False,
                           group_by="ticker", threads=True, progress=False)
    except Exception as ex:
        print(f"[analyze] 배치 다운로드 실패: {ex}")
        return out
    single = len(syms) == 1
    for sym in syms:
        try:
            sub = (data if single else data[sym]).dropna(subset=["Close"])
            if len(sub) < 25:
                continue
            ind = _calc_indicators(
                [float(x) for x in sub["Close"]], [float(x) for x in sub["High"]],
                [float(x) for x in sub["Low"]], [float(x) for x in sub["Volume"]],
                [float(x) for x in sub["Open"]])
            if ind:
                # ★데이터 충분성 가드(P1-신호품질)★ 배치에도 동일하게 표본 수 기록.
                n_sub = len(sub)
                ind["_n_bars"] = n_sub
                if n_sub < 30:
                    print(f"[analyze] ⚠️ 배치 일봉 표본 부족 {sym}: {n_sub}봉 (권장 30+)")
                # 최근 7/30/90 거래일 일봉 — 배치 download 의 sub 재사용, 추가 API 호출 없음.
                # 형식: [iso_ts, open, high, low, close, volume] (daily_7d 와 동일 6-튜플).
                try:
                    def _d7(v):
                        try:
                            f = float(v)
                            return None if f != f else round(f, 2)
                        except Exception:
                            return None

                    def _batch_daily_rows(df_slice):
                        rows = []
                        for idx, row in df_slice.iterrows():
                            ts = idx
                            if getattr(ts, "tzinfo", None) is None:
                                try:
                                    ts = ts.tz_localize("UTC")
                                except Exception:
                                    pass
                            rows.append([ts.isoformat(), _d7(row.get("Open")),
                                         _d7(row.get("High")), _d7(row.get("Low")),
                                         _d7(row.get("Close")), int(row.get("Volume", 0) or 0)])
                        return rows

                    ind["daily_7d"] = _batch_daily_rows(sub.tail(7))
                    ind["daily_30d"] = _batch_daily_rows(sub.tail(30))
                    ind["daily_90d"] = _batch_daily_rows(sub.tail(90))
                except Exception:
                    ind["daily_7d"] = []
                    ind["daily_30d"] = []
                    ind["daily_90d"] = []
                out[sym] = ind
        except Exception:
            continue
    return out


# ── 전체종목 유니버스(네이버 거래대금 상위) ─────────────────────────────────
# pykrx 는 최근 KRX 가 로그인/OTP 를 요구하고 GitHub Actions 클라우드 IP 를 막아
# 빈 결과(→ 관심종목 폴백)를 내므로, 가격 폴링에서 이미 잘 쓰는 네이버 모바일
# JSON API 로 대체한다(로그인 불필요·클라우드 동작). 시총 랭킹 응답에 당일
# 거래대금(accumulatedTradingValueRaw)이 포함돼, 이를 기준으로 재정렬해 '거래
# 활발한 상위' 유니버스를 만든다.
_UNIVERSE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120 Safari/537.36")
NAVER_RANK = ("https://m.stock.naver.com/api/stocks/marketValue/{mk}"
              "?page={page}&pageSize=100")


def _naver_rank_rows(mk, pages=5):
    """네이버 모바일 시총 랭킹 API 로 {mk} 종목(여러 페이지)을 받는다. 각 항목에
    당일 거래대금이 있어 호출 측이 거래대금 순으로 재정렬한다. 실패 시 빈 리스트."""
    rows = []
    for pg in range(1, pages + 1):
        url = NAVER_RANK.format(mk=mk, page=pg)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _UNIVERSE_UA,
                              "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as ex:
            print(f"[analyze] 네이버 랭킹 {mk} p{pg} 실패: {ex}")
            break
        stocks = data.get("stocks") if isinstance(data, dict) else None
        if not stocks:
            break
        rows.extend(stocks)
    return rows


def build_universe(market_targets, top_per_market=100):
    """네이버 거래대금 상위로 시장별 유니버스 [{code,name,market,_mk}] 반환.
    네이버 모바일 시총 랭킹 API(여러 페이지)에서 **보통주만** 추려 당일 거래대금
    순으로 top_per_market 선정한다. 종목명·코드를 네이버 실측으로 받아 정합성을
    지킨다(하드코딩·날조 없음). 네트워크 실패 시 빈 리스트 → 호출 측이 관심종목만
    으로 폴백."""
    valid = {"KOSPI", "KOSDAQ"}
    out = []
    for mk in [m.upper() for m in market_targets if m.upper() in valid]:
        rows = _naver_rank_rows(mk, pages=5)
        cleaned = []
        for s in rows:
            code = str(s.get("itemCode", "")).strip()
            name = str(s.get("stockName", "")).strip()
            # 보통주만(ETF/ETN/리츠 등 제외) + 6자리 코드.
            if s.get("stockEndType") != "stock":
                continue
            if not (len(code) == 6 and code.isdigit()) or not name:
                continue
            try:
                tv = float(s.get("accumulatedTradingValueRaw") or 0)
            except (TypeError, ValueError):
                tv = 0.0
            cleaned.append((tv, code, name))
        cleaned.sort(key=lambda x: x[0], reverse=True)
        for _tv, code, name in cleaned[:top_per_market]:
            out.append({"code": code, "name": name, "market": "KR", "_mk": mk})
        print(f"[analyze] 네이버 유니버스 {mk}: 후보 {len(cleaned)} → 상위 "
              f"{min(top_per_market, len(cleaned))} 선정(거래대금순)")
    return out


# ── 수급(외국인·기관)·재무(PER/PBR) — 네이버 integration(로그인 불필요) ───────────
def _parse_num(s):
    """'+2,880,306' · '26.07배' · '4.48배' · '47.63%' → float. 비수치는 None."""
    if s is None:
        return None
    t = (str(s).replace(",", "").replace("+", "").replace("배", "")
         .replace("%", "").replace("원", "").strip())
    try:
        return float(t)
    except (TypeError, ValueError):
        return None


def fetch_supply_finance(code):
    """네이버 종목 integration API 로 **수급(외국인·기관 최근 순매수)·재무(PER/PBR)**
    를 1회 호출로 수집한다(로그인 불필요·클라우드 동작). 실패 시 None."""
    url = f"https://m.stock.naver.com/api/stock/{code}/integration"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UNIVERSE_UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    out = {}
    # 수급 — dealTrendInfos(최근 거래일별) 외국인·기관 순매수 수량 합.
    f_sum = o_sum = 0.0
    days = 0
    # dealTrendInfos 는 최근 거래일이 첫 행(실측 확인). 첫 유효행을 1거래일 값으로 보존.
    f_1d = o_1d = None
    d1_date = None
    for row in d.get("dealTrendInfos") or []:
        f = _parse_num(row.get("foreignerPureBuyQuant"))
        o = _parse_num(row.get("organPureBuyQuant"))
        if f is not None:
            f_sum += f
        if o is not None:
            o_sum += o
        if f_1d is None and o_1d is None and (f is not None or o is not None):
            f_1d, o_1d = f, o
            bz = str(row.get("bizdate") or "").strip()
            if len(bz) == 8 and bz.isdigit():
                d1_date = f"{bz[:4]}-{bz[4:6]}-{bz[6:]}"
        days += 1
    if days:
        out["for_sum"] = f_sum
        out["org_sum"] = o_sum
        out["sd_days"] = days
        # 가장 최근 1거래일 외국인·기관 순매수(표시 부각용 — 점수엔 미반영).
        if f_1d is not None:
            out["for_1d"] = f_1d
        if o_1d is not None:
            out["org_1d"] = o_1d
        if d1_date:
            out["sd_1d_date"] = d1_date
    # 재무 — totalInfos 의 PER/PBR.
    for x in d.get("totalInfos") or []:
        if x.get("code") == "per":
            out["per"] = _parse_num(x.get("value"))
        elif x.get("code") == "pbr":
            out["pbr"] = _parse_num(x.get("value"))
    return out or None


_FRGN_URL = "https://finance.naver.com/item/frgn.naver?code={code}&page={page}"
# frgn 표 한 행의 숫자 셀(종가·전일비·등락률·거래량·기관·외국인·보유주수·보유율).
_FRGN_NUM = re.compile(r'class="tah[^"]*">\s*([+\-0-9.,%]+)\s*<')
_FRGN_DATE = re.compile(r'(\d{4}\.\d{2}\.\d{2})</span></td>')


def fetch_supply_history(code, pages=15):
    """네이버 frgn 페이지에서 **일별 외국인·기관 순매매량 이력**을 긁는다(학습용).
    반환: {'YYYY-MM-DD': (외국인순매매, 기관순매매)} (실측·날조 없음). 실패 시 {}.
    integration API 는 5일치뿐이라, 과거 학습엔 이 페이지(페이지당 ~20거래일)를 쓴다."""
    out = {}
    for pg in range(1, pages + 1):
        url = _FRGN_URL.format(code=code, page=pg)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UNIVERSE_UA})
            with urllib.request.urlopen(req, timeout=12) as r:
                html = r.read().decode("euc-kr", "replace")
        except Exception:
            break
        pos = list(_FRGN_DATE.finditer(html))
        if not pos:
            break
        for i, m in enumerate(pos):
            date = m.group(1)
            s = m.end()
            e = pos[i + 1].start() if i + 1 < len(pos) else s + 1500
            nums = _FRGN_NUM.findall(html[s:e])
            if len(nums) >= 6:  # [종가,전일비,등락률,거래량,기관,외국인,...]
                org = _parse_num(nums[4])
                frg = _parse_num(nums[5])
                if frg is not None or org is not None:
                    out[date.replace(".", "-")] = (frg or 0.0, org or 0.0)
    return out


def fetch_supply_history_batch(codes, pages=15):
    """여러 종목의 수급 이력을 스레드풀로 병렬 수집 → {code: {date:(frg,org)}}."""
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return out
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for c, hist in ex.map(
                    lambda c: (c, fetch_supply_history(c, pages)), codes):
                if hist:
                    out[c] = hist
    except Exception as ex:
        print(f"[analyze] 수급 이력 배치 수집 실패: {ex}")
    total = sum(len(v) for v in out.values())
    print(f"[learn] 수급 이력 수집: {len(out)}/{len(codes)} 종목 · {total} 종목일")
    return out


def fetch_supply_finance_batch(codes):
    """여러 종목의 수급·재무를 스레드풀로 병렬 수집 → {code: sf}. 실패 종목은 생략."""
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return out
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for c, sf in ex.map(lambda c: (c, fetch_supply_finance(c)), codes):
                if sf:
                    out[c] = sf
    except Exception as ex:
        print(f"[analyze] 수급/재무 배치 수집 실패: {ex}")
    print(f"[analyze] 수급·재무 수집: {len(out)}/{len(codes)} 종목")
    return out


# ── 매수 전환/가속 감지 파라미터 (★★사용자 최우선 지시: 외국인·기관이 "막 사기 시작"
# 하는 단계 = 그 어떤 기술적 요소보다 **압도적으로 크게** 반영). 외국인/기관이 오늘
# 매수로 전환(sign flip)했거나 가속 중이면, 그 종목이 다른 조건과 무관하게 최종 점수
# 순위에서 확실히 최상위권으로 튀어 올라야 한다. 그래서 이 보너스는 기존 개별 가중치
# 스케일(±14/±11, vol_surge 15, breakout 14 등 대부분 10대)을 압도하는 40~90점대로
# 잡고, **0~100 클램프 밖(apply_supply_finance 말미)에서 별도 가산**해 100 상한에
# 무력화되지 않게 한다(뒤따르는 apply_combo 도 이 헤드룸을 보존하도록 상한 완화).
# 대칭: 외국인/기관이 급격히 팔기 시작하는 '이탈'도 동일하게 압도적 감점(즉시 리스크 회피).
_SM_ACCEL_RATIO = 1.8   # 오늘 순매수가 직전 평균의 이 배 이상이면 '가속'으로 본다.
_SM_MIN_DAYS = 3        # 직전 평균은 (오늘 제외) 최소 2일치 필요 → sd_days≥3 일 때만.
_SM_FOR_FLIP, _SM_FOR_ACCEL = 50, 42   # 외국인 전환/가속 가감점(★압도적).
_SM_ORG_FLIP, _SM_ORG_ACCEL = 38, 30   # 기관 전환/가속(외국인 다음으로 중요).
_SM_ACCEL_KICKER_CAP = 20   # 가속이 강할수록 추가 가산(최대치) — 노이즈 폭주 방지 상한.


def _supply_basis_label(asof):
    """수급 전환/가속 근거에 붙일 **정직한 기준일 라벨**을 만든다.
    ★시세정합성 최우선 규칙★: 네이버/KIS/토스 모두 외국인·기관 순매수는 **장중엔
    갱신되지 않고**(전일 확정치), 장 마감 30~60분 뒤에야 당일(D) 확정치로 바뀐다.
    따라서 net_1d 는 '지금 이 순간'이 아니라 '가장 최근 확정 거래일'의 값이다.
    'net_1d 기준일(asof)==KST 오늘'일 때만 오늘 확정으로, 아니면 직전 확정일로 라벨링해
    '어제 데이터를 오늘 일어난 일'로 오인시키지 않는다."""
    if not asof:
        return "최근 확정 거래일 기준"
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    if asof == today:
        return f"{asof} 마감확정 기준"
    return f"최근 확정 {asof} 기준(장중이면 D-1)"


def _supply_momentum_one(net_1d, net_sum, nd, flip_pts, accel_pts,
                         label, asof, why, breakdown):
    """한 투자자(외국인/기관)의 **최근 확정 거래일(net_1d) vs 그 직전 며칠 평균** 흐름을
    점수에 반영한다. ★net_1d 는 실시간 아님 — 장중엔 D-1 확정치라 '기준일(asof)'을
    반드시 문구에 명시(오늘로 오인 금지, 시세정합성 규칙).★
    - 매수 전환: 직전 순매도/보합(prior_avg≤0)인데 최근 확정일이 순매수(net_1d>0) → 압도 가점.
    - 매수 가속: 직전에도 매수였지만 최근 확정일이 평균의 _SM_ACCEL_RATIO배 이상 → 압도 가점
      (가속이 강할수록 최대 _SM_ACCEL_KICKER_CAP 만큼 추가 가산).
    - 대칭: 순매도 전환·순매도 가속(외국인 이탈도 초단기엔 중요)은 같은 폭 감점.
    표본 부족(sd_days<_SM_MIN_DAYS)·1일치 결측이면 0 반환(보너스 없음·날조 금지).
    전환·가속은 상호배타(전환은 prior_avg≤0, 가속은 prior_avg>0 조건).
    반환: 점수 가감(0 가능·100 초과 유발 가능). why/breakdown 에 근거를 남긴다."""
    if net_1d is None or net_sum is None or nd is None or nd < _SM_MIN_DAYS:
        return 0
    prior_avg = (net_sum - net_1d) / (nd - 1)  # 최근 확정일 제외 그 직전 며칠 일평균.
    basis = _supply_basis_label(asof)
    if net_1d > 0:  # 최근 확정일 순매수 — 진입 우호.
        if prior_avg <= 0:  # 직전 순매도/보합 → 최근 확정일 순매수: 방향 전환 '시작'.
            why.append(f"{label} 매수 전환({basis})")
            breakdown.append(
                f"{label} 매수 전환 감지({basis}: 직전 순매도/보합→순매수) — "
                f"압도적 우선 반영 +{flip_pts}")
            return flip_pts
        if net_1d >= _SM_ACCEL_RATIO * prior_avg:  # 평소 매수의 배 이상: 가속.
            mult = min(net_1d / prior_avg, 99)  # 직전평균이 극소일 때 배수 폭주 방지.
            kick = max(0, min(_SM_ACCEL_KICKER_CAP,
                              round((mult - _SM_ACCEL_RATIO) * 6)))
            pts = accel_pts + kick
            why.append(f"{label} 매수 가속({basis})")
            breakdown.append(
                f"{label} 매수 가속({basis}: 평소 대비 {mult:.1f}배) — "
                f"압도적 우선 반영 +{pts}")
            return pts
    elif net_1d < 0:  # 최근 확정일 순매도 — 이탈 신호(초단기 중요).
        if prior_avg >= 0:  # 직전 순매수/보합 → 최근 확정일 순매도: 이탈 전환 '시작'.
            why.append(f"{label} 매도 전환({basis})")
            breakdown.append(
                f"{label} 매도 전환 감지({basis}: 직전 순매수/보합→순매도) — "
                f"압도적 우선 감점 -{flip_pts}")
            return -flip_pts
        if net_1d <= _SM_ACCEL_RATIO * prior_avg:  # 둘 다 음수: |확정일|≥배 → 매도 가속.
            mult = min(net_1d / prior_avg, 99)  # 둘 다 음수라 양수 배수.
            kick = max(0, min(_SM_ACCEL_KICKER_CAP,
                              round((mult - _SM_ACCEL_RATIO) * 6)))
            pts = accel_pts + kick
            why.append(f"{label} 매도 가속({basis})")
            breakdown.append(
                f"{label} 매도 가속({basis}: 평소 대비 {mult:.1f}배) — "
                f"압도적 우선 감점 -{pts}")
            return -pts
    return 0


def apply_supply_finance(score, why, breakdown, sf):
    """수급(외국인·기관 순매수)·재무(PER/PBR)를 점수에 반영하고 근거를 남긴다.
    수급은 초단기에 영향이 커 가중을 두고, 재무는 48h엔 약해 경량 보정만 한다.
    누적(nd일) 순매수에 더해, **오늘 1거래일이 직전 며칠 대비 전환/가속인지**를
    별도 보너스로 얹어 '외국인·기관이 막 사기 시작하는 단계'를 부각한다.
    sf 없으면(수집 실패) 점수 불변(날조 없음)."""
    if not sf:
        return score
    s = score
    f, o = sf.get("for_sum"), sf.get("org_sum")
    nd = sf.get("sd_days", 5)
    # 외국인·기관을 각각 대칭(+/-)으로 가산한다 — 순매수면 가점, **순매도면 같은 폭 감점**
    # (외국인 이탈도 초단기에 중요하므로 혼자 팔아도 반영). 외국인을 약간 더 무겁게.
    if f is not None:
        if f > 0:
            s += 14
            breakdown.append(f"외국인 순매수(최근 {nd}일) +14")
            why.append("외국인 순매수")
        elif f < 0:
            s -= 14
            breakdown.append(f"외국인 순매도(최근 {nd}일) -14")
            why.append("외국인 순매도")
    if o is not None:
        if o > 0:
            s += 11
            breakdown.append(f"기관 순매수(최근 {nd}일) +11")
            why.append("기관 순매수")
        elif o < 0:
            s -= 11
            breakdown.append(f"기관 순매도(최근 {nd}일) -11")
            why.append("기관 순매도")
    per, pbr = sf.get("per"), sf.get("pbr")
    if per is not None:
        if per <= 0:
            s -= 6
            breakdown.append(f"PER {per:g} 적자 -6")
        elif per <= 15:
            s += 5
            breakdown.append(f"PER {per:g} 저평가 +5")
        elif per >= 60:
            s -= 3
            breakdown.append(f"PER {per:g} 고평가 -3")
    if pbr is not None:
        if pbr < 1:
            s += 3
            breakdown.append(f"PBR {pbr:g} 자산가치 이하 +3")
        elif pbr >= 8:
            s -= 2
            breakdown.append(f"PBR {pbr:g} 고평가 -2")
    # ★0~100 양방향 클램프(P1-신호품질)★ 차트+국면+누적수급+재무 기저는 상한 100.
    base = max(0, min(100, round(s)))
    # ★★매수 전환/가속 = 압도적 우선(클램프 밖 별도 가산)★★ 외국인/기관이 오늘 막 사기
    # 시작(전환)했거나 가속 중이면 100 상한을 넘겨 얹어, 다른 모든 요소를 합친 것보다
    # 확실히 크게 최상위로 튀게 한다. 표본 부족·결측이면 0(날조 금지). 이탈은 대칭 감점.
    nd = sf.get("sd_days", 5)
    asof = sf.get("sd_1d_date")  # net_1d 의 실제 확정 거래일(장중이면 D-1) — 정직 라벨용.
    sm = 0
    sm += _supply_momentum_one(sf.get("for_1d"), sf.get("for_sum"), nd, _SM_FOR_FLIP,
                               _SM_FOR_ACCEL, "외국인", asof, why, breakdown)
    sm += _supply_momentum_one(sf.get("org_1d"), sf.get("org_sum"), nd, _SM_ORG_FLIP,
                               _SM_ORG_ACCEL, "기관", asof, why, breakdown)
    # 하한만 0 으로 보호(음수 이탈 감점 반영), 상한은 개방 → 전환/가속 종목이 100 초과로
    # 순위 최상위 확보. 이 초과 헤드룸은 뒤따르는 apply_combo 가 보존하도록 상한 완화됨.
    return max(0, base + sm)


# 차트 점수 가중치 기본값(사람이 정한 규칙값). 데이터 학습(learn_weights.py)으로
# 대체 가능 — control.json engine.learned_weights 로 주입(승인제). 각 키 = 항목별 기여점.
DEFAULT_WEIGHTS = {
    "base": 50,
    "align_up": 18, "align_down": -8,
    "above_ma20": 8,
    "vol_surge": 15, "vol_ok": 7, "vol_dry": -8,
    "strong_close": 14, "weak_close": -12,
    "rsi_up": 13, "rsi_hot": -10, "rsi_oversold": 4,
    "breakout": 14, "mom_up": 8,
    # 미시구조(요소 발굴) — 기본 0(현재 점수 불변). 학습으로 값이 붙으면 활성화.
    "gap_up": 0, "gap_down": 0, "vol_accel": 0, "streak_up": 0,
    "near_high": 0, "range_exp": 0, "ma20_up": 0,
    # 수급(요소 발굴) — 기본 0. Stage B(frgn 이력)에서 학습.
    "for_buy": 0, "for_sell": 0, "org_buy": 0, "org_sell": 0,
    # 고점추격 방어(과열 감점) — 기본 활성(작은 음수). edge 얇아 보수적(합산 최대 −15).
    "ext_ma20": -6, "ext_run": -5, "ext_near_high_hot": -4,
}

# 부호가 명백한 키(단타 모멘텀) — 학습값이 이 부호를 어기면 과적합 오류로 보고
# 학습세트 전체를 거부한다(P0-1). 양수여야 정상 / 음수여야 정상.
_W_POS = ("align_up", "above_ma20", "vol_surge", "vol_ok", "strong_close",
          "rsi_up", "rsi_oversold", "breakout", "mom_up", "gap_up",
          "vol_accel", "streak_up", "near_high", "range_exp", "ma20_up",
          "for_buy", "org_buy")
_W_NEG = ("align_down", "vol_dry", "weak_close", "rsi_hot", "gap_down",
          "for_sell", "org_sell", "ext_ma20", "ext_run", "ext_near_high_hot")

# 사람이 읽는 항목 이름(앱 학습 가중치 카드·설명용). DEFAULT_WEIGHTS 키와 1:1.
WEIGHT_LABELS = {
    "align_up": "정배열(MA5>MA20)", "align_down": "역배열(MA5<MA20)",
    "above_ma20": "현재가 > 20일선", "vol_surge": "거래량 급증(≥1.5x)",
    "vol_ok": "거래량 양호(≥1.0x)", "vol_dry": "거래량 위축(<0.6x)",
    "strong_close": "강세 마감(종가위치≥60%)", "weak_close": "윗꼬리/분배(<30%)",
    "rsi_up": "RSI 상승(50~70)", "rsi_hot": "RSI 과열(>75)",
    "rsi_oversold": "RSI 침체(<35)", "breakout": "변동성 돌파 상회",
    "mom_up": "5일 모멘텀 양(+)",
    "gap_up": "시가 갭상승(≥1%)", "gap_down": "시가 갭하락(≤-1%)",
    "vol_accel": "거래대금 가속(5d≥1.2×20d)", "streak_up": "연속 양봉(3일+)",
    "near_high": "20일 전고점 근접(≥98%)", "range_exp": "장대봉(범위≥1.5×ATR)",
    "ma20_up": "MA20 상승추세",
    "for_buy": "외국인 순매수", "for_sell": "외국인 순매도",
    "org_buy": "기관 순매수", "org_sell": "기관 순매도",
    "ext_ma20": "MA20 이격 과대(고점추격 주의)",
    "ext_run": "단기 급등 과열(5일)",
    "ext_near_high_hot": "전고점+RSI 동반 과열",
}


def chart_features(price, ind, supply=None):
    """지표(+선택적 수급)에서 점수 항목(피처) 충족 여부(1/0)를 뽑는다 — score_stock 과
    learn_weights.py 가 공유해 항상 같은 정의를 쓰게 한다. 키는 DEFAULT_WEIGHTS 와 1:1.
    supply: {'for': 외국인순매수합, 'org': 기관순매수합}(없으면 수급 피처는 0)."""
    vs = ind["vol_surge"]
    up = ind["ma5"] > ind["ma20"]
    f = {
        "align_up": 1 if up else 0,
        "align_down": 0 if up else 1,
        "above_ma20": 1 if price > ind["ma20"] else 0,
        "vol_surge": 1 if vs >= 1.5 else 0,
        "vol_ok": 1 if 1.0 <= vs < 1.5 else 0,
        "vol_dry": 1 if 0 < vs < 0.6 else 0,
        "strong_close": 1 if ind["close_pos"] >= 60 else 0,
        "weak_close": 1 if ind["close_pos"] < 30 else 0,
        "rsi_up": 1 if 50 <= ind["rsi"] <= 70 else 0,
        "rsi_hot": 1 if ind["rsi"] > 75 else 0,
        "rsi_oversold": 1 if ind["rsi"] < 35 else 0,
        "breakout": 1 if price >= ind["breakout"] else 0,
        "mom_up": 1 if ind["mom5"] > 0 else 0,
        # 미시구조.
        "gap_up": 1 if ind.get("gap_pct", 0) >= 1.0 else 0,
        "gap_down": 1 if ind.get("gap_pct", 0) <= -1.0 else 0,
        "vol_accel": 1 if ind.get("vol_accel", 0) >= 1.2 else 0,
        "streak_up": 1 if ind.get("up_streak", 0) >= 3 else 0,
        "near_high": 1 if ind.get("near_high20", 0) >= 0.98 else 0,
        "range_exp": 1 if ind.get("range_exp", 0) >= 1.5 else 0,
        "ma20_up": 1 if ind.get("ma20_slope", 0) > 0 else 0,
        # 수급(supply 있을 때만 1/0, 없으면 0).
        "for_buy": 1 if supply and supply.get("for", 0) > 0 else 0,
        "for_sell": 1 if supply and supply.get("for", 0) < 0 else 0,
        "org_buy": 1 if supply and supply.get("org", 0) > 0 else 0,
        "org_sell": 1 if supply and supply.get("org", 0) < 0 else 0,
        # 고점추격 방어(과열 감점) — 이미 계산된 지표만 사용(신규 데이터 0).
        "ext_ma20": 1 if ind.get("ma20_disparity", 0) >= 12.0 else 0,
        "ext_run": 1 if ind.get("mom5", 0) >= 15.0 else 0,
        "ext_near_high_hot": 1 if (ind.get("near_high20", 0) >= 0.99
                                   and ind.get("rsi", 0) > 72) else 0,
    }
    return f


# 점수 근거에 풍부한 문구가 따로 있는 '원본' 항목들(아래 하드코딩 블록에서 처리).
# 그 외(미시구조·수급) 신규 항목은 라벨 기반으로 일괄 가산한다.
_CORE_FEATURE_KEYS = {
    "align_up", "align_down", "above_ma20", "vol_surge", "vol_ok", "vol_dry",
    "strong_close", "weak_close", "rsi_up", "rsi_hot", "rsi_oversold",
    "breakout", "mom_up",
}


def score_stock(price, ind, weights=None, supply=None):
    """0~100 기술 점수 + (why, breakdown) 반환.
    - weights: 항목별 가중치(없으면 DEFAULT_WEIGHTS). 학습된 가중치를 주입하면 그
      점수로 계산하되 근거(breakdown)는 그대로 노출(설명 가능성 유지).
    - supply: {'for':..,'org':..} 수급(있으면 수급 항목도 학습 가중치로 반영).
    - why: 짧은 강세 근거(evidence 문구용).
    - breakdown: **항목별 점수 내역**(앱 '점수 근거' 팝업용). base 에서 시작해 각
      지표 기여를 +/- 로 적는다. 0점 항목은 생략. 합은 0~100 으로 제한된다."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    f = chart_features(price, ind, supply)
    s = float(w["base"])
    why = []
    bd = [f"기본 점수 +{int(round(w['base']))}"]

    def add(key, why_txt, bd_txt):
        nonlocal s
        pts = w.get(key, 0)
        if pts == 0:
            return  # 0점 항목(미사용·학습으로 꺼진 항목)은 점수·근거에서 생략.
        s += pts
        if why_txt:
            why.append(why_txt)
        bd.append(f"{bd_txt} {pts:+g}")

    if f["align_up"]:
        add("align_up", "정배열(MA5>MA20)", "정배열(MA5>MA20)")
    else:
        add("align_down", None, "역배열(MA5<MA20)")
    if f["above_ma20"]:
        add("above_ma20", None, "현재가가 20일선(MA20) 위")
    if f["vol_surge"]:
        add("vol_surge", f"거래량 급증 {ind['vol_surge']}x",
            f"거래량 급증 {ind['vol_surge']}배")
    elif f["vol_ok"]:
        add("vol_ok", None, f"거래량 양호 {ind['vol_surge']}배")
    elif f["vol_dry"]:
        add("vol_dry", f"거래량 위축 {ind['vol_surge']}x",
            f"거래량 위축(매수세 이탈) {ind['vol_surge']}배")
    if f["strong_close"]:
        add("strong_close", f"강세 마감(종가위치 {ind['close_pos']}%)",
            f"강세 마감(종가위치 {ind['close_pos']}%)")
    elif f["weak_close"]:
        add("weak_close", f"윗꼬리/분배(종가위치 {ind['close_pos']}%)",
            f"윗꼬리/분배(종가위치 {ind['close_pos']}%)")
    if f["rsi_up"]:
        add("rsi_up", f"RSI {ind['rsi']}(상승)",
            f"RSI {ind['rsi']} (상승 구간 50~70)")
    elif f["rsi_hot"]:
        add("rsi_hot", f"RSI {ind['rsi']}(과열)", f"RSI {ind['rsi']} (과열 >75)")
    elif f["rsi_oversold"]:
        add("rsi_oversold", None, f"RSI {ind['rsi']} (침체 반등 기대 <35)")
    if f["breakout"]:
        add("breakout", "변동성 돌파 상회", "변동성 돌파선 상회")
    if f["mom_up"]:
        add("mom_up", None, f"5일 모멘텀 +{ind['mom5']}% (양)")
    # 신규 요소(미시구조·수급) — 학습 가중치가 붙은(0 아님) 항목만 라벨로 가산.
    for key, flag in f.items():
        if key in _CORE_FEATURE_KEYS or not flag:
            continue
        add(key, None, WEIGHT_LABELS.get(key, key))
    # ★점수 상한 100 클램프(P1-신호품질)★ score_stock 단계에서 상한을 확보해
    # 이후 apply_regime/apply_supply_finance/apply_combo 가산 전 기저가 100을
    # 넘지 않도록 한다(클램프 미적용 시 외국인+기관+미국장 보정만 최대 +33이 추가
    # 되어 133점처럼 신호 순위가 왜곡되던 문제 방지). 음수 방어도 유지.
    return max(0, min(100, round(s))), why, bd


# 매매계획 보정 파라미터 기본값(통계 탭 학습으로 조정 가능). control.json engine.tuning.
DEFAULT_TUNING = {
    "stop_mult": 1.5,      # 손절폭 = stop_mult × hf × ATR
    "target1_mult": 2.0,   # 목표1 = 진입 + target1_mult × 위험
    "target2_mult": 3.0,   # 목표2 = 진입 + target2_mult × 위험
    "score_cutoff": 55,    # 신호 채택 점수 임계
    # 비중(위험균등 사이징) — 손절까지 갔을 때 잃을 자본을 risk_per_trade_pct 로 고정.
    "risk_per_trade_pct": 1.0,  # 한 트레이드에 거는 자본 위험(%)
    "max_weight_pct": 15.0,     # 종목 최대 비중(%)
    "min_weight_pct": 3.0,      # 종목 최소 비중(%)
    # 관리 정책(백테스트·권고 공통) — 목표1 부분익절 + 목표1 후 본전 스톱.
    "partial_t1_frac": 0.5,     # 목표1 도달 시 청산할 비중(0=부분익절 안 함)
    "breakeven_after_t1": 1.0,  # 목표1 후 손절을 본전으로(1=적용, 0=원손절 유지)
}


def levels(price, ind, hold_cap, stop_mult=1.5, target1_mult=2.0, target2_mult=3.0):
    """진입/손절/목표 레벨 계산(매매계획·백테스트 공용). 반환 dict.
    stop_mult·targetN_mult 로 손절·목표 폭을 조정한다(학습 보정 적용 지점)."""
    atr = ind["atr"]
    breakout = ind["breakout"]
    if price >= breakout and ind["close_pos"] >= 50:
        entry = round_tick(price)
        etype = "now"
    else:
        entry = round_tick(max(breakout, ind["day_high"]))
        etype = "breakout"
    hf = max(0.6, min(1.8, math.sqrt(hold_cap / 24.0)))
    stop = round_tick(max(entry - stop_mult * hf * atr, ind["recent_low5"]))
    if stop >= entry:
        stop = round_tick(entry * (1 - 0.02 * hf))
    # ★손절 하한(P2)★ 변동성 큰 날 recent_low5 가 진입에 붙어 손절거리가 비현실적으로
    # 좁아지는 것 방지 — 손절거리 최소 0.5·ATR(또는 1.5%) 확보.
    min_risk = max(0.5 * atr, entry * 0.015)
    if entry - stop < min_risk:
        stop = round_tick(entry - min_risk)
    risk = entry - stop
    # ★도달성(P0-3·스킬D)★ 48h 단타는 목표를 ATR 절대폭으로도 캡한다 — R 배수만 쓰면
    # ATR 큰 종목의 목표가 +9~20%로 멀어져 시간청산만 나던 문제(forward 0%) 완화.
    # target1 ≤ entry+1.2·ATR, target2 ≤ entry+2.0·ATR.
    target1 = round_tick(min(entry + target1_mult * risk, entry + 1.2 * atr))
    target2 = round_tick(min(entry + target2_mult * risk, entry + 2.0 * atr))
    if target2 <= target1:
        target2 = round_tick(target1 + 0.4 * atr)
    # 돌파 진입가가 현재가 +2.5% 초과면 '쫓는' 신호 → 도달성 낮음(관찰 강등 후보).
    reachable = not (etype == "breakout" and entry > price * 1.025)
    return {"entry": entry, "stop": stop, "target1": target1,
            "target2": target2, "etype": etype, "risk": risk,
            "reachable": reachable}


def position_weight(entry, stop, score, cutoff, tuning):
    """**위험균등(risk-parity) 비중 산정** — 손절까지 갔을 때 잃는 자본을
    risk_per_trade_pct 로 고정한다(손절이 넓은 위험한 종목일수록 자동으로 작게).
    그 위에 확신도(점수)·진입유형으로 가감하고 [min,max]%로 캡한다.
    반환: (weight_pct, stop_pct, est_loss_pct)."""
    t = {**DEFAULT_TUNING, **(tuning or {})}
    stop_dist = (entry - stop) / entry if entry else 0  # 손절까지 거리(비율)
    if stop_dist <= 0:
        return t["min_weight_pct"], 0.0, 0.0
    risk_budget = t["risk_per_trade_pct"]
    raw = risk_budget / (stop_dist * 100) * 100  # = risk_budget / 손절거리% × 100
    # 확신도 스케일: 컷오프에서 0.6배 → 85점 이상 1.0배.
    span = max(1.0, 85.0 - cutoff)
    conf = max(0.0, min(1.0, (score - cutoff) / span))
    conf_scale = 0.6 + 0.4 * conf
    w = raw * conf_scale
    w = max(t["min_weight_pct"], min(t["max_weight_pct"], w))
    est_loss = w * stop_dist  # 손절 시 자본 손실(%) ≈ 비중 × 손절거리
    return round(w), round(stop_dist * 100, 1), round(est_loss, 2)


def score_bucket(score):
    """점수 → 구간 라벨(backtest 와 동일 경계). 구간별 차등 튜닝 적용에 사용."""
    if score >= 75:
        return "75+"
    if score >= 65:
        return "65-74"
    return "55-64"


def effective_mults(tuning, score):
    """전역 tuning + (있으면) 점수구간별 by_bucket 을 합쳐 손절·목표 배수를 정한다."""
    t = {**DEFAULT_TUNING, **(tuning or {})}
    sm, t1, t2 = t["stop_mult"], t["target1_mult"], t["target2_mult"]
    bb = (tuning or {}).get("by_bucket") if tuning else None
    if isinstance(bb, dict):
        mv = bb.get(score_bucket(score))
        if isinstance(mv, dict):
            sm = mv.get("stop_mult", sm)
            t1 = mv.get("target1_mult", t1)
            t2 = mv.get("target2_mult", t2)
    return sm, t1, t2


def _supply_fields(sf):
    """수급(sf) → feed 출력 필드. 외국인·기관 최근 순매수 합(주식수)·표본일수.
    부호가 방향(>0 순매수·<0 순매도)이다. 미확보 시 빈 dict(표시 생략)."""
    if not sf:
        return {}
    out = {}
    if sf.get("for_sum") is not None:
        out["foreign_net"] = round(float(sf["for_sum"]), 1)
    if sf.get("org_sum") is not None:
        out["inst_net"] = round(float(sf["org_sum"]), 1)
    if out:
        out["supply_days"] = int(sf.get("sd_days", 5))
        # ★최근 1거래일 부각★ 외국인·기관 순매수(주식수, 부호=방향). 점수엔 미반영,
        # 표시 전용 — 5일 합산이 누적이라 '오늘 들어오는/나가는 흐름'을 별도 노출.
        if sf.get("for_1d") is not None:
            out["foreign_net_1d"] = round(float(sf["for_1d"]), 1)
        if sf.get("org_1d") is not None:
            out["inst_net_1d"] = round(float(sf["org_1d"]), 1)
        if sf.get("sd_1d_date"):
            out["supply_1d_date"] = sf["sd_1d_date"]
    return out


def _combo_fields(ind):
    """ind 에 부착된 조합 승률 → feed 출력 필드(앱 표시·투명성용). 미부착 시 빈 dict."""
    if ind.get("combo_winrate") is None:
        return {}
    return {
        "combo_winrate": ind.get("combo_winrate"),
        "combo_sample": ind.get("combo_sample"),
        "combo_lift": ind.get("combo_lift"),
    }


def build_signal(rank, item, price, change_pct, ind, hold_cap, tuning=None,
                 score=60, cutoff=55, chase=False):
    """기술 점수 통과 종목 → 매매계획 신호 dict. tuning(없으면 기본)로 손절·목표 조정.
    score 구간별 차등 배수(by_bucket)가 있으면 반영하고, 비중은 위험균등으로 산정."""
    t = {**DEFAULT_TUNING, **(tuning or {})}
    sm, t1m, t2m = effective_mults(tuning, score)
    lv = levels(price, ind, hold_cap, sm, t1m, t2m)
    entry, stop, etype, risk = lv["entry"], lv["stop"], lv["etype"], lv["risk"]
    target1, target2 = lv["target1"], lv["target2"]
    if etype == "now":
        enote = f"기술 점수 상위·돌파 상회. 현재가({entry:,}) 부근 즉시 진입 가능, 거래량 확인."
    else:
        enote = f"{entry:,} 돌파 + 거래량 동반 시 진입(미돌파 시 미진입). 추격금지."
    rr = round((target1 - entry) / risk, 2) if risk > 0 else 0
    # 위험균등 비중 — 손절 거리·확신도 반영. 돌파대기는 미체결 위험으로 0.85배.
    weight, stop_pct, est_loss = position_weight(entry, stop, score, cutoff, t)
    if etype == "breakout":
        weight = int(max(t["min_weight_pct"], round(weight * 0.85)))
        est_loss = round(weight * stop_pct / 100, 2)
    # ★데이터 충분성 가드(P1-신호품질)★ 일봉 표본이 30 미만이면 ATR14·RSI14·MA20
    # 지표 신뢰도가 낮아 과대점수 위험. data_quality 필드로 기록해 앱·로그가 인지.
    n_bars = ind.get("_n_bars", 99)
    data_quality = "low" if n_bars < 30 else ("ok" if n_bars < 45 else "good")
    risk_notes_base = [
        "촉매(뉴스) 미확인 — 기술 신호만. 진입 전 재료·시초가 갭 확인.",
        f"비중 {weight}%는 위험균등 산정 — 손절({stop_pct}%) 도달 시 자본 약 "
        f"{est_loss}% 손실 수준(위험 {t['risk_per_trade_pct']}%/트레이드 기준).",
        f"목표1 도달 시 손절을 본전으로 올려 이익 보호 · 보유 {hold_cap}h 경과 시 잔여 청산.",
    ]
    if data_quality == "low":
        risk_notes_base.insert(0, f"⚠️ 일봉 표본 {n_bars}봉으로 부족 — ATR·MA20 신뢰도 낮음. 결과 참고만.")
    return {
        "rank": rank, "name": item["name"], "code": item["code"], "market": "KR",
        "direction": "long", "confidence": "mid" if etype == "now" else "low",
        "price": float(price), "currency": "KRW", "entry": float(entry),
        "entry_type": etype, "entry_note": enote, "stop": stop,
        "target1": target1, "target2": target2, "rr": rr,
        "atr_pct": ind["atr_pct"], "hold_cap_hours": hold_cap, "weight_pct": weight,
        # 거래량 평소 대비 배수(오늘 거래량 ÷ 20일 평균) — 앱 주가탭 거래량 게이지용.
        "vol_surge": ind["vol_surge"],
        # 수급(외국인·기관 최근 순매수 합·일수) — 앱 주가탭 수급 아이콘용.
        **_supply_fields(ind.get("_sf")),
        # 조합별 과거 승률(점수×거래량×외국인×기관) — 표본 충분 시만 부착(graceful).
        **_combo_fields(ind),
        "stop_pct": stop_pct, "est_loss_pct": est_loss,
        # ★데이터 충분성(P1)★ — "low"(n<30)·"ok"(30~44)·"good"(45+). 앱 참고용.
        "data_quality": data_quality,
        "catalyst_verified": False, "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "evidence": "기술 분석(뉴스 미확인) — " + ", ".join(ind["_why"]) +
                    f". ATR {ind['atr_pct']}%·5일모멘텀 {ind['mom5']}%.",
        # 앱 '점수 근거' 팝업용 — 항목별 점수 내역(기본 50 + 각 지표 기여).
        "score_reasons": list(ind.get("_breakdown", [])),
        "risk_notes": risk_notes_base,
        "tags": ["기술분석", "온디맨드"]
                + (["돌파대기"] if etype == "breakout" else ["즉시진입"])
                + (["추격주의"] if chase else [])
                + (["데이터부족"] if data_quality == "low" else []),
        # 도달성(P0-3): 돌파 진입가가 현재가 +2.5% 초과면 False → 빌드 루프가 관찰 강등.
        "reachable": lv.get("reachable", True),
        # 5분봉 시계열(앱 '최근 24시간 차트'용). 수집 실패 시 빈 리스트([]).
        "intraday_5m": ind.get("intraday_5m", []),
        # 최근 7거래일 일봉(앱 신호카드 '1주일 차트'용).
        "daily_7d": ind.get("daily_7d", []),
        # 최근 ~1달(약 22거래일) 일봉. 앱 미니차트가 daily_7d 보다 우선 사용.
        "daily_30d": ind.get("daily_30d", []),
        # 최근 ~3달(약 65거래일) 일봉. 앱이 '3달 차트'에 사용할 경우 이 값을 우선.
        "daily_90d": ind.get("daily_90d", []),
    }


# ── 미국 시세 견고 조회 헬퍼(최우선 정합성 규칙) ────────────────────────────
# ★사고(2026-06-16) 교훈★ 일봉을 직접 closes[-1]/closes[-2] 로 차분하면
#   (1) 미국장이 열린 시각에 돌면 마지막 칸이 '아직 안 끝난 부분봉'인데 종가로 오인하고
#   (2) 야후 일봉에 거래일이 통째로 누락되면 엉뚱한 과거일이 기준이 되어
#   등락률의 '방향'까지 뒤집힌다(나스닥 실제 -1.15% 가 +2.52% 로 표시됨).
# → 세션 경계를 야후가 직접 책임지는 previous_close 를 1순위 기준으로 쓴다.
US_INDEX_SYMS = {"^IXIC", "^GSPC", "^DJI", "^SOX"}


def _us_now_et():
    """미국 동부 현재 시각(서머타임 자동). zoneinfo 실패 시 KST-13h 근사."""
    try:
        import zoneinfo
        return datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return datetime.datetime.now(KST).replace(tzinfo=None) - datetime.timedelta(hours=13)


def _us_today_et():
    """미국 동부 기준 '오늘' 날짜(마지막 일봉이 당일 세션인지 판별용)."""
    return _us_now_et().date()


def _us_market_open():
    """미 정규장(평일 09:30–16:00 ET) 개장 여부. 마지막 일봉이 '진행중 부분봉'
    인지 판별해 live 오탐(마감 후를 장중으로 표기)을 막는다. 공휴일은 미반영
    (그날은 일봉이 없어 어차피 live=False)."""
    n = _us_now_et()
    if n.weekday() >= 5:  # 토·일
        return False
    mins = n.hour * 60 + n.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def _us_quote(sym):
    """미국 지수/종목의 견고한 시세 dict 또는 None.

    1순위: fast_info.previous_close(야후가 직전 정규장 종가를 직접 계산 — 누락일·
    부분봉의 영향을 받지 않음). 2순위: 일봉 백업. 둘 다 검증을 통과해야 한다.
    반환: {price, change_pct, asof('YYYY-MM-DD'), live(bool), prev_close} 또는 None.
    """
    import yfinance as yf
    last = prev = None
    asof = None
    live = False
    # 일봉(asof·live 판별·백업·검증용)
    dates, closes = [], []
    try:
        h = yf.Ticker(sym).history(period="8d", auto_adjust=False)
        h = h.dropna(subset=["Close"])
        dates = [d.date() for d in h.index]
        closes = [float(x) for x in h["Close"]]
    except Exception:
        pass
    if dates:
        asof = dates[-1].strftime("%Y-%m-%d")
        # 마지막 일봉이 '오늘(ET)'이고 정규장이 실제 열려 있을 때만 장중(부분봉).
        live = dates[-1] >= _us_today_et() and _us_market_open()
    # 1순위 기준: 야후가 계산한 previous_close
    try:
        fi = yf.Ticker(sym).fast_info
        lp = float(fi.last_price)
        pc = float(fi.previous_close)
        if lp > 0 and pc > 0:
            last, prev = lp, pc
    except Exception:
        pass
    # 백업: 일봉 차분(단, 마지막 칸이 진행중 세션이면 그 직전 종가를 기준으로)
    if last is None and len(closes) >= 2 and closes[-2] > 0:
        last, prev = closes[-1], closes[-2]
    if last is None or not prev:
        return None
    chg = (last / prev - 1) * 100
    # 정합성 게이트: 비현실적 등락률은 데이터 오류로 보고 폐기(방향 뒤집힘 사고 차단)
    cap = 20.0 if sym in US_INDEX_SYMS else 45.0
    if abs(chg) > cap:
        print(f"[analyze] ⚠️ 미국 {sym} 등락률 이상치 {chg:+.2f}% (>{cap}%) — 폐기")
        return None
    return {"price": round(last, 2), "change_pct": round(chg, 2),
            "asof": asof, "live": live, "prev_close": round(prev, 2)}


# ── 시장 환경(거시) 보정 — 측정 가능한 미국증시만 ───────────────────────────
def fetch_us_regime():
    """전일 미국 증시 등락으로 **시장 환경 보정치**를 계산한다(측정 가능한 거시만).

    한국 개장은 전일 미국장에 강하게 동조하므로, S&P500·나스닥·**SOX(반도체,
    KR 영향 큼)** 전일 등락률의 가중평균을 점수 보정(-8~+8)으로 환산한다. 금리·
    지정학 등 정성적 거시는 **수치 날조 위험**이 있어 점수에 넣지 않고 뉴스 종합분석의
    정성 판단/리스크 노트에 맡긴다. yfinance 실패 시 None(보정 없이 진행).

    반환: {'sp','nasdaq','sox'(각 %), 'regime_pct'(가중평균%), 'adj'(-8~+8)} 또는 None.
    """
    try:
        import yfinance as yf
    except Exception:
        return None
    syms = {"sp": "^GSPC", "nasdaq": "^IXIC", "sox": "^SOX"}
    weights = {"sp": 0.30, "nasdaq": 0.30, "sox": 0.40}
    chg = {}
    for k, sym in syms.items():
        q = _us_quote(sym)  # ★previous_close 기준 견고 조회(부분봉·누락일 방어)
        if q:
            chg[k] = q["change_pct"]
    if not chg:
        return None
    den = sum(weights[k] for k in chg)
    regime_pct = sum(chg[k] * weights[k] for k in chg) / den if den else 0.0
    # 전일 미국장 +2% ≈ +5점, ±8 클램프(기술 신호를 압도하지 않는 보조 보정).
    adj = max(-8.0, min(8.0, regime_pct * 2.5))
    out = {k: round(v, 2) for k, v in chg.items()}
    out["regime_pct"] = round(regime_pct, 2)
    out["adj"] = round(adj, 1)
    return out


def apply_regime(score, why, breakdown, regime):
    """기술 점수에 시장 환경 보정(adj)을 더해 0~100 으로 클램프하고 근거를 남긴다.
    why(짧은 근거)·breakdown(항목별 내역) 양쪽에 미국증시 보정 항목을 추가한다."""
    if not regime or not regime.get("adj"):
        return score
    adj = regime["adj"]
    parts = []
    for k, lbl in (("sp", "S&P"), ("nasdaq", "나스닥"), ("sox", "SOX")):
        if k in regime:
            parts.append(f"{lbl}{regime[k]:+g}%")
    note = f"미국증시 환경 {adj:+g} (전일 {'·'.join(parts)})"
    why.append(note)
    breakdown.append(note)
    # ★0~100 양방향 클램프(P1-신호품질)★ score_stock 에서 이미 min(100,...) 적용됐으나
    # 이 함수가 단독 호출될 수 있어 상한도 명시한다.
    return max(0, min(100, round(score + adj)))


# ── 미국 야간 컨텍스트(지수·빅테크·한줄평) 실측 갱신 ───────────────────────────
US_INDICES = [("나스닥", "^IXIC"), ("S&P500", "^GSPC"),
              ("다우", "^DJI"), ("필라델피아반도체(SOX)", "^SOX")]
US_BIGTECH = [("엔비디아", "NVDA"), ("마이크론", "MU"), ("브로드컴", "AVGO"),
              ("애플", "AAPL"), ("마이크로소프트", "MSFT"), ("알파벳", "GOOGL"),
              ("아마존", "AMZN"), ("메타", "META"), ("테슬라", "TSLA")]


def fetch_kr_context_naver():
    """코스피·코스닥·코스피200 현재지수·등락률을 네이버 SERVICE_INDEX 로 실측해
    kr_context(asof·session·indices)를 만든다. EUC-KR 안전 디코딩, 부호는 rf 로
    확정(INC-002). intraday-refresh cron 스킵 시에도 analyze 가 self-heal 한다.
    실패 시 None(호출 측이 기존 kr_context 보존)."""
    import urllib.request
    NAME = {"KOSPI": ("코스피", "KOSPI"), "KOSDAQ": ("코스닥", "KOSDAQ"),
            "KPI200": ("코스피200", "KOSPI200")}
    url = ("https://polling.finance.naver.com/api/realtime"
           "?query=SERVICE_INDEX:KOSPI,KOSDAQ,KPI200")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"})
        raw = urllib.request.urlopen(req, timeout=8).read()
    except Exception as ex:
        print(f"[analyze] KR 지수 네이버 실패: {ex}")
        return None
    d = None
    for enc in ("utf-8", "cp949"):
        try:
            d = json.loads(raw.decode(enc))
            break
        except Exception:
            continue
    if not d:
        return None
    indices, open_any = [], False
    areas = ((d.get("result") or {}).get("areas") or [{}])
    for it in (areas[0].get("datas") if areas else []) or []:
        cd = it.get("cd")
        nv = it.get("nv")
        if cd not in NAME or nv is None:
            continue
        name, sym = NAME[cd]
        try:
            price = round(float(nv) / 100.0, 2)
            cr = it.get("cr")
            chg = _sign_cr(float(cr), it.get("rf")) if cr is not None else 0.0
        except (TypeError, ValueError):
            continue
        indices.append({"name": name, "symbol": sym,
                        "price": price, "change_pct": round(chg, 2)})
        if str(it.get("ms", "")).upper() == "OPEN":
            open_any = True
    if not indices:
        return None
    now = datetime.datetime.now(KST).replace(microsecond=0, second=0)
    # ★정합성 self-heal★ market_state.korea(status/basis/asof)도 같은 네이버 실측에서
    # 함께 산출한다 — 이 블록은 intraday_refresh 만 갱신해 왔는데, GitHub cron 스킵으로
    # 장중 한 번도 못 돌면 며칠씩 옛 시각에 멈췄다(2026-06-19 market_state.korea 가
    # 06-17 13:28 에 고정된 사고). analyze 는 예약·온디맨드로 안정적으로 돌므로 여기서
    # 항상 신선화한다. 세션은 ms=OPEN(정규장) + KST 창(08:00~20:00 NXT)으로 판정.
    minutes = now.hour * 60 + now.minute
    in_window = (8 * 60) <= minutes <= (20 * 60) and now.weekday() < 5
    if open_any:
        st_status, st_session = "open", "regular"
        st_basis = "정규장 실시간(네이버 지수 실측)"
    elif in_window and (8 * 60) <= minutes < (9 * 60):
        st_status, st_session = "pre", "pre"
        st_basis = "장전 NXT 프리마켓(08:00~09:00, 네이버 지수 실측)"
    elif in_window and (15 * 60 + 30) < minutes <= (20 * 60):
        st_status, st_session = "post", "after"
        st_basis = "장후 NXT 애프터마켓(15:30~20:00, 네이버 지수 실측)"
    else:
        st_status, st_session = "closed", "closed"
        st_basis = "전일 종가(장 마감)"
    return {"asof": now.isoformat(),
            "session": "regular" if open_any else st_session,
            "stale": False,  # 방금 네이버 실측 — 신선(앱 신선도 게이트용 boolean)
            "indices": indices,
            # 호출 측(main)이 feed['market_state']['korea'] 에 적용한다(아래 _ms 키는
            # kr_context 본문에는 싣지 않고 main 에서 분리해 쓴다).
            "_market_state_korea": {"status": st_status, "basis": st_basis,
                                    "session": st_session, "asof": now.isoformat()}}


def fetch_us_context():
    """미국 지수·빅테크 전일(또는 실시간) 종가·등락률을 yfinance 로 실측해 us_context
    dict 를 만든다. 한줄평·한국영향은 **측정된 수치에서 결정적으로 생성**(날조 없음).
    실패 시 None(호출 측이 기존 us_context 보존). 차트 엔진이 매 실행 갱신하므로
    미국 카드가 더 이상 옛 날짜에 멈추지 않는다.

    ★정합성★ 등락률은 _us_quote 의 previous_close 기준(부분봉·누락일 방어).
    asof 는 종목별 실제값을 그대로 싣는다(전 종목 동일 날짜로 덮어쓰지 않음)."""
    try:
        import yfinance as yf  # noqa: F401  (가용성 가드)
    except Exception:
        return None

    indices, bigtech = [], []
    asofs, live_any = [], False
    for name, sym in US_INDICES:
        q = _us_quote(sym)
        if q:
            indices.append({"name": name, "symbol": sym, "price": q["price"],
                            "change_pct": q["change_pct"], "asof": q["asof"],
                            "live": q["live"]})
            if q["asof"]:
                asofs.append(q["asof"])
            live_any = live_any or q["live"]
    for name, sym in US_BIGTECH:
        q = _us_quote(sym)
        if q:
            bigtech.append({"name": name, "symbol": sym, "price": q["price"],
                            "change_pct": q["change_pct"], "asof": q["asof"],
                            "live": q["live"]})
            if q["asof"]:
                asofs.append(q["asof"])
            live_any = live_any or q["live"]
    if not indices and not bigtech:
        return None
    asof = max(asofs) if asofs else None

    # 신선도 게이트: asof 가 영업일 기준 너무 오래되면 발행 측이 알 수 있게 표기.
    stale = False
    try:
        if asof:
            age = (_us_today_et() - datetime.date.fromisoformat(asof)).days
            stale = age > 5  # 주말·공휴일 여유 포함. 그 이상이면 데이터 멈춤 의심.
            if stale:
                print(f"[analyze] ⚠️ 미국 컨텍스트 신선도 경고: asof={asof} ({age}일 전)")
    except Exception:
        pass

    # 한줄평·한국영향 — 측정값에서 결정적으로 생성(추측 없음).
    sox = next((i for i in indices if "SOX" in i["symbol"]), None)
    sp = next((i for i in indices if i["symbol"] == "^GSPC"), None)
    nq = next((i for i in indices if i["symbol"] == "^IXIC"), None)
    up = sum(1 for b in bigtech if b["change_pct"] > 0)
    down = sum(1 for b in bigtech if b["change_pct"] < 0)
    parts = []
    if sp:
        parts.append(f"S&P {sp['change_pct']:+g}%")
    if nq:
        parts.append(f"나스닥 {nq['change_pct']:+g}%")
    if sox:
        parts.append(f"SOX {sox['change_pct']:+g}%")
    live_tag = "장중(실시간) " if live_any else ""
    summary = (("미국 " + live_tag + ", ".join(parts) + ". ") if parts else "") + \
        f"빅테크 상승 {up}·하락 {down}." + (" ⚠️데이터 신선도 점검 필요." if stale else "")
    if sox:
        if sox["change_pct"] >= 0.5:
            kr_impl = f"미 반도체 강세(SOX {sox['change_pct']:+g}%) — 삼성전자·SK하이닉스 등 반도체 우호적."
        elif sox["change_pct"] <= -0.5:
            kr_impl = f"미 반도체 약세(SOX {sox['change_pct']:+g}%) — 반도체 단기 부담."
        else:
            kr_impl = f"미 반도체 보합(SOX {sox['change_pct']:+g}%) — 반도체 영향 중립."
    else:
        kr_impl = "미국 반도체(SOX) 데이터 미확보 — 한국 영향 판단 보류."
    return {
        "asof": asof or datetime.datetime.now(KST).strftime("%Y-%m-%d"),
        "basis": ("미국 장중 실시간(야후 실측)" if live_any
                  else "미국 정규장 종가(야후 실측)"),
        "session": "open" if live_any else "closed",
        "live": live_any,
        "stale": stale,
        "summary": summary,
        "kr_implication": kr_impl,
        "indices": indices,
        "bigtech": bigtech,
    }


def generate_market_outlook(signals, kr_context, us_context):
    """실측 지표(한국 지수 등락·신호 분포·미국 야간)만으로 오늘의 증시 전망을
    규칙 기반으로 생성한다. 수치 날조 없음 — 미확보 값은 판정에서 제외.
    미래보장 아님: 현재·과거 지표 기반 분위기 판단(참고용).

    반환:
        {regime, headline, buy_view, sell_view, basis, asof} 또는 None(오류·데이터 부족).

    regime 4단계:
        '강세'  — 지수↑ + 신호 다수 + 미국 우호가 겹칠 때
        '약세'  — 지수↓ + 신호 적음 + 미국 부담이 겹칠 때
        '혼조'  — 코스피·코스닥 방향 엇갈리거나 지수·신호 방향 불일치
        '중립'  — 뚜렷한 방향 없음(보합권)
    """
    try:
        from collections import Counter

        now_kst = datetime.datetime.now(KST).replace(microsecond=0)
        asof = now_kst.isoformat()

        # ── 1. 한국 지수 등락률 수집(실측값만) ──────────────────────
        kr_indices = (kr_context or {}).get("indices", [])
        kospi_chg = next(
            (i["change_pct"] for i in kr_indices if i.get("symbol") == "KOSPI"), None)
        kosdaq_chg = next(
            (i["change_pct"] for i in kr_indices if i.get("symbol") == "KOSDAQ"), None)
        main_idx_chg = kospi_chg if kospi_chg is not None else kosdaq_chg

        # ── 2. 신호 분포 분석 ─────────────────────────────────────
        n_signals = len(signals) if signals else 0
        # signals 변수는 _score 가 살아있는 list(feed.update 이전 raw list).
        scores = [s.get("_score", 0) for s in (signals or [])]
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

        # 주도 테마: 신호 태그에서 기술·합의 메타 태그 제외 후 빈도 상위 3개
        _meta_tags = {"합의2", "합의3", "기술주도", "촉매주도",
                      "돌파대기", "정배열", "역배열", "눌림목"}
        tag_cnt = Counter()
        for s in (signals or []):
            for t in s.get("tags", []):
                if t not in _meta_tags:
                    tag_cnt[t] += 1
        top_themes = [t for t, _ in tag_cnt.most_common(3)]

        # ── 3. 미국 야간 지수(실측값만) ──────────────────────────
        usc = us_context or {}
        us_indices = usc.get("indices", [])
        sp_chg = next(
            (i["change_pct"] for i in us_indices if i.get("symbol") == "^GSPC"), None)
        sox_chg = next(
            (i["change_pct"] for i in us_indices
             if "SOX" in i.get("symbol", "")), None)

        # ── 4. regime 점수 합산 ─────────────────────────────────
        # 지수 기여: ±2, 신호수 기여: ±1, 미국 기여: ±1
        reg_score = 0
        basis_parts = [f"매수신호 {n_signals}개·평균점수 {avg_score}"]

        if main_idx_chg is not None:
            if main_idx_chg >= 1.0:
                reg_score += 2
            elif main_idx_chg >= 0.2:
                reg_score += 1
            elif main_idx_chg <= -1.0:
                reg_score -= 2
            elif main_idx_chg <= -0.2:
                reg_score -= 1
            if kospi_chg is not None:
                basis_parts.append(f"코스피 {kospi_chg:+g}%")
            if kosdaq_chg is not None:
                basis_parts.append(f"코스닥 {kosdaq_chg:+g}%")

        if n_signals >= 4:
            reg_score += 1
        elif n_signals == 0:
            reg_score -= 1

        if sp_chg is not None:
            if sp_chg >= 0.5:
                reg_score += 1
            elif sp_chg <= -0.5:
                reg_score -= 1
            basis_parts.append(f"전일 S&P {sp_chg:+g}%")
        if sox_chg is not None:
            basis_parts.append(f"SOX {sox_chg:+g}%")

        # ── 5. regime 결정 ───────────────────────────────────────
        # 코스피·코스닥 방향 엇갈림 → 혼조 우선 적용
        _both = kospi_chg is not None and kosdaq_chg is not None
        _split = _both and (
            (kospi_chg > 0.3 and kosdaq_chg < -0.3) or
            (kospi_chg < -0.3 and kosdaq_chg > 0.3)
        )
        if _split:
            regime = "혼조"
        elif reg_score >= 2:
            regime = "강세"
        elif reg_score <= -2:
            regime = "약세"
        elif abs(reg_score) <= 1:
            regime = "중립"
        elif reg_score > 1:
            regime = "강세"
        else:
            regime = "약세"

        # ── 6. 텍스트 생성 ──────────────────────────────────────
        theme_str = "·".join(top_themes) if top_themes else "전 업종"
        theme_note = (f"주도 테마({theme_str}) 위주 선별 진입."
                      if top_themes else "업종 편중 없음 — 종목 개별 신호 중심.")

        if regime == "강세":
            headline = (f"{theme_str} 주도 강세 — 기술신호 {n_signals}개·평균점수 {avg_score}. "
                        f"과열 구간 진입 시 비중 조절 유의.")
            buy_view = (
                f"지수 상승 흐름 — 점수 상위 신호의 돌파·눌림 진입 집중. {theme_note} "
                f"{'미국 우호환경(S&P ' + f'{sp_chg:+g}%)으로 외인 유입 기대.' if (sp_chg or 0) >= 0.5 else '거래량 동반 여부 반드시 확인.'}"
            )
            sell_view = (
                f"급등 후 상단 매물대·과열 구간 도달 시 일부 차익실현 검토. "
                f"48시간 보유캡 도달 종목 우선 정리. 손절가 이탈 시 재료 불문 기계적 손절."
            )
        elif regime == "약세":
            headline = (f"지수 약세 — 진입 기회 제한({n_signals}개 신호). "
                        f"손절 규율 최우선, 현금 비중 확대 검토.")
            buy_view = (
                f"신호 수 제한적 — 고점수·고확신 종목만 소량 진입(비중 절제). "
                f"{theme_note} 지수 낙폭 확대 시 신규 진입 전면 보류."
            )
            sell_view = (
                f"지수 하락 환경 — 보유 비중 축소 우선. "
                f"{'미 반도체 약세(SOX ' + f'{sox_chg:+g}%)로 반도체 비중 경계. ' if (sox_chg or 0) <= -1.0 else ''}"
                f"손절가 이탈 즉시 기계적 청산. 반등 시도도 매도 기회로 활용."
            )
        elif regime == "혼조":
            headline = (f"코스피·코스닥 방향 엇갈린 혼조 — {theme_str} 선별 진입. "
                        f"섹터 순환매 가능성, 방향 확인 후 진입.")
            buy_view = (
                f"강한 섹터(수급 유입·거래량 증가)에 한정 진입. {theme_note} "
                f"지수 방향 불일치 구간 — 단기 변동성 확대 대비 비중 절제."
            )
            sell_view = (
                f"방향성 부재 구간 — 목표가 도달 시 즉시 분할 익절. "
                f"48시간 보유캡 준수, 수익 확보 후 현금화 우선."
            )
        else:  # 중립
            headline = (f"지수 보합 중립 — 뚜렷한 방향성 부재. "
                        f"신호 {n_signals}개 내 고점수 종목만 선별 접근.")
            buy_view = (
                f"방향성 불명확 — 점수 상위 소수 종목에 집중(분산 금지). {theme_note} "
                f"거래량·분봉 확인 후 돌파 진입이 원칙."
            )
            sell_view = (
                f"보유 중 목표가·손절가 룰 엄격 적용. "
                f"수익권 진입 종목은 일부 차익실현 후 트레일링 적용 검토. "
                f"48시간 보유캡 이내 청산 원칙 유지."
            )

        basis = " · ".join(basis_parts) + " (실측 지표, 날조 없음)"

        return {
            "regime": regime,
            "headline": headline,
            "buy_view": buy_view,
            "sell_view": sell_view,
            "basis": basis,
            "asof": asof,
        }

    except Exception as ex:
        print(f"[analyze] market_outlook 생성 실패(건너뜀): {ex}")
        return None


SIGNALS_LOG_PATH = REPO_ROOT / "signals_log.json"


def append_signal_log(signals, now_iso, date_str, hold_cap):
    """발행 신호를 signals_log.json 에 누적(전향 추적용). 같은 (code,date)는 1회만.
    backtest.py 가 보유창 경과분을 평가한다. 최대 1000건 유지."""
    log = []
    if SIGNALS_LOG_PATH.exists():
        try:
            data = json.loads(SIGNALS_LOG_PATH.read_text(encoding="utf-8-sig"))
            log = data if isinstance(data, list) else data.get("signals", [])
        except Exception:
            log = []
    seen = {(e.get("code"), e.get("date")) for e in log}
    added = 0
    for s in signals:
        key = (s["code"], date_str)
        if key in seen:
            continue
        seen.add(key)
        log.append({
            "code": s["code"], "name": s["name"], "date": date_str,
            "issued_at": now_iso, "entry": s["entry"], "stop": s["stop"],
            "target1": s["target1"], "target2": s["target2"],
            "entry_type": s["entry_type"], "hold_cap_hours": hold_cap,
        })
        added += 1
    if added == 0:
        return
    if len(log) > 1000:
        log = log[-1000:]
    try:
        SIGNALS_LOG_PATH.write_text(
            json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[analyze] 신호 로그 +{added} (총 {len(log)})")
    except Exception as ex:
        print(f"[analyze] 신호 로그 저장 실패: {ex}")


CATALYST_FRESH_HOURS = 18  # 이 시간 이내의 뉴스 촉매는 차트 재분석에도 보존.


def reapply_fresh_catalyst(feed, prev_catalyst, now):
    """직전 feed 의 catalyst 블록이 신선(<CATALYST_FRESH_HOURS)하면 보존하고, 이번
    차트 신호에 다시 입힌다. 차트 엔진이 자주 돌며 뉴스(촉매)를 'catalyst_verified=false'
    로 덮어쓰는 것을 막는다(뉴스 분석은 가끔만 도므로). 신선하지 않으면 아무것도 안 함."""
    if not isinstance(prev_catalyst, dict):
        return 0
    asof = prev_catalyst.get("asof")
    try:
        dt = datetime.datetime.fromisoformat(str(asof))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        age_h = (now - dt).total_seconds() / 3600.0
    except Exception:
        return 0
    if age_h < 0 or age_h > CATALYST_FRESH_HOURS:
        return 0  # 오래됐으면 보존하지 않음(catalyst 블록은 새 feed 에서 빠진다).
    items = prev_catalyst.get("items", {}) if isinstance(
        prev_catalyst.get("items"), dict) else {}
    feed["catalyst"] = prev_catalyst  # 블록 유지.
    cnt = 0
    for s in feed.get("signals", []):
        info = items.get(str(s.get("code", "")).strip())
        if not info:
            continue
        headline = str(info.get("headline", "")).strip()
        detail = str(info.get("detail", "")).strip()
        if headline:
            s["catalyst"] = (headline + (" — " + detail if detail else "")).strip()
        if info.get("sources"):
            s["catalyst_verified"] = True
            cnt += 1
            s["evidence"] = (f"촉매: {headline}. " + s.get("evidence", "")).strip()
            tags = [t for t in s.get("tags", []) if "미검증" not in t]
            if "촉매검증" not in tags:
                tags = ["촉매검증"] + tags
            s["tags"] = tags
    if cnt:
        ah = int(round(age_h))
        feed["data_source"] = feed.get("data_source", "") + \
            f" (뉴스/촉매 {ah}h 전 반영 보존)"
        rn = feed.get("risk_notes", [])
        rn = [x for x in rn if "catalyst_verified=false" not in x
              and "뉴스/촉매 미반영" not in x]
        feed["risk_notes"] = [
            f"뉴스/촉매 분석 결과를 보존 반영(약 {ah}시간 전). 시점에 따라 정정될 수 있으니 진입 전 원문 확인."
        ] + rn
    return cnt


# 고점추격(과열) 기본 설정 — control.json engine.overheat 로 덮어쓸 수 있다.
# gate_enabled=True 면 disparity_max/mom5_max 초과 종목을 신호에서 배제(눌림 대기).
# tag_* 임계는 게이트보다 낮아, 게이트 OFF 여도 '추격주의' 태그(경고)는 항상 붙는다.
DEFAULT_OVERHEAT = {
    "gate_enabled": False, "disparity_max": 18.0, "mom5_max": 22.0,
    "tag_disparity": 10.0, "tag_mom5": 12.0,
}


def overheat_state(ind, oh):
    """(gate_block, tag_chase) — 이미 계산된 MA20 이격·5일 모멘텀만 사용.
    gate_block: 게이트(배제) 임계 초과. tag_chase: '추격주의' 태그 임계 초과."""
    disp = ind.get("ma20_disparity", 0) or 0
    mom5 = ind.get("mom5", 0) or 0
    block = (disp >= oh["disparity_max"]) or (mom5 >= oh["mom5_max"])
    chase = (disp >= oh["tag_disparity"]) or (mom5 >= oh["tag_mom5"])
    return block, chase


def load_control():
    """control.json 파싱. 반환: (watchlist, scope, market_targets, hold_cap, tuning,
    weights, overheat).
    - tuning: engine.tuning(없으면 DEFAULT_TUNING) — 손절·목표 배수 학습 보정.
    - weights: engine.learned_weights(없으면 None=DEFAULT_WEIGHTS) — 점수 가중치 학습 보정.
    - overheat: engine.overheat(없으면 DEFAULT_OVERHEAT) — 고점추격 게이트·태그 임계."""
    wl, scope, mkts, cap = [], "watchlist", ["KOSPI", "KOSDAQ"], 48
    tuning = dict(DEFAULT_TUNING)
    weights = None
    overheat = dict(DEFAULT_OVERHEAT)
    if CONTROL_PATH.exists():
        try:
            c = json.loads(CONTROL_PATH.read_text(encoding="utf-8-sig"))
            e = c.get("engine", {})
            scope = e.get("analysis_scope", scope)
            mkts = e.get("market_targets", mkts) or mkts
            cap = int(e.get("hold_cap_hours", cap) or cap)
            wl = c.get("watchlist", []) or []
            t = e.get("tuning")
            if isinstance(t, dict):
                for k in DEFAULT_TUNING:
                    if k in t and isinstance(t[k], (int, float)):
                        tuning[k] = float(t[k])
                # 점수구간별 차등 배수(by_bucket: {'55-64':{stop_mult,..}, ...}).
                bb = t.get("by_bucket")
                if isinstance(bb, dict):
                    parsed = {}
                    for bk, mv in bb.items():
                        if isinstance(mv, dict):
                            d = {k: float(mv[k]) for k in
                                 ("stop_mult", "target1_mult", "target2_mult")
                                 if k in mv and isinstance(mv[k], (int, float))}
                            if d:
                                parsed[str(bk)] = d
                    if parsed:
                        tuning["by_bucket"] = parsed
            lw = e.get("learned_weights")
            if isinstance(lw, dict):
                parsed = {k: float(lw[k]) for k in DEFAULT_WEIGHTS
                          if k in lw and isinstance(lw[k], (int, float))}
                # ★부호 가드(P0-1)★ 약한 AUC로 과적합된 학습이 단타 모멘텀을 거꾸로
                # 잡는 사고 방지(align_up −8·vol_surge −9·breakout −8·for_sell +10 등).
                # 단타에서 부호가 명백한 키만 검사 — 위반하면 학습세트 전체 거부→DEFAULT.
                viol = [k for k in _W_POS if k in parsed and parsed[k] < 0] + \
                       [k for k in _W_NEG if k in parsed and parsed[k] > 0]
                if viol:
                    print(f"[analyze] ⚠️ 학습 가중치 부호 비정상 {viol} — "
                          f"학습세트 거부, DEFAULT 사용")
                elif parsed:
                    weights = {**DEFAULT_WEIGHTS, **parsed}
            # 고점추격(과열) 토글 — bool/숫자만 받아 덮어쓴다(나머지 기본값 유지).
            oh = e.get("overheat")
            if isinstance(oh, dict):
                for k in DEFAULT_OVERHEAT:
                    if k == "gate_enabled":
                        if isinstance(oh.get(k), bool):
                            overheat[k] = oh[k]
                    elif isinstance(oh.get(k), (int, float)):
                        overheat[k] = float(oh[k])
        except Exception as ex:
            print(f"[analyze] control 읽기 실패: {ex}")
    return wl, scope, mkts, cap, tuning, weights, overheat


def yahoo_symbol(code):
    """KR 6자리 코드 → 야후 심볼(.KS 우선, 실패 시 .KQ는 호출측에서 시도)."""
    return f"{code}.KS"


def score_watchlist(watchlist, regime, cutoff, sf_map=None, weights=None,
                    combo_table=None):
    """관심종목을 KIS 현재가(있으면 정밀)+yfinance 일봉으로 점수화한다.
    KIS 토큰 발급/조회 실패해도 중단하지 않고 yfinance 로 폴백한다.
    sf_map(코드→수급·재무)이 있으면 점수에 수급·재무를 추가 반영한다.
    weights(학습된 가중치)가 있으면 차트 점수에 적용한다.
    combo_table(조합별 과거 승률)이 있으면 신호 컷오프·정렬 점수에 소프트 보정한다.
    반환: [(item, price, change, ind, score), ...]."""
    sf_map = sf_map or {}
    combo_table = combo_table or {}
    out = []
    cfg = _kis_cfg()
    token = None
    if cfg:
        try:
            token = _kis_token(cfg)
        except Exception as ex:
            print(f"[analyze] KIS 토큰 실패 — yfinance 일봉으로 폴백: {ex}")
            token = None
    for item in watchlist:
        code = item.get("code", "")
        if not code:
            continue
        price, change = (None, None)
        if cfg and token:
            price, change = kis_quote(code, cfg, token, "UN")
        ind = daily_indicators(code + ".KS") or daily_indicators(code + ".KQ")
        if price is None and ind is not None:
            price = ind["yf_close"]
        if price is None or ind is None:
            print(f"[analyze] 시세/지표 미확보 제외: {item.get('name', code)}")
            continue
        sc, why, bd = score_stock(price, ind, weights)
        sc = apply_regime(sc, why, bd, regime)
        sc = apply_supply_finance(sc, why, bd, sf_map.get(code))
        ind["_why"] = why
        ind["_breakdown"] = bd
        ind["_sf"] = sf_map.get(code)  # 수급(외국인·기관) — feed 출력·앱 표시용.
        sc = apply_combo(sc, ind, combo_table)  # 조합별 과거 승률 소프트 보정(±4).
        out.append((item, price, change, ind, sc))
    return out


def score_universe(uni, regime, sf_map=None, weights=None, combo_table=None):
    """전체종목 유니버스를 yfinance 배치로 점수화한다(현재가=일봉 종가, 속도·정합성).
    sf_map(코드→수급·재무)이 있으면 점수에 수급·재무를 추가 반영한다.
    weights(학습된 가중치)가 있으면 차트 점수에 적용한다.
    combo_table(조합별 과거 승률)이 있으면 신호 컷오프·정렬 점수에 소프트 보정한다.
    반환: [(item, price, change, ind, score), ...]."""
    sf_map = sf_map or {}
    combo_table = combo_table or {}
    out = []
    if not uni:
        return out
    sym_for = {t["code"]: t["code"] + (".KQ" if t.get("_mk") == "KOSDAQ" else ".KS")
               for t in uni}
    inds = daily_indicators_batch(list(sym_for.values()))
    for t in uni:
        code = t["code"]
        ind = inds.get(sym_for[code])
        if ind is None and not t.get("_mk"):
            ind = daily_indicators(code + ".KS") or daily_indicators(code + ".KQ")
        if ind is None:
            continue
        price = ind["yf_close"]
        sc, why, bd = score_stock(price, ind, weights)
        sc = apply_regime(sc, why, bd, regime)
        sc = apply_supply_finance(sc, why, bd, sf_map.get(code))
        ind["_why"] = why
        ind["_breakdown"] = bd
        ind["_sf"] = sf_map.get(code)  # 수급(외국인·기관) — feed 출력·앱 표시용.
        sc = apply_combo(sc, ind, combo_table)  # 조합별 과거 승률 소프트 보정(±4).
        out.append((t, price, ind.get("change"), ind, sc))
    return out


def _sign_cr(cr, rf):
    """네이버 rf(등락 방향코드)로 등락률 부호 확정. 1=상한·2=상승 → +, 5=하락·
    4=하한 → −, 3=보합 → 0. 그 외/미상은 원값 유지. 마감 후 cr 이 부호 없이
    크기만 오는 케이스(방향 뒤집힘)를 막는다(INC-002)."""
    mag = abs(cr)
    rf = str(rf).strip()
    if rf in ("1", "2"):
        return mag
    if rf in ("4", "5"):
        return -mag
    if rf == "3":
        return 0.0
    return cr


def fetch_kr_changes_naver(codes):
    """KR 6자리 코드들의 전일대비 등락률(%)을 네이버 polling 으로 **배치** 조회한다.

    ★정합성(INC-002·003)★ 등락률 부호는 `cr`(마감 후 크기만 옴) 이 아니라 `rf` 로
    확정한다. 이 값은 KIS 미확보(0.0 표기) 와 yfinance 일봉 직접차분(방향 뒤집힘)을
    동시에 대체하는 **권위 출처**다(제공처가 계산한 전일대비). 실패 코드는 생략.
    반환: {code: change_pct(float)}.
    """
    import urllib.request
    import json as _json
    clean = [c for c in {str(c).strip() for c in codes}
             if c.isdigit() and len(c) == 6]
    if not clean:
        return {}
    out = {}
    for i in range(0, len(clean), 50):  # 묶음 50개씩
        chunk = clean[i:i + 50]
        url = ("https://polling.finance.naver.com/api/realtime"
               "?query=SERVICE_ITEM:" + ",".join(chunk))
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://m.stock.naver.com/"})
            raw = urllib.request.urlopen(req, timeout=8).read()
            d = None
            for enc in ("utf-8", "cp949"):
                try:
                    d = _json.loads(raw.decode(enc))
                    break
                except Exception:
                    continue
            if not d:
                continue
            areas = ((d.get("result") or {}).get("areas") or [{}])
            for it in (areas[0].get("datas") if areas else []) or []:
                cd = it.get("cd")
                cr = it.get("cr")
                if cd and cr is not None:
                    try:
                        out[cd] = round(_sign_cr(float(cr), it.get("rf")), 2)
                    except (TypeError, ValueError):
                        pass
        except Exception as ex:
            print(f"[analyze] 네이버 등락률 배치 실패({chunk[:3]}…): {ex}")
            continue
    return out


def _tentative_plan(price, ind, hold_cap, tuning, score, cutoff):
    """관찰종목용 '잠정(관찰) 매매계획' — 신호와 동일한 매매계획 코어(levels +
    position_weight + 구간별 배수)를 재사용해 진입·손절·목표1/2·RR·비중을 산출한다.
    실측 price·ATR 기반(날조 없음). 신호로 채택되지 않은 후보이므로 plan_tentative=True
    로 표식해 앱이 '잠정'으로 구분한다. ind/price 가 비정상이면 빈 dict."""
    try:
        sm, t1m, t2m = effective_mults(tuning, score)
        lv = levels(price, ind, hold_cap, sm, t1m, t2m)
        entry, stop, risk = lv["entry"], lv["stop"], lv["risk"]
        target1, target2 = lv["target1"], lv["target2"]
        rr = round((target1 - entry) / risk, 2) if risk > 0 else 0
        t = {**DEFAULT_TUNING, **(tuning or {})}
        weight, stop_pct, est_loss = position_weight(entry, stop, score, cutoff, t)
        if lv["etype"] == "breakout":
            weight = int(max(t["min_weight_pct"], round(weight * 0.85)))
            est_loss = round(weight * stop_pct / 100, 2)
        return {
            "plan_tentative": True,        # ★잠정(관찰) 표식 — 앱이 신호와 구분.
            "entry": float(entry), "entry_type": lv["etype"],
            "stop": stop, "target1": target1, "target2": target2,
            "rr": rr, "atr_pct": ind["atr_pct"],
            "weight_pct": weight, "stop_pct": stop_pct, "est_loss_pct": est_loss,
            "hold_cap_hours": hold_cap,
            "reachable": lv.get("reachable", True),
        }
    except Exception:
        return {}


def _observation(item, price, change, ind, sc, note=None,
                 hold_cap=48, tuning=None, cutoff=55):
    """관찰(observations) 항목 dict — 신호와 같은 표시 필드 + 잠정 매매계획.
    note 가 있으면 강등 사유(예: 도달성 낮음)를 reason 앞에 붙인다.
    hold_cap/tuning/cutoff 로 신호와 동일 방식의 잠정 진입·손절·목표를 채운다."""
    why = ", ".join(ind["_why"]) if ind.get("_why") else "뚜렷한 강세 신호 부족"
    head = f"기술 점수 {sc}점 — {note}" if note else f"기술 점수 {sc}점 — 조건 미충족(관망)"
    obs = {
        "name": item["name"], "code": item["code"], "market": "KR",
        "price": float(price), "currency": "KRW", "watch_trigger": None,
        "change_pct": round(change, 2) if change is not None else None,
        "vol_surge": ind["vol_surge"],
        # ★관찰에도 점수 노출(앱 '분석 전' 오표기 방지) — sc 는 이미 계산된 점수.
        # 신호와 대칭으로 score·score_reasons 둘 다 실어 앱이 배지·근거를 표시한다.
        "score": sc,
        "score_reasons": list(ind.get("_breakdown", [])),
        **_supply_fields(ind.get("_sf")),
        **_combo_fields(ind),
        "reason": f"{head}. {why}. ATR {ind['atr_pct']}%.",
        # 5분봉 시계열(앱 '최근 24시간 차트'용, 관찰 종목도 포함).
        "intraday_5m": ind.get("intraday_5m", []),
        # 최근 7거래일 일봉(앱 신호카드 '1주일 차트'용).
        "daily_7d": ind.get("daily_7d", []),
        # 최근 ~1달(약 22거래일) 일봉. 앱 미니차트가 daily_7d 보다 우선 사용.
        "daily_30d": ind.get("daily_30d", []),
        # 최근 ~3달(약 65거래일) 일봉. 앱이 '3달 차트'에 사용할 경우 이 값을 우선.
        "daily_90d": ind.get("daily_90d", []),
    }
    # ★잠정(관찰) 매매계획★ — 신호와 동일 코어로 진입/손절/목표/RR/비중 채움.
    obs.update(_tentative_plan(price, ind, hold_cap, tuning, sc, cutoff))
    return obs


def build_watch_advice(signals, observations, regime, krc_session, universe_failed):
    """신호탭 최상단 '관망 권고' 배너용 구조화 advice를 분석 결과로 생성한다.
    앱이 이모지·항목으로 풍부히 렌더할 수 있게 구조(level/headline/bullets/action)를 준다.
    날조 없음 — 모두 이번 분석의 실측 집계(신호 수·상위 점수·시장환경)에서 도출."""
    n_sig = len(signals)
    top = signals[0] if signals else None
    top_score = top.get("_score") if top else None
    regime_pct = (regime or {}).get("regime_pct")
    bullets = []
    if n_sig == 0:
        level = "observe"
        headline = "발굴된 매수 신호가 없습니다 — 관망 권고"
        bullets.append("기술 조건을 통과한 종목이 없어 무리한 진입보다 대기를 권합니다.")
    elif top_score is not None and top_score < 60:
        level = "caution"
        headline = f"신호 {n_sig}건이나 확신도 낮음 — 신중 접근"
        bullets.append(f"최상위 점수 {top_score}점으로 약한 편 — 분할·소액부터 검토.")
    else:
        level = "normal"
        headline = f"매수 신호 {n_sig}건 — 상위 후보 {top.get('name')}({top_score}점)"
        bullets.append(f"최상위 {top.get('name')} {top_score}점 · 진입 전 재료·시초가 갭 확인.")
    if regime_pct is not None:
        if regime_pct <= -0.8:
            bullets.append(f"전일 미국증시 약세({regime_pct:+g}%) — 위험회피 국면, 비중 보수적으로.")
        elif regime_pct >= 0.8:
            bullets.append(f"전일 미국증시 강세({regime_pct:+g}%) — 우호적이나 추격매수 경계.")
        else:
            bullets.append(f"전일 미국증시 보합({regime_pct:+g}%) — 종목 선별 중심.")
    if krc_session in ("pre", "after"):
        bullets.append("현재 NXT 연장(장전/장후) 시간 — 유동성 얇음, 호가 슬리피지 유의.")
    if universe_failed:
        bullets.append("전체종목 목록 일시 미확보 — 이번 회차는 관심종목 위주입니다.")
    bullets.append("공통 유의: 뉴스/촉매 미반영(기술 신호만). 실주문은 본인 판단·실행.")
    action = {
        "observe": "관망 — 신규 진입 보류",
        "caution": "신중 — 소액·분할 진입 검토",
        "normal": "선별 진입 — 상위 후보 우선 검토",
    }[level]
    return {
        "level": level,
        "headline": headline,
        "bullets": bullets,
        "action": action,
        "signal_count": n_sig,
        "top_signal": (top.get("name") if top else None),
        "top_score": top_score,
    }


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    now = datetime.datetime.now(KST)
    now_iso = now.replace(microsecond=0, second=0).isoformat()
    watchlist, _scope, mkts, hold_cap, tuning, weights, overheat = load_control()
    cutoff = int(tuning.get("score_cutoff", 55))
    if tuning != DEFAULT_TUNING:
        print(f"[analyze] 손절·목표 학습 보정 적용: {tuning}")
    if weights:
        print(f"[analyze] 학습된 점수 가중치 적용: {weights}")

    # 조합별 과거 승률 lookup 테이블(stats.json) — 신호/관찰 표시 + 소프트 랭킹 보정용.
    combo_table = load_combo_table()
    if combo_table:
        print(f"[analyze] 조합 승률 테이블 로드: {len(combo_table)}개 조합(±{COMBO_ADJ_CAP} 보정)")

    # 시장 환경(미국 전일) 보정치 — 1회 계산해 모든 종목 점수에 동일 적용.
    regime = fetch_us_regime()
    if regime:
        print(f"[analyze] 시장환경: 전일 미국 가중 {regime['regime_pct']:+g}% → "
              f"보정 {regime['adj']:+g} (S&P{regime.get('sp', 0):+g}·"
              f"나스닥{regime.get('nasdaq', 0):+g}·SOX{regime.get('sox', 0):+g})")

    # ── 통합 분석: 관심종목 + 전체종목을 **항상 함께** 분석한다 ──────────────────
    # 관심종목은 KIS 현재가(있으면 정밀)+yfinance 일봉으로, 전체종목은 네이버 거래대금
    # 상위 유니버스(market_targets 범위)를 yfinance 배치로 점수화한다. 두 결과를
    # group 태그('watchlist'|'market')로 구분해 한 feed 의 signals 에 함께 싣는다.
    wl_codes = {str(w.get("code", "")).strip() for w in watchlist if w.get("code")}

    universe_failed = False
    uni = build_universe(mkts)
    uni = [u for u in uni if u.get("code") not in wl_codes]
    if not uni:
        universe_failed = True
        print("[analyze] 전체종목 유니버스 미확보 — 이번 회차는 관심종목만.")

    # 수급(외국인·기관)·재무(PER/PBR) 1회 배치 수집 — 관심종목 + 유니버스 전체.
    sf_codes = list(wl_codes) + [u["code"] for u in uni]
    sf_map = fetch_supply_finance_batch(sf_codes)

    wl_cand = score_watchlist(watchlist, regime, cutoff, sf_map, weights, combo_table)
    mk_cand = score_universe(uni, regime, sf_map, weights, combo_table)
    print(f"[analyze] 통합 분석: 관심종목 후보 {len(wl_cand)} / 전체종목 후보 "
          f"{len(mk_cand)} (markets={mkts})")

    if not wl_cand and not mk_cand:
        print("[analyze] 분석 가능한 종목 없음 — feed 미변경")
        return 0

    # ★정합성(INC-002·003)★ KR 종목 등락률은 네이버(cr+rf, 제공처 전일대비)로 일괄
    # 덮어쓴다 — 관심종목 KIS 0.0 누락과 전체종목 yfinance 일봉차분(방향 뒤집힘)을
    # 동시에 제거한다. 실패분은 기존값(KIS/yfinance) 유지. US 티커는 맵에 없어 무영향.
    kr_codes = [c.get("code", "") for c, *_ in wl_cand] + \
               [c.get("code", "") for c, *_ in mk_cand]
    kr_chg_map = fetch_kr_changes_naver(kr_codes)
    if kr_chg_map:
        print(f"[analyze] KR 등락률 네이버 보정(rf 부호): {len(kr_chg_map)}종목")

    # ★프리마켓 0.00 오표기 방지(2026-07-03)★ 네이버 SERVICE_ITEM 은 KRX 정규장
    # (09:00) 개장 전엔 라이브 틱이 없어 전일종가를 그대로 돌려주며 cr=0 이 고정
    # 출력된다 — 관측 결과 08:00~09:00 프리마켓 시간대에 관심종목 30개 전부가
    # 동시에 change_pct=0.00 으로 기록되는 사고가 있었다(실측 '변동없음'이 아니라
    # '아직 데이터 없음'을 0 으로 날조한 것). 08:00~09:00(KST, 평일)엔 네이버 값이
    # 정확히 0.0 이면 무조건 신뢰하지 않는다: KIS UN(NXT) 등 실측 대체값이 있으면
    # 그 값을 쓰고, 없으면 null(미확보)로 발행한다. 정규장 개장 후(09:00~)엔 실제
    # 보합(0.00)도 있을 수 있으므로 이 가드를 적용하지 않는다.
    _premkt = now.weekday() < 5 and (8 * 60) <= (now.hour * 60 + now.minute) < (9 * 60)

    # ★KR 등락률 이상치 게이트(P1-신호품질·시세 정합성)★ 네이버 권위값으로 덮어쓴
    # 등락률도 ±45% 초과면 데이터 오류로 보고 None 처리(change_pct=null 로 발행).
    # 방향 뒤집힘 사고는 이미 rf 부호로 차단됐지만, 크기 이상치는 별도 게이트 필요.
    def _kr_chg_gated(code, fallback):
        v = kr_chg_map.get(code, fallback)
        if _premkt and v == 0.0:
            if fallback is not None and fallback != 0.0:
                return fallback  # KIS UN 등 실측 프리마켓 값 보존(네이버 고정값이 덮어쓰지 않게)
            print(f"[analyze] KR {code} 프리마켓 네이버 미개장 고정값(0.00) — 미확보 처리(null)")
            return None
        if v is not None and abs(v) > 45.0:
            print(f"[analyze] ⚠️ KR {code} 등락률 이상치 {v:+.2f}% (>45%) — 폐기(null)")
            return None
        return v

    # 관심종목 신호(점수순 최대 5) + 관찰(미충족 watchlist).
    obs_cap = 40
    wl_cand.sort(key=lambda x: x[4], reverse=True)
    wl_signals, observations = [], []
    rank = 0
    for item, price, change, ind, sc in wl_cand:
        change = _kr_chg_gated(item.get("code", ""), change)  # 네이버 권위값 + 이상치 게이트
        if sc >= cutoff and rank < 5:
            block, chase = overheat_state(ind, overheat)
            # 고점추격 게이트(기본 OFF) — 과열 심하면 신호 제외, '눌림 대기' 관찰로 강등.
            if overheat["gate_enabled"] and block:
                if len(observations) < obs_cap:
                    observations.append(_observation(item, price, change, ind, sc,
                        note=f"단기 과열(MA20 이격 {ind.get('ma20_disparity')}%·5일 {ind.get('mom5')}%) — 눌림 대기",
                        hold_cap=hold_cap, tuning=tuning, cutoff=cutoff))
                continue
            sig = build_signal(rank + 1, item, price, change, ind, hold_cap,
                               tuning, score=sc, cutoff=cutoff, chase=chase)
            if sig.get("reachable", True):
                rank += 1
                sig["_score"] = sc
                sig["group"] = "watchlist"
                wl_signals.append(sig)
            elif len(observations) < obs_cap:
                # ★도달성 강등(P0-3)★ 돌파 진입가가 현재가 +2.5% 초과 → 관찰로.
                observations.append(_observation(item, price, change, ind, sc,
                    note="돌파 진입가가 현재가 +2.5% 초과 — 도달성 낮아 관찰",
                    hold_cap=hold_cap, tuning=tuning, cutoff=cutoff))
        elif len(observations) < obs_cap:
            observations.append(_observation(item, price, change, ind, sc,
                hold_cap=hold_cap, tuning=tuning, cutoff=cutoff))

    # 전체종목 신호(점수순 최대 30·기준 50점, 관심종목 제외). 비신호는 관찰에 넣지 않는다(후보 수백).
    mk_cand.sort(key=lambda x: x[4], reverse=True)
    market_signals = []
    mrank = 0
    for item, price, change, ind, sc in mk_cand:
        change = _kr_chg_gated(item.get("code", ""), change)  # 네이버 권위값 + 이상치 게이트
        if sc >= 50 and mrank < 30:  # 전체종목은 50점(관심종목 55보다 완화)
            block, chase = overheat_state(ind, overheat)
            # 고점추격 게이트(기본 OFF) — 과열 종목 신호 제외(전체종목은 관찰 누적 안 함).
            if overheat["gate_enabled"] and block:
                continue
            sig = build_signal(mrank + 1, item, price, change, ind, hold_cap,
                               tuning, score=sc, cutoff=cutoff, chase=chase)
            if not sig.get("reachable", True):
                continue  # 도달성 낮음(돌파가 +2.5% 초과) → 신호 제외(P0-3).
            mrank += 1
            sig["_score"] = sc
            sig["group"] = "market"
            market_signals.append(sig)

    # feed 에는 두 그룹을 합쳐 싣되 group 태그로 구분(앱이 섹션으로 나눠 표시).
    signals = wl_signals + market_signals

    # 기존 feed 보존 항목(us_context·kr_context·positions·portfolio·assumptions).
    feed = {}
    if FEED_PATH.exists():
        try:
            feed = json.loads(FEED_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            feed = {}
    # 신선한 뉴스 촉매 블록은 차트 재작성에도 보존한다(아래 reapply).
    prev_catalyst = feed.get("catalyst")
    feed.pop("catalyst", None)  # 일단 제거 후, 신선하면 reapply 가 되살린다.

    top = signals[0]["name"] if signals else (observations[0]["name"] if observations else None)
    # 관망 권고(구조화) — 신호탭 최상단 배너용. signals 에 _score 가 살아있을 때 계산.
    _krc_session = (feed.get("kr_context") or {}).get("session")
    watch_advice = build_watch_advice(signals, observations, regime,
                                      _krc_session, universe_failed)
    # 이번 분석이 실제로 본 범위(통합: 관심종목 + 전체종목 거래대금 상위).
    if universe_failed:
        scan_label = "통합 분석 — 관심종목 + 전체종목(시장 목록 확보 실패로 이번엔 전체종목 생략)"
    else:
        scan_label = (f"통합 분석 — 관심종목({len(wl_signals)} 신호) + "
                      f"전체종목 거래대금 상위({'·'.join(mkts)}, {len(market_signals)} 신호)")
    regime_note = None
    if regime:
        regime_note = (f"시장 환경 보정: 전일 미국증시 가중 {regime['regime_pct']:+g}% "
                       f"(S&P {regime.get('sp', 0):+g}%·나스닥 {regime.get('nasdaq', 0):+g}%·"
                       f"SOX {regime.get('sox', 0):+g}%) → 모든 종목 점수 {regime['adj']:+g}점 반영. "
                       f"금리·지정학은 숫자 보정 없이 뉴스 종합분석의 정성 판단으로만 반영.")
    feed.update({
        "schema_version": "1.0",
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now_iso,
        # analyzed_at = 신호를 새로 산출한 '전체(예약) 분석' 시각. generated_at 은
        # 장중 시세갱신(intraday_refresh)마다 now 로 덮어써지므로, 앱의 '예약분석 경과'
        # 표기는 이 필드를 쓴다(intraday_refresh 가 보존). 분석 직후엔 둘이 같다.
        "analyzed_at": now_iso,
        "horizon_hours": hold_cap,
        "data_source": f"온디맨드 기술 분석(KIS 현재가 + yfinance 일봉 지표) + 수급(외국인·기관) + 재무(PER·PBR) + 미국증시 전일 환경 보정 · 분석 범위: {scan_label}. 뉴스/촉매는 ‘뉴스도 함께’에서만.",
        "market_state": feed.get("market_state", {"korea": {"status": "closed"}, "us": {"status": "closed"}}),
        # 시장 환경 보정(측정 가능한 미국증시만) — 앱이 방법론·투명성 표기에 쓸 수 있다.
        "market_regime": regime,
        # 관망/위험회피 권고(구조화) — 앱이 이모지·항목으로 풍부히 렌더(E).
        "watch_advice": watch_advice,
        "summary": {
            "signal_count": len(signals), "observation_count": len(observations),
            "position_count": len(feed.get("positions", [])),
            "top_signal": top,
            "headline": f"기술 분석 갱신({now.strftime('%m-%d %H:%M')} KST) — 차트·거래량·변동성 기준 "
                        f"{len(signals)}개 신호. 뉴스 미반영이니 진입 전 재료 확인.",
        },
        "signals": [{**{k: v for k, v in s.items()
                        if k not in ("_score", "reachable")},
                     "score": s["_score"]} for s in signals],
        "observations": observations,
        "risk_notes": ([
            "⚠️ 전체종목(시장) 목록 확보에 실패해 이번 분석은 관심종목만 담았습니다(네트워크 일시 오류). 잠시 후 다시 분석하면 전체종목 상위도 함께 나옵니다.",
        ] if universe_failed else []) + [
            "온디맨드 기술 분석 — 뉴스/촉매 미반영(catalyst_verified=false). 진입 전 재료·공시 직접 확인.",
            "지표는 KIS 현재가 + yfinance 일봉 실측(날조 없음). 변동성 장세 보수적 대응.",
            regime_note or "시장 환경 보정 미적용(미국증시 데이터 일시 미확보).",
            "본 신호는 투자 참고용이며 매수·매도를 보장하지 않는다. 실주문은 본인 판단·실행.",
        ],
        "disclaimer": feed.get("disclaimer",
                               "본 산출물은 투자 참고용이며 매수·매도를 보장하지 않습니다. 실주문은 사용자가 직접 판단·실행합니다."),
    })
    # 미국 야간 컨텍스트(지수·빅테크·한줄평) 실측 갱신 — 매 실행 새로고침해 옛 날짜 고정 해소.
    usc = fetch_us_context()
    if usc:
        feed["us_context"] = usc
        print(f"[analyze] 미국 컨텍스트 갱신: {usc['summary']}")
    # ★정합성 self-heal★ 한국 지수도 매 analyze 실행마다 네이버로 갱신한다 —
    # intraday-refresh cron(GitHub Actions)이 장중에 스킵돼 코스피가 어제값에
    # 멈추던 문제 방지(2026-06-17 코스피 8545(+5.2%) stale 사고). 부호는 rf 로 확정.
    krc = fetch_kr_context_naver()
    if krc:
        # market_state.korea self-heal 분 — kr_context 본문에는 싣지 않는다.
        ms_korea = krc.pop("_market_state_korea", None)
        old = feed.get("kr_context") or {}
        old.update(krc)  # 다른 필드 보존, 지수·asof·session 만 갱신.
        feed["kr_context"] = old
        # ★정합성 self-heal★ market_state.korea(status/basis/asof)도 방금 실측한
        # 신선값으로 갱신 — intraday_refresh cron 스킵으로 며칠 멈추던 사고 방지.
        if ms_korea:
            ms = feed.setdefault("market_state", {})
            ms_k = ms.setdefault("korea", {})
            ms_k.update(ms_korea)
        print(f"[analyze] KR 지수 갱신: "
              f"{[(i['name'], i['change_pct']) for i in krc['indices']]}")

    # ── 오늘의 증시 전망(market_outlook) 자동 생성 ───────────────────────────
    # 실측 지표(한국 지수·신호 분포·미국 야간)만으로 regime 판정. 날조 없음.
    # 미산출(데이터 부족·오류)이면 market_outlook 키를 넣지 않는다(앱 null 미표시).
    # usc 는 fetch_us_context() 반환값(None 이면 함수 내부에서 빈 dict 처리).
    outlook = generate_market_outlook(signals, feed.get("kr_context"), usc)
    if outlook:
        feed["summary"]["market_outlook"] = outlook
        # 면책 한 줄 — risk_notes 에 1회만 추가(중복 방지).
        _outlook_disc = "이 증시 전망은 과거·현재 지표 기반이며 미래를 보장하지 않습니다(참고용)."
        rn = feed.get("risk_notes", [])
        if _outlook_disc not in rn:
            feed["risk_notes"] = rn + [_outlook_disc]
        print(f"[analyze] market_outlook 생성: regime={outlook['regime']} "
              f"(신호 {len(signals)}개, basis={outlook['basis'][:50]}...)")

    feed.setdefault("positions", feed.get("positions", []))
    feed.setdefault("portfolio", feed.get("portfolio", {"total_unrealized": 0, "count": 0, "to_close": 0}))
    feed.setdefault("assumptions", feed.get("assumptions", {"fee_pct": 0.015, "tax_pct_kr": 0.18, "tax_pct_us": 0.0}))

    # 신선한 뉴스 촉매가 있으면 이번 차트 신호에 다시 입힌다(뉴스 덮어쓰기 방지).
    kept = reapply_fresh_catalyst(feed, prev_catalyst, now)
    if kept:
        print(f"[analyze] 뉴스 촉매 보존 반영: {kept}건")

    # ★검증 자동화(상시 게이트)★ 발행 직전 시세 정합성 검사 — '수동 검증'을
    # 매 분석마다 자동 실행되게 코드화(INC-001~005). 경고는 feed['integrity']에
    # 기록해 앱·로그가 보고, 콘솔에 ⚠️ 출력한다(차단보다 표기 우선 — 빈칸 금지 원칙).
    try:
        from verify_quotes import verify_feed
        chk = verify_feed(feed, now=now)
        feed["integrity"] = {"checked_at": now_iso,
                             "critical": chk["critical"], "warn": chk["warn"]}
        for c in chk["critical"]:
            print(f"[analyze] ❌ 정합성 critical: {c}")
        for w in chk["warn"]:
            print(f"[analyze] ⚠️ 정합성 warn: {w}")
    except Exception as ex:
        print(f"[analyze] 정합성 검증 스킵(오류): {ex}")

    # ★직렬화 직전 정제★ — 지표 계산 과정에서 yfinance nan/inf 가 살아남아
    # allow_nan=False 로 직렬화가 통째로 터지는 사고(2026-06-25 INC) 방어.
    # nan="값 없음"=null 변환(날조 없음). _calc_indicators nan 방어와 이중 안전망.
    feed = _json_safe(feed)
    FEED_PATH.write_text(json.dumps(feed, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n", encoding="utf-8")
    # 전향 추적용 — 이번 발행 신호를 로그에 누적(통계 탭 forward 평가용).
    append_signal_log(feed["signals"], now_iso, now.strftime("%Y-%m-%d"), hold_cap)
    print(f"[analyze] 완료 @ {now_iso} — 신호 {len(signals)} "
          f"(관심 {len(wl_signals)}+전체 {len(market_signals)}) / 관찰 {len(observations)} "
          f"(top {top})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
