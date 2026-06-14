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
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = REPO_ROOT / "feed.json"
CONTROL_PATH = REPO_ROOT / "control.json"
KIS_BASE = "https://openapi.koreainvestment.com:9443"
KIS_TOKEN_PATH = REPO_ROOT / "config" / ".kis_token.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


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
    """(현재가, 전일대비율) 또는 (None, None)."""
    import time
    time.sleep(0.25)
    params = urllib.parse.urlencode({"FID_COND_MRKT_DIV_CODE": mrkt, "FID_INPUT_ISCD": code})
    req = urllib.request.Request(
        f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price?{params}",
        headers={"Authorization": f"Bearer {token}", "appkey": cfg["app_key"],
                 "appsecret": cfg["app_secret"], "tr_id": "FHKST01010100", "custtype": "P"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            o = json.load(r).get("output", {})
        p = str(o.get("stck_prpr", "")).replace(",", "")
        c = str(o.get("prdy_ctrt", "")).replace(",", "")
        return (float(p) if p else None, float(c) if c not in ("", "None") else None)
    except Exception:
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
    """OHLCV 시계열(과거→현재)로 기술 지표 dict. 데이터 부족 시 None."""
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
    return {
        "ma5": ma5, "ma20": ma20, "atr": atr, "atr_pct": round(atr_pct, 2),
        "vol_surge": round(vol_surge, 2), "rsi": round(rsi, 1),
        "close_pos": round(close_pos, 1), "breakout": breakout,
        "mom5": round(mom5, 2), "day_high": day_hi, "recent_low5": recent_low5,
        "yf_close": closes[-1], "change": change,
    }


def daily_indicators(yahoo):
    """yfinance 일봉(개별 호출)으로 지표 dict. 실패 시 None."""
    try:
        import yfinance as yf
        h = yf.Ticker(yahoo).history(period="80d", auto_adjust=False)
        if len(h) < 25:
            return None
        return _calc_indicators(
            [float(x) for x in h["Close"]], [float(x) for x in h["High"]],
            [float(x) for x in h["Low"]], [float(x) for x in h["Volume"]],
            [float(x) for x in h["Open"]])
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
        data = yf.download(syms, period="80d", auto_adjust=False,
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


def build_universe(market_targets, top_per_market=60):
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


def score_stock(price, ind):
    """0~100 기술 점수 + 근거 리스트."""
    s = 50.0
    why = []
    if ind["ma5"] > ind["ma20"]:
        s += 18
        why.append("정배열(MA5>MA20)")
    else:
        s -= 8
    if price > ind["ma20"]:
        s += 8
    if ind["vol_surge"] >= 1.5:
        s += 15
        why.append(f"거래량 급증 {ind['vol_surge']}x")
    elif ind["vol_surge"] >= 1.0:
        s += 7
    if ind["close_pos"] >= 60:
        s += 14
        why.append(f"강세 마감(종가위치 {ind['close_pos']}%)")
    elif ind["close_pos"] < 30:
        s -= 12
        why.append(f"윗꼬리/분배(종가위치 {ind['close_pos']}%)")
    if 50 <= ind["rsi"] <= 70:
        s += 13
        why.append(f"RSI {ind['rsi']}(상승)")
    elif ind["rsi"] > 75:
        s -= 10
        why.append(f"RSI {ind['rsi']}(과열)")
    elif ind["rsi"] < 35:
        s += 4
    if price >= ind["breakout"]:
        s += 14
        why.append("변동성 돌파 상회")
    if ind["mom5"] > 0:
        s += 8
    return max(0, min(100, round(s))), why


# 매매계획 보정 파라미터 기본값(통계 탭 학습으로 조정 가능). control.json engine.tuning.
DEFAULT_TUNING = {
    "stop_mult": 1.5,      # 손절폭 = stop_mult × hf × ATR
    "target1_mult": 2.0,   # 목표1 = 진입 + target1_mult × 위험
    "target2_mult": 3.0,   # 목표2 = 진입 + target2_mult × 위험
    "score_cutoff": 55,    # 신호 채택 점수 임계
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
    risk = entry - stop
    target1 = round_tick(entry + target1_mult * risk)
    target2 = round_tick(entry + target2_mult * risk)
    return {"entry": entry, "stop": stop, "target1": target1,
            "target2": target2, "etype": etype, "risk": risk}


def build_signal(rank, item, price, change_pct, ind, hold_cap, tuning=None):
    """기술 점수 통과 종목 → 매매계획 신호 dict. tuning(없으면 기본)로 손절·목표 조정."""
    t = {**DEFAULT_TUNING, **(tuning or {})}
    lv = levels(price, ind, hold_cap, t["stop_mult"], t["target1_mult"],
                t["target2_mult"])
    entry, stop, etype, risk = lv["entry"], lv["stop"], lv["etype"], lv["risk"]
    target1, target2 = lv["target1"], lv["target2"]
    if etype == "now":
        enote = f"기술 점수 상위·돌파 상회. 현재가({entry:,}) 부근 즉시 진입 가능, 거래량 확인."
    else:
        enote = f"{entry:,} 돌파 + 거래량 동반 시 진입(미돌파 시 미진입). 추격금지."
    rr = round((target1 - entry) / risk, 2) if risk > 0 else 0
    weight = int(max(3, min(8, round(8 - ind["atr_pct"] * 0.25))))
    return {
        "rank": rank, "name": item["name"], "code": item["code"], "market": "KR",
        "direction": "long", "confidence": "mid" if etype == "now" else "low",
        "price": float(price), "currency": "KRW", "entry": float(entry),
        "entry_type": etype, "entry_note": enote, "stop": stop,
        "target1": target1, "target2": target2, "rr": rr,
        "atr_pct": ind["atr_pct"], "hold_cap_hours": hold_cap, "weight_pct": weight,
        "catalyst_verified": False, "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "evidence": "기술 분석(뉴스 미확인) — " + ", ".join(ind["_why"]) +
                    f". ATR {ind['atr_pct']}%·5일모멘텀 {ind['mom5']}%.",
        "risk_notes": ["촉매(뉴스) 미확인 — 기술 신호만. 진입 전 재료·시초가 갭 확인.",
                       f"ATR {ind['atr_pct']}% 변동성 — 손절폭·비중 유의."],
        "tags": ["기술분석", "온디맨드"] + (["돌파대기"] if etype == "breakout" else ["즉시진입"]),
    }


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
        try:
            h = yf.Ticker(sym).history(period="6d", auto_adjust=False)
            closes = [float(x) for x in h["Close"].dropna()]
            if len(closes) >= 2 and closes[-2]:
                chg[k] = (closes[-1] / closes[-2] - 1) * 100
        except Exception:
            continue
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


def apply_regime(score, why, regime):
    """기술 점수에 시장 환경 보정(adj)을 더해 0~100 으로 클램프하고 근거를 남긴다."""
    if not regime or not regime.get("adj"):
        return score
    adj = regime["adj"]
    parts = []
    for k, lbl in (("sp", "S&P"), ("nasdaq", "나스닥"), ("sox", "SOX")):
        if k in regime:
            parts.append(f"{lbl}{regime[k]:+g}%")
    why.append(f"미국증시 환경 {adj:+g} (전일 {'·'.join(parts)})")
    return max(0, min(100, round(score + adj)))


# ── 미국 야간 컨텍스트(지수·빅테크·한줄평) 실측 갱신 ───────────────────────────
US_INDICES = [("나스닥", "^IXIC"), ("S&P500", "^GSPC"),
              ("다우", "^DJI"), ("필라델피아반도체(SOX)", "^SOX")]
US_BIGTECH = [("엔비디아", "NVDA"), ("마이크론", "MU"), ("브로드컴", "AVGO"),
              ("애플", "AAPL"), ("마이크로소프트", "MSFT"), ("알파벳", "GOOGL"),
              ("아마존", "AMZN"), ("메타", "META"), ("테슬라", "TSLA")]


def fetch_us_context():
    """미국 지수·빅테크 전일(또는 실시간) 종가·등락률을 yfinance 로 실측해 us_context
    dict 를 만든다. 한줄평·한국영향은 **측정된 수치에서 결정적으로 생성**(날조 없음).
    실패 시 None(호출 측이 기존 us_context 보존). 차트 엔진이 매 실행 갱신하므로
    미국 카드가 더 이상 옛 날짜에 멈추지 않는다."""
    try:
        import yfinance as yf
    except Exception:
        return None
    syms = [s for _, s in US_INDICES + US_BIGTECH]
    try:
        data = yf.download(syms, period="6d", auto_adjust=False,
                           group_by="ticker", threads=True, progress=False)
    except Exception as ex:
        print(f"[analyze] 미국 컨텍스트 다운로드 실패: {ex}")
        return None

    asof = None

    def one(sym):
        nonlocal asof
        try:
            sub = data[sym].dropna(subset=["Close"])
            closes = [float(x) for x in sub["Close"]]
            if len(closes) < 2 or closes[-2] == 0:
                return None
            chg = (closes[-1] / closes[-2] - 1) * 100
            try:
                asof = sub.index[-1].strftime("%Y-%m-%d")
            except Exception:
                pass
            return {"price": round(closes[-1], 2), "change_pct": round(chg, 2)}
        except Exception:
            return None

    indices, bigtech = [], []
    for name, sym in US_INDICES:
        q = one(sym)
        if q:
            indices.append({"name": name, "symbol": sym, **q, "asof": asof})
    for name, sym in US_BIGTECH:
        q = one(sym)
        if q:
            bigtech.append({"name": name, "symbol": sym, **q, "asof": asof})
    if not indices and not bigtech:
        return None

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
    summary = (("미국 " + ", ".join(parts) + ". ") if parts else "") + \
        f"빅테크 상승 {up}·하락 {down}."
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
        "basis": "미국 정규장 종가(야후 실측)",
        "session": "closed",
        "summary": summary,
        "kr_implication": kr_impl,
        "indices": indices,
        "bigtech": bigtech,
    }


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


def load_control():
    """control.json 파싱. 반환: (watchlist, scope, market_targets, hold_cap, tuning).
    tuning 은 engine.tuning(없으면 DEFAULT_TUNING) — 통계 탭 학습 보정 적용 지점."""
    wl, scope, mkts, cap = [], "watchlist", ["KOSPI", "KOSDAQ"], 48
    tuning = dict(DEFAULT_TUNING)
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
        except Exception as ex:
            print(f"[analyze] control 읽기 실패: {ex}")
    return wl, scope, mkts, cap, tuning


def yahoo_symbol(code):
    """KR 6자리 코드 → 야후 심볼(.KS 우선, 실패 시 .KQ는 호출측에서 시도)."""
    return f"{code}.KS"


def score_watchlist(watchlist, regime, cutoff):
    """관심종목을 KIS 현재가(있으면 정밀)+yfinance 일봉으로 점수화한다.
    KIS 토큰 발급/조회 실패해도 중단하지 않고 yfinance 로 폴백한다.
    반환: [(item, price, change, ind, score), ...]."""
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
        sc, why = score_stock(price, ind)
        sc = apply_regime(sc, why, regime)
        ind["_why"] = why
        out.append((item, price, change, ind, sc))
    return out


def score_universe(uni, regime):
    """전체종목 유니버스를 yfinance 배치로 점수화한다(현재가=일봉 종가, 속도·정합성).
    반환: [(item, price, change, ind, score), ...]."""
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
        sc, why = score_stock(price, ind)
        sc = apply_regime(sc, why, regime)
        ind["_why"] = why
        out.append((t, price, ind.get("change"), ind, sc))
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    now = datetime.datetime.now(KST)
    now_iso = now.replace(microsecond=0, second=0).isoformat()
    watchlist, _scope, mkts, hold_cap, tuning = load_control()
    cutoff = int(tuning.get("score_cutoff", 55))
    if tuning != DEFAULT_TUNING:
        print(f"[analyze] 학습 보정 적용: {tuning}")

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

    wl_cand = score_watchlist(watchlist, regime, cutoff)

    universe_failed = False
    uni = build_universe(mkts)
    uni = [u for u in uni if u.get("code") not in wl_codes]
    if not uni:
        universe_failed = True
        print("[analyze] 전체종목 유니버스 미확보 — 이번 회차는 관심종목만.")
    mk_cand = score_universe(uni, regime)
    print(f"[analyze] 통합 분석: 관심종목 후보 {len(wl_cand)} / 전체종목 후보 "
          f"{len(mk_cand)} (markets={mkts})")

    if not wl_cand and not mk_cand:
        print("[analyze] 분석 가능한 종목 없음 — feed 미변경")
        return 0

    # 관심종목 신호(점수순 최대 5) + 관찰(미충족 watchlist).
    obs_cap = 40
    wl_cand.sort(key=lambda x: x[4], reverse=True)
    wl_signals, observations = [], []
    rank = 0
    for item, price, change, ind, sc in wl_cand:
        if sc >= cutoff and rank < 5:
            rank += 1
            sig = build_signal(rank, item, price, change, ind, hold_cap, tuning)
            sig["_score"] = sc
            sig["group"] = "watchlist"
            wl_signals.append(sig)
        elif len(observations) < obs_cap:
            observations.append({
                "name": item["name"], "code": item["code"], "market": "KR",
                "price": float(price), "currency": "KRW", "watch_trigger": None,
                "change_pct": round(change, 2) if change is not None else None,
                "reason": f"기술 점수 {sc}/100 — 조건 미충족(관망). "
                          + (", ".join(ind["_why"]) if ind["_why"] else "뚜렷한 강세 신호 부족")
                          + f". ATR {ind['atr_pct']}%.",
            })

    # 전체종목 신호(점수순 최대 5, 관심종목 제외). 비신호는 관찰에 넣지 않는다(후보 수백).
    mk_cand.sort(key=lambda x: x[4], reverse=True)
    market_signals = []
    mrank = 0
    for item, price, change, ind, sc in mk_cand:
        if sc >= cutoff and mrank < 5:
            mrank += 1
            sig = build_signal(mrank, item, price, change, ind, hold_cap, tuning)
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
        "horizon_hours": hold_cap,
        "data_source": f"온디맨드 기술 분석(KIS 현재가 + yfinance 일봉 지표) + 미국증시 전일 환경 보정 · 분석 범위: {scan_label}. 뉴스/촉매 미반영.",
        "market_state": feed.get("market_state", {"korea": {"status": "closed"}, "us": {"status": "closed"}}),
        # 시장 환경 보정(측정 가능한 미국증시만) — 앱이 방법론·투명성 표기에 쓸 수 있다.
        "market_regime": regime,
        "summary": {
            "signal_count": len(signals), "observation_count": len(observations),
            "position_count": len(feed.get("positions", [])),
            "top_signal": top,
            "headline": f"기술 분석 갱신({now.strftime('%m-%d %H:%M')} KST) — 차트·거래량·변동성 기준 "
                        f"{len(signals)}개 신호. 뉴스 미반영이니 진입 전 재료 확인.",
        },
        "signals": [{**{k: v for k, v in s.items() if k != "_score"},
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

    feed.setdefault("positions", feed.get("positions", []))
    feed.setdefault("portfolio", feed.get("portfolio", {"total_unrealized": 0, "count": 0, "to_close": 0}))
    feed.setdefault("assumptions", feed.get("assumptions", {"fee_pct": 0.015, "tax_pct_kr": 0.18, "tax_pct_us": 0.0}))

    # 신선한 뉴스 촉매가 있으면 이번 차트 신호에 다시 입힌다(뉴스 덮어쓰기 방지).
    kept = reapply_fresh_catalyst(feed, prev_catalyst, now)
    if kept:
        print(f"[analyze] 뉴스 촉매 보존 반영: {kept}건")

    FEED_PATH.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 전향 추적용 — 이번 발행 신호를 로그에 누적(통계 탭 forward 평가용).
    append_signal_log(feed["signals"], now_iso, now.strftime("%Y-%m-%d"), hold_cap)
    print(f"[analyze] 완료 @ {now_iso} — 신호 {len(signals)} "
          f"(관심 {len(wl_signals)}+전체 {len(market_signals)}) / 관찰 {len(observations)} "
          f"(top {top})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
