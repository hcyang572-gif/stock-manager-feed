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


def build_universe(market_targets, top_per_market=60):
    """pykrx 로 시장별 **거래대금 상위** 종목을 가져와 [{code,name,market,_mk}] 반환.
    종목 코드·이름을 KRX 에서 실측으로 받아 정합성을 지킨다(하드코딩·날조 없음).
    pykrx 미설치/네트워크 실패 시 빈 리스트 → 호출 측이 관심종목만으로 폴백."""
    out = []
    try:
        from pykrx import stock
    except Exception as ex:
        print(f"[analyze] pykrx 없음 — 유니버스 생략(관심종목만): {ex}")
        return []
    now = datetime.datetime.now(KST)
    today = now.strftime("%Y%m%d")
    weekago = (now - datetime.timedelta(days=12)).strftime("%Y%m%d")
    valid = {"KOSPI", "KOSDAQ"}
    for mk in [m.upper() for m in market_targets if m.upper() in valid]:
        try:
            df = stock.get_market_price_change(weekago, today, market=mk)
        except Exception as ex:
            print(f"[analyze] pykrx {mk} 조회 실패: {ex}")
            continue
        if df is None or len(df) == 0:
            continue
        if "거래대금" in df.columns:
            df = df.sort_values("거래대금", ascending=False)
        cnt = 0
        for code, row in df.iterrows():
            name = str(row.get("종목명", "")).strip()
            if not name:
                continue
            out.append({"code": str(code).zfill(6), "name": name,
                        "market": "KR", "_mk": mk})
            cnt += 1
            if cnt >= top_per_market:
                break
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


def build_signal(rank, item, price, change_pct, ind, hold_cap):
    """기술 점수 통과 종목 → 매매계획 신호 dict."""
    atr = ind["atr"]
    breakout = ind["breakout"]
    # 진입: 돌파+강세마감이면 즉시, 아니면 돌파 대기.
    if price >= breakout and ind["close_pos"] >= 50:
        entry = round_tick(price)
        etype = "now"
        enote = f"기술 점수 상위·돌파 상회. 현재가({entry:,}) 부근 즉시 진입 가능, 거래량 확인."
    else:
        entry = round_tick(max(breakout, ind["day_high"]))
        etype = "breakout"
        enote = f"{entry:,} 돌파 + 거래량 동반 시 진입(미돌파 시 미진입). 추격금지."
    # 보유 캡(시간)에 따라 손절·목표 폭을 조정한다 — 가격 변동성은 √시간에 비례하므로
    # 24h 를 기준(hf=1)으로 짧으면 타이트(hf↓)하게, 길면 넓게(hf↑) 잡는다(0.6~1.8 클램프).
    hf = max(0.6, min(1.8, math.sqrt(hold_cap / 24.0)))
    stop = round_tick(max(entry - 1.5 * hf * atr, ind["recent_low5"]))
    if stop >= entry:
        stop = round_tick(entry * (1 - 0.02 * hf))
    risk = entry - stop
    target1 = round_tick(entry + 2 * risk)
    target2 = round_tick(entry + 3 * risk)
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


def load_control():
    wl, scope, mkts, cap = [], "watchlist", ["KOSPI", "KOSDAQ"], 48
    if CONTROL_PATH.exists():
        try:
            c = json.loads(CONTROL_PATH.read_text(encoding="utf-8-sig"))
            e = c.get("engine", {})
            scope = e.get("analysis_scope", scope)
            mkts = e.get("market_targets", mkts) or mkts
            cap = int(e.get("hold_cap_hours", cap) or cap)
            wl = c.get("watchlist", []) or []
        except Exception as ex:
            print(f"[analyze] control 읽기 실패: {ex}")
    return wl, scope, mkts, cap


