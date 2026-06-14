#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""백그라운드 가격 도달·급등락 감시 → FCM 푸시(앱 종료 중에도 알림).

워크플로(price-alert-watch.yml)가 5분 주기로 호출한다. 정규장+시간외
(08:00~20:00, control.robot.window) 동안 KIS 통합(NXT) 실측가로:
  (1) feed.json 신호의 진입/손절/목표 레벨 도달
  (2) control.json 관심종목의 단기 급등/급락(윈도우 내 변동률)
을 감지해 토픽 'analysis' 로 푸시한다. FCM_SERVICE_ACCOUNT 없으면 발송만 건너뜀.

판정·기본값은 앱(price_alert_service / surge_alert_service)과 동일하게 맞춘다:
  진입·목표=상방(>=), 손절=하방(<=). 급등락 기본 ±3%·10분.
사용자 설정(control.json.alerts)이 있으면 그대로 반영한다(앱이 저장).

상태(중복방지 발사기록·급등락 표본)는 config/.alert_state.json 에 저장하고,
워크플로가 actions/cache 로 실행 간 보존한다(레포 커밋 없음). 시세 조회·창
판정은 intraday_refresh 의 함수를 재사용한다(수치 날조 없음).
"""
import datetime
import json
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import intraday_refresh as ir  # 같은 폴더 — 시세 조회/세션·개장 판정 재사용
import fcm_notify              # send_message(title, body)


def fetch_kr_intraday(code):
    """네이버 polling 으로 KR 종목 장중 데이터(현재가·누적거래량·당일 고가·등락률)를
    실측 조회한다(장중 진입 모멘텀 판정용). 실패 시 None. 수치는 모두 실측(날조 없음)."""
    code = (code or "").strip()
    if not code:
        return None
    url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
    try:
        req = urllib.request.Request(
            url, headers={"Referer": "https://m.stock.naver.com/",
                          "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    datas = d.get("datas") or []
    if not datas:
        return None
    k = datas[0]

    def num(*keys):
        for key in keys:
            v = str(k.get(key, "")).replace(",", "").strip()
            if v:
                try:
                    return float(v)
                except ValueError:
                    continue
        return None

    price = num("closePriceRaw", "closePrice")
    if price is None:
        return None
    return {
        "price": price,
        "accvol": num("accumulatedTradingVolumeRaw", "accumulatedTradingVolume") or 0,
        "high": num("highPriceRaw", "highPrice") or price,
        "ratio": num("fluctuationsRatioRaw", "fluctuationsRatio") or 0,
    }

KST = ZoneInfo("Asia/Seoul")
REPO = Path(__file__).resolve().parent.parent
FEED_PATH = REPO / "feed.json"
CONTROL_PATH = REPO / "control.json"
STATE_PATH = REPO / "config" / ".alert_state.json"

# control.json.alerts 기본값(앱 기본과 동일).
DEFAULT_ALERTS = {
    "price_enabled": True,
    "lv_entry": True, "lv_stop": True, "lv_target1": True, "lv_target2": True,
    "surge_enabled": True, "surge_threshold": 3.0, "surge_window": 10,
    # 보유종목 경보(옵트인) — 기본 OFF(프라이버시). 켜면 holdings_watch 감시.
    "hold_enabled": False, "hold_loss_pct": 5.0,
    # 장중 진입 모멘텀 경보(옵트인·기본 OFF) — 관심종목 상승+거래량가속+고가돌파.
    "mom_enabled": False, "mom_threshold": 1.5,
}

# 신호 레벨 정의: (feed 키, 설정 키, 라벨, 방향). 방향 '>='=상방, '<='=하방.
LEVEL_DEFS = [
    ("entry", "lv_entry", "진입가", ">="),
    ("stop", "lv_stop", "손절가", "<="),
    ("target1", "lv_target1", "목표1가", ">="),
    ("target2", "lv_target2", "목표2가", ">="),
]

# 한 번에 보낼 개별 푸시 최대치. 초과하면 요약 1건으로(도배 방지).
MAX_INDIVIDUAL = 3
# 급등락 표본 최소 누적 시간(초) — 앱과 동일(헛알람 방지).
MIN_HISTORY_SEC = 60


def _load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _get_alerts(control):
    a = dict(DEFAULT_ALERTS)
    src = control.get("alerts") or {}
    for k in DEFAULT_ALERTS:
        if k in src and src[k] is not None:
            a[k] = src[k]
    try:
        a["surge_threshold"] = float(a["surge_threshold"]) or 3.0
    except (TypeError, ValueError):
        a["surge_threshold"] = 3.0
    try:
        a["surge_window"] = int(a["surge_window"]) or 10
    except (TypeError, ValueError):
        a["surge_window"] = 10
    try:
        a["hold_loss_pct"] = float(a["hold_loss_pct"]) or 5.0
    except (TypeError, ValueError):
        a["hold_loss_pct"] = 5.0
    try:
        a["mom_threshold"] = float(a["mom_threshold"]) or 1.5
    except (TypeError, ValueError):
        a["mom_threshold"] = 1.5
    return a


def main():
    force = "--force" in sys.argv[1:]
    now = datetime.datetime.now(KST)

    # 운영창(08:00~20:00 등)을 control.json 으로 맞춘 뒤 개장 판정.
    ir.WINDOW_OPEN, ir.WINDOW_CLOSE, _ = ir.load_control()
    ok, reason = ir.is_trading_now(now)
    if not ok and not force:
        print(f"[alert-watch] skip ({reason}) @ {now.isoformat()}")
        return 0

    control = _load_json(CONTROL_PATH, {})
    alerts = _get_alerts(control)
    if (not alerts["price_enabled"] and not alerts["surge_enabled"]
            and not alerts["hold_enabled"] and not alerts["mom_enabled"]):
        print("[alert-watch] 모든 알람 OFF — skip")
        return 0

    now_iso = now.replace(microsecond=0, second=0).isoformat()
    now_ms = int(now.timestamp() * 1000)
    tradingday = now.date().isoformat()
    session_key, session_label = ir.session_of(now)

    feed = _load_json(FEED_PATH, {})
    signals = feed.get("signals", []) or []
    watchlist = control.get("watchlist") or []

    state = _load_json(STATE_PATH, {})
    fired = state.get("fired") or {}            # {tradingday: {key: true}}
    # 거래일이 바뀌면 발사기록 리셋(직전 1일치만 보관).
    for d in list(fired.keys()):
        if d != tradingday:
            fired.pop(d, None)
    day_fired = fired.setdefault(tradingday, {})
    samples = state.get("surge_samples") or {}   # {code: [[ts, price], ...]}
    cooldown = state.get("surge_cooldown") or {}  # {code|dir: until_ms}

    # 시세 캐시 — 같은 종목 중복 조회 방지.
    quote_cache = {}

    def price_of(code, market):
        key = f"{market}:{code}"
        if key not in quote_cache:
            try:
                p, *_ = ir.fetch_quote(code, market, session_key, session_label, now_iso)
            except Exception as e:
                print(f"[alert-watch] 시세 조회 실패 {key}: {e}")
                p = None
            quote_cache[key] = p
        return quote_cache[key]

    events = []  # (title, body)

    # ── (1) 신호 가격 도달 ──
    if alerts["price_enabled"]:
        for s in signals:
            code = (s.get("code") or "").strip()
            market = (s.get("market") or "KR").upper()
            if not code:
                continue
            price = price_of(code, market)
            if not price or price <= 0:
                continue
            name = s.get("name") or code
            for lkey, setting, label, op in LEVEL_DEFS:
                if not alerts.get(setting, True):
                    continue
                try:
                    level = float(s.get(lkey))
                except (TypeError, ValueError):
                    continue
                if not level or level <= 0:
                    continue
                reached = (price >= level) if op == ">=" else (price <= level)
                if not reached:
                    continue
                fkey = f"{code}|{lkey}"
                if day_fired.get(fkey):
                    continue
                day_fired[fkey] = True
                events.append((
                    f"🎯 {name} {label} 도달",
                    f"현재가 {price:,.0f} · {label} {level:,.0f} 도달. 탭해서 확인하세요.",
                ))

    # ── (2) 관심종목 급등/급락 ──
    if alerts["surge_enabled"]:
        thr = alerts["surge_threshold"]
        win = alerts["surge_window"]
        window_ms = win * 60 * 1000
        cutoff = now_ms - window_ms
        for w in watchlist:
            code = (w.get("code") or "").strip()
            market = (w.get("market") or "KR").upper()
            if not code:
                continue
            price = price_of(code, market)
            if not price or price <= 0:
                continue
            buf = samples.setdefault(code, [])
            buf.append([now_ms, price])
            buf[:] = [x for x in buf if x[0] >= cutoff]
            if len(buf) < 2:
                continue
            ref_ts, ref_price = buf[0]
            if ref_price <= 0 or now_ms - ref_ts < MIN_HISTORY_SEC * 1000:
                continue
            pct = (price - ref_price) / ref_price * 100.0
            if abs(pct) < thr:
                continue
            is_surge = pct > 0
            dirkey = f"{code}|{'up' if is_surge else 'down'}"
            if now_ms < cooldown.get(dirkey, 0):
                continue
            cooldown[dirkey] = now_ms + window_ms
            name = w.get("name") or code
            arrow = "📈 급등" if is_surge else "📉 급락"
            events.append((
                f"{arrow} {name} {pct:+.1f}%",
                f"최근 {win}분 {pct:+.1f}% ({ref_price:,.0f}→{price:,.0f}). 탭해서 확인하세요.",
            ))

    # ── (3) 보유종목 급락·이탈(옵트인, holdings_watch) ──
    if alerts["hold_enabled"]:
        holdings = control.get("holdings_watch") or []
        thr = alerts["surge_threshold"]
        win = alerts["surge_window"]
        hloss = alerts["hold_loss_pct"]
        window_ms = win * 60 * 1000
        cutoff = now_ms - window_ms
        hbuf = state.get("hold_samples") or {}
        for h in holdings:
            code = (h.get("code") or "").strip()
            market = (h.get("market") or "KR").upper()
            if not code:
                continue
            price = price_of(code, market)
            if not price or price <= 0:
                continue
            name = h.get("name") or code
            # (a) 이탈(손실): 매수가 대비 -hloss% 이하 — 하루 1회.
            try:
                entry = float(h.get("entry") or 0)
            except (TypeError, ValueError):
                entry = 0.0
            if entry > 0:
                loss_pct = (price - entry) / entry * 100.0
                if loss_pct <= -hloss:
                    fkey = f"hold_loss|{code}"
                    if not day_fired.get(fkey):
                        day_fired[fkey] = True
                        events.append((
                            f"🛑 {name} 손실 {loss_pct:+.1f}%",
                            f"매수가 {entry:,.0f} 대비 {loss_pct:+.1f}% "
                            f"(현재 {price:,.0f}). 청산을 고려하세요.",
                        ))
            # (b) 급락(window): 하락 방향만 — 빠르게 빠질 때 즉시 알림.
            buf = hbuf.setdefault(code, [])
            buf.append([now_ms, price])
            buf[:] = [x for x in buf if x[0] >= cutoff]
            if len(buf) >= 2:
                ref_ts, ref_price = buf[0]
                if ref_price > 0 and now_ms - ref_ts >= MIN_HISTORY_SEC * 1000:
                    pct = (price - ref_price) / ref_price * 100.0
                    if pct <= -thr:
                        dirkey = f"hold|{code}|down"
                        if now_ms >= cooldown.get(dirkey, 0):
                            cooldown[dirkey] = now_ms + window_ms
                            events.append((
                                f"📉 {name} 급락 {pct:+.1f}%",
                                f"보유종목 최근 {win}분 {pct:+.1f}% "
                                f"({ref_price:,.0f}→{price:,.0f}). 청산을 고려하세요.",
                            ))
        for code in list(hbuf.keys()):
            if len(hbuf[code]) > 50:
                hbuf[code] = hbuf[code][-50:]
        state["hold_samples"] = hbuf

    # ── (4) 장중 진입 모멘텀(옵트인) — 관심종목 KR ──
    # "오를 때 빨리 타기": 최근 N분 상승 + **거래량 가속**(누적거래량 증가분) +
    # 당일 고가 돌파 + 과열 아님일 때만 알림(실측 네이버 데이터, 날조 없음).
    if alerts["mom_enabled"]:
        momthr = alerts["mom_threshold"]
        win = alerts["surge_window"]
        window_ms = win * 60 * 1000
        mcut = now_ms - window_ms
        mbuf = state.get("mom_samples") or {}
        for w in watchlist:
            code = (w.get("code") or "").strip()
            market = (w.get("market") or "KR").upper()
            if not code or market != "KR":
                continue
            info = fetch_kr_intraday(code)
            if not info:
                continue
            price = info["price"]
            buf = mbuf.setdefault(code, [])
            buf.append([now_ms, price, info["accvol"]])
            buf[:] = [x for x in buf if x[0] >= mcut]
            if len(buf) < 3:
                continue
            ref_ts, ref_price, _ = buf[0]
            if ref_price <= 0 or now_ms - ref_ts < 6 * 60 * 1000:
                continue
            chg = (price - ref_price) / ref_price * 100.0
            # 거래량 가속: 직전 구간 증가분 vs 그 이전 구간들 평균.
            last_iv = info["accvol"] - buf[-2][2]
            prior_ivs = [buf[j][2] - buf[j - 1][2] for j in range(1, len(buf) - 1)]
            avg_prior = sum(prior_ivs) / len(prior_ivs) if prior_ivs else 0
            vol_accel = avg_prior > 0 and last_iv >= 1.8 * avg_prior
            near_high = price >= info["high"] * 0.999  # 당일 고가 부근/돌파
            not_overext = info["ratio"] < 12           # 이미 급등 끝물 아님
            if chg >= momthr and vol_accel and near_high and not_overext:
                dirkey = f"mom|{code}"
                if now_ms >= cooldown.get(dirkey, 0):
                    cooldown[dirkey] = now_ms + window_ms
                    name = w.get("name") or code
                    body = (f"최근 {win}분 +{chg:.1f}% · 거래량 가속 · 당일 고가 돌파. "
                            f"진입 검토(추격 주의·손절 함께).")
                    events.append((
                        f"📈 {name} 진입 모멘텀 +{chg:.1f}%", body,
                        {"type": "momentum", "code": code, "name": name,
                         "pct": f"{chg:.1f}", "price": f"{price:.0f}",
                         "level": f"{info['high']:.0f}", "currency": "KRW"},
                    ))
        for c in list(mbuf.keys()):
            if len(mbuf[c]) > 50:
                mbuf[c] = mbuf[c][-50:]
        state["mom_samples"] = mbuf

    # 표본 버퍼가 무한정 커지지 않게 종목별 최근 50개만 유지.
    for code in list(samples.keys()):
        if len(samples[code]) > 50:
            samples[code] = samples[code][-50:]

    # 상태 저장(다음 실행에서 cache 복원).
    state["fired"] = fired
    state["surge_samples"] = samples
    state["surge_cooldown"] = cooldown
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    if not events:
        print(f"[alert-watch] 도달/급등락 없음 @ {now_iso} ({session_label})")
        return 0

    # 발송 — 건수 많으면 요약 1건(도배 방지). 이벤트는 (title, body[, data]).
    if len(events) > MAX_INDIVIDUAL:
        head = " / ".join(ev[0] for ev in events[:MAX_INDIVIDUAL])
        fcm_notify.send_message(
            f"🔔 알람 {len(events)}건",
            f"{head} 외 {len(events) - MAX_INDIVIDUAL}건. 탭해서 확인하세요.")
    else:
        for ev in events:
            data = ev[2] if len(ev) > 2 else None
            fcm_notify.send_message(ev[0], ev[1], data=data)
    print(f"[alert-watch] {len(events)}건 발송 @ {now_iso} ({session_label})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
