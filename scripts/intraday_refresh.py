#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
장중·장외 경량 시세 갱신 — feed.json 의 신호/관찰 종목 현재가를 KIS 통합(NXT) 시세로 갱신.

- 신규 종목 발굴(풀 스캔)은 하지 않는다(그건 아침 풀 발굴 1회가 담당). 여기서는
  이미 선정된 종목의 price/ext/asof 만 실측으로 최신화한다(수치 날조 없음).
- **NXT(넥스트레이드) 연장거래 반영**: 한국 대체거래소 NXT 는 08:00~20:00 거래.
  KIS `UN`(KRX+NXT 통합) 시세를 현재가(price)로, `J`(정규장) 종가를 기준(ref_close)으로
  하여 시간외 등락(ext.delta_pct)을 계산한다. 프리/정규/애프터 세션도 함께 표기.
- 한국 개장일·시간 가드: 주말·공휴일·연말휴장 제외, **08:00~20:00 KST** 에서만 동작.
- 값이 바뀐 게 없으면 파일을 건드리지 않는다(워크플로가 변경 없을 때 커밋 생략).

GitHub Actions(.github/workflows/intraday-refresh.yml)가 10분마다 호출한다.
로컬 점검: `python scripts/intraday_refresh.py --force`(시간 가드 무시).
"""
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

# KIS 초당 호출 제한(유량초과) 회피용 호출 간 최소 간격(초).
KIS_CALL_GAP = 0.25

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = REPO_ROOT / "feed.json"
KIS_CONFIG_PATH = REPO_ROOT / "config" / "kis_config.json"
KIS_TOKEN_PATH = REPO_ROOT / "config" / ".kis_token.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
NAVER_POLL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
KIS_BASE = "https://openapi.koreainvestment.com:9443"

# NXT 연장거래 운영시간(KST, 분 단위). 프리 08:00~09:00 / 정규 09:00~15:30 / 애프터 15:30~20:00.
WINDOW_OPEN = 8 * 60          # 08:00
WINDOW_CLOSE = 20 * 60        # 20:00
REGULAR_OPEN = 9 * 60         # 09:00
REGULAR_CLOSE = 15 * 60 + 30  # 15:30


def session_of(now_kst):
    """현재 KST 시각 → (session_key, 한글라벨). 창 밖이면 ('closed','마감')."""
    m = now_kst.hour * 60 + now_kst.minute
    if WINDOW_OPEN <= m < REGULAR_OPEN:
        return "pre", "프리마켓"
    if REGULAR_OPEN <= m <= REGULAR_CLOSE:
        return "regular", "정규장"
    if REGULAR_CLOSE < m <= WINDOW_CLOSE:
        return "after", "애프터마켓"
    return "closed", "마감"


def _load_kis_config():
    """KIS 설정 로드 — 파일 우선, 없으면 환경변수(GitHub Actions)."""
    app_key = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    if KIS_CONFIG_PATH.exists():
        cfg = json.loads(KIS_CONFIG_PATH.read_text(encoding="utf-8"))
        app_key = app_key or cfg.get("app_key")
        app_secret = app_secret or cfg.get("app_secret")
        account_type = cfg.get("account_type", "real")
    else:
        account_type = os.environ.get("KIS_ACCOUNT_TYPE", "real")
    if not app_key or not app_secret:
        return None
    return {"app_key": app_key, "app_secret": app_secret, "account_type": account_type}


def _get_kis_token(cfg):
    """KIS 액세스 토큰 취득 — 캐시 유효하면 재사용, 만료 시 재발급."""
    now_ts = datetime.datetime.now(KST).timestamp()
    if KIS_TOKEN_PATH.exists():
        try:
            cached = json.loads(KIS_TOKEN_PATH.read_text(encoding="utf-8"))
            if cached.get("expires_at", 0) > now_ts + 300:
                return cached["access_token"]
        except Exception:
            pass
    body = json.dumps({
        "grant_type": "client_credentials",
        "appkey": cfg["app_key"],
        "appsecret": cfg["app_secret"],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{KIS_BASE}/oauth2/tokenP",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.load(r)
    token = resp.get("access_token")
    expires_in = int(resp.get("expires_in", 86400))
    if token:
        KIS_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        KIS_TOKEN_PATH.write_text(
            json.dumps({"access_token": token, "expires_at": now_ts + expires_in},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    return token


def fetch_price_kis(code, cfg, token, mrkt="J"):
    """KIS OpenAPI 로 국내 종목 현재가 조회.
    mrkt: 'J'=정규장(KRX), 'NX'=NXT, 'UN'=KRX+NXT 통합. 실패 시 None.
    """
    tr_id = "FHKST01010100" if cfg.get("account_type") == "real" else "VHKST01010100"
    time.sleep(KIS_CALL_GAP)  # 초당 호출 제한 회피
    params = urllib.parse.urlencode({
        "FID_COND_MRKT_DIV_CODE": mrkt,
        "FID_INPUT_ISCD": code,
    })
    req = urllib.request.Request(
        f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price?{params}",
        headers={
            "Authorization": f"Bearer {token}",
            "appkey": cfg["app_key"],
            "appsecret": cfg["app_secret"],
            "tr_id": tr_id,
            "custtype": "P",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        o = data.get("output", {})
        price_str = str(o.get("stck_prpr", "")).replace(",", "")
        chg_str = str(o.get("prdy_ctrt", "")).replace(",", "")  # 전일대비 등락률(%)
        price = float(price_str) if price_str else None
        chg = float(chg_str) if chg_str not in ("", "None") else None
        return price, chg
    except Exception:
        return None, None


# 한국 대표 지수(KIS 업종지수 inquire-index-price, MRKT=U).
KR_INDEX_LIST = [("코스피", "KOSPI", "0001"),
                 ("코스닥", "KOSDAQ", "1001"),
                 ("코스피200", "KOSPI200", "2001")]


def fetch_kr_indices(cfg, token):
    """KIS 로 코스피·코스닥·코스피200 현재지수·등락률 조회(각 1회 재시도). 실패 종목은 제외."""
    out = []
    for name, sym, iscd in KR_INDEX_LIST:
        params = urllib.parse.urlencode({"FID_COND_MRKT_DIV_CODE": "U",
                                         "FID_INPUT_ISCD": iscd})
        req = urllib.request.Request(
            f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-index-price?{params}",
            headers={
                "Authorization": f"Bearer {token}",
                "appkey": cfg["app_key"],
                "appsecret": cfg["app_secret"],
                "tr_id": "FHPUP02100000",
                "custtype": "P",
            },
        )
        for attempt in range(2):  # 일시적 유량초과 대비 1회 재시도
            time.sleep(KIS_CALL_GAP)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.load(r)
                o = data.get("output", {})
                p = o.get("bstp_nmix_prpr")
                c = o.get("bstp_nmix_prdy_ctrt")
                if p:
                    out.append({"name": name, "symbol": sym,
                                "price": round(float(p), 2),
                                "change_pct": round(float(c), 2) if c else 0.0})
                    break
            except Exception:
                pass
    return out


def _get_json(url, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def is_trading_now(now_kst):
    """(개장일·시간) → (bool, 사유). 주말·공휴일·연말휴장 제외, 08:00~20:00(NXT 연장창)."""
    if now_kst.weekday() >= 5:
        return False, "weekend"
    if (now_kst.month, now_kst.day) == (12, 31):
        return False, "krx-yearend"  # KRX 연말 휴장
    try:
        import holidays
        if now_kst.date() in holidays.SouthKorea(years=now_kst.year):
            return False, "holiday"
    except Exception:
        pass  # holidays 미설치/실패 시 주말·변경가드로만 동작
    minutes = now_kst.hour * 60 + now_kst.minute
    if minutes < WINDOW_OPEN or minutes > WINDOW_CLOSE:
        return False, "outside-window"
    return True, "ok"


def resolve_symbol(code, market):
    """code+market → 야후 심볼. 한국은 검색으로 .KS/.KQ 정확 해석, 미국은 티커."""
    code = (code or "").strip().upper()
    if not code:
        return None
    if "." in code:
        return code
    if (market or "").upper() == "US":
        return code
    # 한국: 검색 endpoint 로 정확한 거래소 접미사 해석(.KS 무작정 금지).
    try:
        d = _get_json(
            "https://query1.finance.yahoo.com/v1/finance/search?"
            + urllib.parse.urlencode({"q": code, "quotesCount": 8, "newsCount": 0})
        )
    except Exception:
        return None
    fallback = None
    for q in d.get("quotes", []):
        sym = str(q.get("symbol", ""))
        qtype = str(q.get("quoteType", "")).upper()
        exch = str(q.get("exchange", "")).upper()
        if qtype not in ("EQUITY", "ETF"):
            continue
        if not (sym.endswith(".KS") or sym.endswith(".KQ")):
            continue
        if sym.split(".")[0] != code:
            continue
        if exch in ("KSC", "KOE"):
            return sym
        fallback = fallback or sym
    return fallback


def fetch_price_naver(code):
    """네이버 polling API 로 국내 종목 현재가 조회(정규장 기준). 실패 시 None."""
    code = (code or "").strip()
    if not code:
        return None
    try:
        d = _get_json(NAVER_POLL.format(code=code),
                      headers={"Referer": "https://m.stock.naver.com/"})
    except Exception:
        return None
    datas = d.get("datas") or []
    if not datas:
        return None
    raw = str(datas[0].get("closePrice", "")).replace(",", "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def fetch_price_yahoo(symbol):
    """Yahoo chart API 로 현재가 조회. 실패 시 None(클라우드 IP 는 403 가능)."""
    if not symbol:
        return None
    for host in HOSTS:
        try:
            d = _get_json(
                f"https://{host}/v8/finance/chart/{symbol}?range=1d&interval=1d")
            meta = d["chart"]["result"][0]["meta"]
            p = meta.get("regularMarketPrice")
            if p:
                return float(p)
        except Exception:
            continue
    return None


_kis_cfg = None    # 모듈 레벨 캐시
_kis_token = None


def _ensure_kis():
    """KIS 설정/토큰 준비 → (cfg, token) 또는 (None, None)."""
    global _kis_cfg, _kis_token
    if _kis_cfg is None:
        _kis_cfg = _load_kis_config()
    if _kis_cfg and _kis_token is None:
        try:
            _kis_token = _get_kis_token(_kis_cfg)
        except Exception as e:
            print(f"[KIS] 토큰 발급 실패: {e}")
            _kis_token = False  # 이번 실행은 KIS 건너뜀
    if _kis_cfg and _kis_token:
        return _kis_cfg, _kis_token
    return None, None


def fetch_quote(code, market, session_key, session_label, now_iso):
    """현재가 조회 → (price, source, ext, change_pct).
    KR: KIS 통합(UN)=현재가 + 정규장(J)=기준종가로 ext 생성 → 네이버/야후 백업(ext 없음).
    미국 등: Yahoo 단독(ext 없음).
    ext 는 NXT 연장 시세 정보 dict(없으면 None). change_pct 는 전일대비 등락률(없으면 None).
    """
    market = (market or "KR").upper()
    if market == "KR":
        cfg, token = _ensure_kis()
        if cfg and token:
            un, un_chg = fetch_price_kis(code, cfg, token, "UN")  # 통합(KRX+NXT) 현재가·등락률
            reg, _ = fetch_price_kis(code, cfg, token, "J")       # 정규장 종가(기준)
            if un is not None:
                ext = {
                    "venue": "NXT",
                    "label": f"{session_label}(NXT)",
                    "session": session_key,
                    "price": float(un),
                    "asof": now_iso,
                    "basis": "KIS 통합(KRX+NXT) 연장거래 08:00~20:00 실시간, 정규장 종가 대비",
                }
                if reg:
                    ext["ref_close"] = float(reg)
                    ext["delta_pct"] = round((un - reg) / reg * 100, 2)
                return float(un), "kis-nxt", ext, un_chg
        # 네이버 2차(정규장 현재가, ext 없음)
        p = fetch_price_naver(code)
        if p is not None:
            return p, "naver", None, None
        # Yahoo 백업
        sym = resolve_symbol(code, market)
        p = fetch_price_yahoo(sym) if sym else None
        return (p, "yahoo", None, None) if p is not None else (None, None, None, None)
    # 미국 등: Yahoo 단독
    sym = resolve_symbol(code, market)
    p = fetch_price_yahoo(sym) if sym else None
    return (p, "yahoo", None, None) if p is not None else (None, None, None, None)


def main():
    force = "--force" in sys.argv[1:]
    now = datetime.datetime.now(KST)
    ok, reason = is_trading_now(now)
    if not ok and not force:
        print(f"[intraday] skip ({reason}) @ {now.isoformat()}")
        return 0

    now_iso = now.replace(microsecond=0, second=0).isoformat()
    session_key, session_label = session_of(now)

    feed = json.loads(FEED_PATH.read_text(encoding="utf-8-sig"))
    changed = 0
    src_count = {}  # source → 조회 성공 건수(로그용)
    miss = []       # 시세 미확보 종목(로그용)
    seen = {}       # code+market → (price, ext, change_pct) 캐시(중복 종목 1회만 조회)
    for section in ("signals", "observations"):
        for item in feed.get(section, []):
            code = item.get("code", "")
            market = item.get("market", "KR")
            key = f"{market}:{code}"
            if key in seen:
                price, ext, chg = seen[key]
            else:
                price, source, ext, chg = fetch_quote(
                    code, market, session_key, session_label, now_iso)
                seen[key] = (price, ext, chg)
                if price is not None:
                    src_count[source] = src_count.get(source, 0) + 1
                else:
                    miss.append(item.get("name", code))
            if price is None:
                continue
            if item.get("price") != price:
                changed += 1
            item["price"] = price
            if ext is not None:
                item["ext"] = ext
            if chg is not None:
                item["change_pct"] = round(chg, 2)

    # 한국 지수(코스피·코스닥·코스피200) 갱신 — 장중/장외 동안 실시간.
    cfg, token = _ensure_kis()
    if cfg and token:
        idxs = fetch_kr_indices(cfg, token)
        if idxs:
            # 이번에 못 받은 지수는 직전 값을 유지(누락으로 사라지지 않게).
            old_list = (feed.get("kr_context") or {}).get("indices") or []
            old_by = {x.get("symbol"): x for x in old_list}
            new_by = {x["symbol"]: x for x in idxs}
            merged = [new_by.get(sym) or old_by.get(sym)
                      for _, sym, _ in KR_INDEX_LIST]
            merged = [m for m in merged if m]
            if old_list != merged:
                changed += 1
            feed["kr_context"] = {"asof": now_iso, "basis": "한국 지수(KIS 실측)",
                                  "session": session_key, "indices": merged}

    src_log = " ".join(f"{s}:{n}" for s, n in src_count.items()) or "없음"
    if miss:
        print(f"[intraday] 시세 미확보: {', '.join(miss)}")
    if changed == 0:
        print(f"[intraday] 변경 없음 @ {now_iso} (force={force}, 소스 {src_log})")
        return 0

    # 변경 있을 때만 시각·시장상태 갱신.
    korea = feed.setdefault("market_state", {}).setdefault("korea", {})
    basis_by_session = {
        "pre": "장전 NXT 프리마켓(08:00~09:00, 10분 자동갱신)",
        "regular": "정규장 실시간(10분 자동갱신)",
        "after": "장후 NXT 애프터마켓(15:30~20:00, 10분 자동갱신)",
        "closed": "전일 종가(장 마감)",
    }
    status_by_session = {"pre": "pre", "regular": "open", "after": "post", "closed": "closed"}
    korea["status"] = status_by_session.get(session_key, "closed")
    korea["basis"] = basis_by_session.get(session_key, "")
    korea["session"] = session_key
    korea["asof"] = now_iso
    feed["generated_at"] = now_iso

    FEED_PATH.write_text(
        json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[intraday] {changed}개 종목 시세 갱신 @ {now_iso} "
          f"[{session_label}] (소스 {src_log})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