def yahoo_symbol(code):
    """KR 6자리 코드 → 야후 심볼(.KS 우선, 실패 시 .KQ는 호출측에서 시도)."""
    return f"{code}.KS"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    now = datetime.datetime.now(KST)
    now_iso = now.replace(microsecond=0, second=0).isoformat()
    watchlist, scope, mkts, hold_cap = load_control()
    if not watchlist and scope != "market":
        print("[analyze] watchlist 비어있음 — 중단")
        return 0

    # 분석 대상 구성. scope=='market' 이면 watchlist(시드) + KRX 거래대금 상위
    # 유니버스를 합쳐 **전체종목**을 차트로 스캔한다. 유니버스 확보 실패 시
    # 관심종목만으로 안전하게 폴백한다(중단 없음).
    # 시장 환경(미국 전일) 보정치 — 1회 계산해 모든 종목 점수에 동일 적용.
    regime = fetch_us_regime()
    if regime:
        print(f"[analyze] 시장환경: 전일 미국 가중 {regime['regime_pct']:+g}% → "
              f"보정 {regime['adj']:+g} (S&P{regime.get('sp', 0):+g}·"
              f"나스닥{regime.get('nasdaq', 0):+g}·SOX{regime.get('sox', 0):+g})")

    targets = [dict(w) for w in watchlist]
    use_batch = False
    if scope == "market":
        uni = build_universe(mkts)
        if uni:
            seen = {t.get("code") for t in targets}
            for u in uni:
                if u["code"] not in seen:
                    targets.append(u)
                    seen.add(u["code"])
            use_batch = True
            print(f"[analyze] 전체종목 스캔: watchlist {len(watchlist)} + "
                  f"유니버스 {len(uni)} → 대상 {len(targets)} (markets={mkts})")
        else:
            print("[analyze] 유니버스 미확보 — 관심종목만 분석으로 폴백")

    cand = []  # (item, price, change, ind, score)
    if use_batch:
        # 전체종목 — 야후 배치 다운로드(시장별 접미사). 수백 종목이라 KIS 현재가는
        # 생략하고 yfinance 일봉 종가/등락을 쓴다(정합성 유지·속도 확보).
        sym_for = {t["code"]: t["code"] + (".KQ" if t.get("_mk") == "KOSDAQ" else ".KS")
                   for t in targets}
        inds = daily_indicators_batch(list(sym_for.values()))
        for t in targets:
            code = t["code"]
            ind = inds.get(sym_for[code])
            # watchlist 시드는 시장 정보가 없을 수 있어 개별 .KS/.KQ 폴백.
            if ind is None and not t.get("_mk"):
                ind = daily_indicators(code + ".KS") or daily_indicators(code + ".KQ")
            if ind is None:
                continue
            price = ind["yf_close"]
            sc, why = score_stock(price, ind)
            sc = apply_regime(sc, why, regime)
            ind["_why"] = why
            cand.append((t, price, ind.get("change"), ind, sc))
    else:
        # 관심종목만 — KIS 현재가(있으면 더 정확) + yfinance 일봉. KIS 토큰 발급/조회
        # 실패해도 절대 중단하지 않고 yfinance 로 폴백(1분당 1회 발급 제한 EGW00133).
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
            cand.append((item, price, change, ind, sc))

    if not cand:
        print("[analyze] 분석 가능한 종목 없음 — feed 미변경")
        return 0

    cand.sort(key=lambda x: x[4], reverse=True)
    # 점수 55 이상을 신호(최대 5), 나머지 관찰. 전체종목 스캔은 후보가 수백 개라
    # 관찰을 점수 상위 일부(OBS_CAP)로 제한해 feed·앱이 비대해지지 않게 한다.
    obs_cap = 40
    signals, observations = [], []
    rank = 0
    for item, price, change, ind, sc in cand:
        if sc >= 55 and rank < 5:
            rank += 1
            sig = build_signal(rank, item, price, change, ind, hold_cap)
            sig["_score"] = sc
            signals.append(sig)
        elif len(observations) < obs_cap:
            observations.append({
                "name": item["name"], "code": item["code"], "market": "KR",
                "price": float(price), "currency": "KRW", "watch_trigger": None,
                "change_pct": round(change, 2) if change is not None else None,
                "reason": f"기술 점수 {sc}/100 — 조건 미충족(관망). "
                          + (", ".join(ind["_why"]) if ind["_why"] else "뚜렷한 강세 신호 부족")
                          + f". ATR {ind['atr_pct']}%.",
            })

    # 기존 feed 보존 항목(us_context·kr_context·positions·portfolio·assumptions).
    feed = {}
    if FEED_PATH.exists():
        try:
            feed = json.loads(FEED_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            feed = {}

    top = signals[0]["name"] if signals else (observations[0]["name"] if observations else None)
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
        "data_source": "온디맨드 기술 분석(KIS 현재가 + yfinance 일봉 지표) + 미국증시 전일 환경 보정. 뉴스/촉매 미반영.",
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
        "signals": [{k: v for k, v in s.items() if k != "_score"} for s in signals],
        "observations": observations,
        "risk_notes": [
            "온디맨드 기술 분석 — 뉴스/촉매 미반영(catalyst_verified=false). 진입 전 재료·공시 직접 확인.",
            "지표는 KIS 현재가 + yfinance 일봉 실측(날조 없음). 변동성 장세 보수적 대응.",
            regime_note or "시장 환경 보정 미적용(미국증시 데이터 일시 미확보).",
            "본 신호는 투자 참고용이며 매수·매도를 보장하지 않는다. 실주문은 본인 판단·실행.",
        ],
        "disclaimer": feed.get("disclaimer",
                               "본 산출물은 투자 참고용이며 매수·매도를 보장하지 않습니다. 실주문은 사용자가 직접 판단·실행합니다."),
    })
    feed.setdefault("positions", feed.get("positions", []))
    feed.setdefault("portfolio", feed.get("portfolio", {"total_unrealized": 0, "count": 0, "to_close": 0}))
    feed.setdefault("assumptions", feed.get("assumptions", {"fee_pct": 0.015, "tax_pct_kr": 0.18, "tax_pct_us": 0.0}))

    FEED_PATH.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 전향 추적용 — 이번 발행 신호를 로그에 누적(통계 탭 forward 평가용).
    append_signal_log(feed["signals"], now_iso, now.strftime("%Y-%m-%d"), hold_cap)
    print(f"[analyze] 완료 @ {now_iso} — 신호 {len(signals)} / 관찰 {len(observations)} "
          f"(top {top}, scope {scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
