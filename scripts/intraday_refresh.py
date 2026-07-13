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

- **호가불균형(orderbook_imbalance)·대량체결(large_trade_*, 2026-07-09 신규)**:
  이 루프(control.json robot.interval_min, 기본 5분)에서 함께 갱신한다. 수급
  (외국인/기관)과 달리 호가·체결테이프는 원천적으로 실시간이라 촘촘히 봐도
  의미가 있다 — analyze_technical.py 의 고정 10분 재발굴과 무관하게 이 5분
  루프에 얹는다. 대상은 이미 선정된 signals+observations(전체종목 풀스캔
  아님)로 한정해 API 레이트리밋을 보호한다. 소스: 호가불균형=KIS(이미 조회된
  bid_rem/ask_rem 재사용, 신규 호출 0회)→토스 폴백, 대량체결=토스 전용(KIS
  inquire-ccnl 은 문서상 가능하나 레이트리밋 보호로 이번엔 미사용).
"""
import base64
import datetime
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

# KIS 초당 호출 제한(유량초과) 회피용 호출 간 최소 간격(초).
KIS_CALL_GAP = 0.25

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = REPO_ROOT / "feed.json"
CONTROL_PATH = REPO_ROOT / "control.json"
KIS_CONFIG_PATH = REPO_ROOT / "config" / "kis_config.json"
KIS_TOKEN_PATH = REPO_ROOT / "config" / ".kis_token.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
NAVER_POLL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
KIS_BASE = "https://openapi.koreainvestment.com:9443"

# NXT 연장거래 운영시간(KST, 분 단위). 프리 08:00~09:00 / 정규 09:00~15:30 / 애프터 15:30~20:00.
# 기본값 — control.json(robot.window_start/end·interval_min)이 있으면 그 값으로 덮어쓴다.
WINDOW_OPEN = 8 * 60          # 08:00
WINDOW_CLOSE = 20 * 60        # 20:00
REGULAR_OPEN = 9 * 60         # 09:00
REGULAR_CLOSE = 15 * 60 + 30  # 15:30
INTERVAL_MIN = 10             # 갱신 간격(분) — 이 배수의 분에만 동작


def _hhmm_to_min(s, default):
    """'HH:MM' → 분. 실패 시 default."""
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default


def load_control():
    """control.json(앱·PC가 설정) 로드 → 로봇 운영값 반영. 없으면 기본값.
    반환: (window_open, window_close, interval_min)."""
    wo, wc, iv = WINDOW_OPEN, WINDOW_CLOSE, INTERVAL_MIN
    try:
        if CONTROL_PATH.exists():
            c = json.loads(CONTROL_PATH.read_text(encoding="utf-8-sig"))
            r = c.get("robot", {}) or {}
            wo = _hhmm_to_min(r.get("window_start"), WINDOW_OPEN)
            wc = _hhmm_to_min(r.get("window_end"), WINDOW_CLOSE)
            iv = int(r.get("interval_min", INTERVAL_MIN) or INTERVAL_MIN)
            if iv < 1:
                iv = INTERVAL_MIN
    except Exception as e:
        print(f"[control] 읽기 실패, 기본값 사용: {e}")
    return wo, wc, iv


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
    # ★발급은 전용 워크플로(KIS_TOKEN_ISSUE=1)에서만★ — 그 외엔 캐시 없으면 발급하지
    # 않고 None 반환(호출부가 네이버로 폴백). 서버 cron마다 토큰 재발급 = KIS 발급
    # SMS 폭탄을 막는다(앱과 동일 원칙 — 발급은 하루 1회 전용 잡에서만).
    if os.environ.get("KIS_TOKEN_ISSUE") != "1":
        return None
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

# 네이버 지수 폴백 — 클라우드에서 KIS 없이도 지수를 갱신(KIS 장애·미설정 대비).
# SERVICE_INDEX 폴링: nv=지수×100, cr=등락률%. KPI200=코스피200.
NAVER_INDEX_POLL = ("https://polling.finance.naver.com/api/realtime"
                    "?query=SERVICE_INDEX:KOSPI,KOSDAQ,KPI200")
NAVER_INDEX_MAP = {"KOSPI": ("코스피", "KOSPI"),
                   "KOSDAQ": ("코스닥", "KOSDAQ"),
                   "KPI200": ("코스피200", "KOSPI200")}


def _sign_cr(cr, rf):
    """네이버 rf(등락 방향코드)로 등락률 부호 확정. 1=상한·2=상승 → +, 5=하락·
    4=하한 → −, 3=보합 → 0. 그 외/미상은 원값 유지(이미 부호가 있을 수 있음).
    마감 후 cr 이 부호 없이 크기만 오는 케이스(방향 뒤집힘)를 막는다(INC-002)."""
    mag = abs(cr)
    rf = str(rf).strip()
    if rf in ("1", "2"):
        return mag
    if rf in ("4", "5"):
        return -mag
    if rf == "3":
        return 0.0
    return cr


def fetch_kr_indices_naver():
    """네이버 SERVICE_INDEX 폴링으로 코스피·코스닥·코스피200 현재지수·등락률 조회.
    KIS 폴백용(클라우드 동작·키 불필요). nv 는 ×100 정수라 /100 한다. 실패 시 []."""
    try:
        d = _get_json(NAVER_INDEX_POLL,
                      headers={"Referer": "https://m.stock.naver.com/"})
    except Exception:
        return []
    out = []
    areas = ((d.get("result") or {}).get("areas") or [{}])
    datas = (areas[0].get("datas") if areas else []) or []
    for it in datas:
        cd = it.get("cd")
        if cd not in NAVER_INDEX_MAP:
            continue
        nv = it.get("nv")
        if nv is None:
            continue
        cr = it.get("cr")
        name, sym = NAVER_INDEX_MAP[cd]
        try:
            # ★정합성★ 네이버는 마감 후 cr 을 부호 없이 크기만 주기도 한다. 방향은
            # rf(2=상승·3=보합·5=하락 등)에 있으므로 rf 로 부호를 확정한다(INC-002).
            chg = _sign_cr(float(cr), it.get("rf")) if cr is not None else 0.0
            out.append({"name": name, "symbol": sym,
                        "price": round(float(nv) / 100.0, 2),
                        "change_pct": round(chg, 2)})
        except (TypeError, ValueError):
            continue
    return out


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


def fetch_kr_changes_naver(codes):
    """KR 6자리 코드들의 전일대비 등락률(%)을 네이버 polling 배치로 조회한다.

    ★정합성(INC-002·004)★ 부호는 `cr`(마감 후 크기만 옴)이 아니라 `rf`로 확정하고,
    응답이 **EUC-KR**(종목명)이라 utf-8→cp949 순으로 안전 디코딩한다. 이 값을 KR
    종목 change_pct 의 권위값으로 써서 KIS 프리장 0.0/스테일을 대체한다.
    {code: change_pct}. 실패 코드는 생략."""
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
                "User-Agent": UA, "Referer": "https://m.stock.naver.com/"})
            raw = urllib.request.urlopen(req, timeout=10).read()
            d = None
            for enc in ("utf-8", "cp949"):
                try:
                    d = json.loads(raw.decode(enc))
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
            print(f"[intraday] 네이버 등락률 배치 실패({chunk[:3]}…): {ex}")
            continue
    return out


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


def _to_int(v):
    """쉼표 포함 숫자 문자열 → int. 실패 시 None."""
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _to_float(v):
    """쉼표 포함 숫자 문자열 → float. 실패/빈값 시 None(0.0 날조 금지)."""
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s not in ("", "None") else None
    except (TypeError, ValueError):
        return None


def fetch_ask_ratio_kis(code, cfg, token):
    """KIS 호가 API(inquire-asking-price-exp-ccn)로 매수/매도호가 총잔량을 조회해
    매수비중(ask_ratio, 0~1)을 계산한다.
    반환 (bid_rem, ask_rem, ask_ratio, best_bid, best_ask) 또는 None
    (best_bid/best_ask 는 대량체결 방향판정용 매수/매도 1호가 — 파싱 실패 시 None 가능).

    ★정합성★ 합이 0이거나 비율이 0~1 밖이면 데이터 오류로 폐기(None) — 날조 금지.
    호가는 장중에만 유효하므로 호출부(fetch_ask_ratio)가 장중에만 부른다.
    토큰은 호출부가 넘긴 캐시 토큰만 사용 — 여기서 새로 발급하지 않는다.
    """
    tr_id = "FHKST01010200"  # 주식현재가 호가/예상체결(실전·모의 공통)
    time.sleep(KIS_CALL_GAP)  # 초당 호출 제한 회피
    params = urllib.parse.urlencode({
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
    })
    req = urllib.request.Request(
        f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/"
        f"inquire-asking-price-exp-ccn?{params}",
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
        o = data.get("output1", {}) or {}
        bid = _to_int(o.get("total_bidp_rsqn"))  # 매수호가 총잔량
        ask = _to_int(o.get("total_askp_rsqn"))  # 매도호가 총잔량
        if bid is None or ask is None:
            return None
        tot = bid + ask
        if tot <= 0:
            return None  # 합 0(장 시작 전/데이터 없음) → 폐기
        ratio = round(bid / tot, 4)  # 매수비중(>0.5=매수우위)
        if not (0.0 <= ratio <= 1.0):
            return None  # 이상치 폐기
        # 매도호가1(askp1)·매수호가1(bidp1) — 대량체결 방향판정(Lee-Ready)에 재사용
        # (신규 API 호출 없이 이미 받은 응답에서 파생). 파싱 실패해도 ask_ratio 자체는
        # 유효하니 best_bid/best_ask 는 None 으로 두고 계속 진행.
        best_bid = _to_float(o.get("bidp1"))
        best_ask = _to_float(o.get("askp1"))
        return bid, ask, ratio, best_bid, best_ask
    except Exception:
        return None


def fetch_ask_ratio(code, market, cfg, token, trading_open):
    """호가 매수비중 조회 — KR·장중·KIS 토큰이 모두 있을 때만. 그 외 None(생략).
    네이버 polling엔 호가 잔량 필드가 없어 KIS 호가 API로만 채운다."""
    if (market or "KR").upper() != "KR":
        return None
    if not trading_open:
        return None  # 마감 후엔 호가 무의미 → 생략(직전값도 쓰지 않음)
    if not (cfg and token):
        return None
    return fetch_ask_ratio_kis(code, cfg, token)


# ═════════════════════════════════════════════════════════════════════════
# 토스증권(Open API) — 호가불균형 폴백 + 대량체결(비정상적으로 큰 단일 체결) 감지
# (2026-07-09 신규, [[toss-api-integration]])
#
# 토스 토큰은 "클라이언트당 활성 토큰 1개"만 유효해 재발급 시 이전 토큰이 즉시
# 무효화된다(SMS 는 없음). 로컬 PC(scripts/compare_quotes.py, 동일 client_id 사용)와
# 계정을 공유하므로 서로의 토큰을 무효화할 수 있다 — 완화책: ①디스크 캐시를
# GitHub Actions cache 로 날짜 키 유지해 job 재시작마다 재발급하지 않음(KIS 토큰
# 캐시와 동일 패턴) ②401 을 받으면 딱 1회만 force 재발급 후 재시도(연쇄 재발급
# 금지) ③이 신호는 두 소스 중 하나일 뿐이라 토스가 막혀도 KIS/미가용으로 조용히
# 폴백한다(전체 스크립트를 막지 않음).
TOSS_BASE = "https://openapi.tossinvest.com"
TOSS_CONFIG_PATH = REPO_ROOT / "config" / "toss_config.json"
TOSS_TOKEN_PATH = REPO_ROOT / "config" / ".toss_token.json"
TOSS_CALL_GAP = 0.15           # 토스 호출 간 최소 간격(초) — 레이트리밋 보호
LARGE_TRADE_MULTIPLE = 5.0      # 최근 체결 중앙값 대비 이 배수 이상이면 '대량체결' 후보
LARGE_TRADE_MIN_SAMPLE = 10     # 중앙값 계산에 필요한 최소 체결 건수(부족하면 판정 보류)
LARGE_TRADE_LOOKBACK_MIN = 5    # 대량체결로 인정할 최신성(분) — 이보다 오래되면 무시
# ★실측 캘리브레이션(2026-07-09, 관심종목 38개 실측)★ 배수(multiple)만으로 판정하면
# 오탐이 매우 많다 — KRX 체결은 1~5주 단위 잘게 쪼갠 틱이 흔해 중앙값이 1~5주 수준으로
# 낮고, 그러면 평범한 20~100주 체결도 손쉽게 수십~수백 배로 찍힌다(1차 시도 5배 임계값
# 으로는 38종목 중 30종목이 매 스캔마다 '대량체결'로 찍혀 신호 변별력이 사실상 없었음).
# 그래서 배수 조건에 더해 '체결대금(가격×수량)'이 최소값 이상일 때만 대량체결로
# 확정한다 — 000660(193배·8.4억원)·007390(2,845배·2.4억원)처럼 실제 의미있는 단일
# 체결만 남기고, 069540(65배·313만원)처럼 배수만 큰데 대금이 작은 저가주 노이즈는
# 폐기한다.
# ★2026-07-09 재조정★ 자동매매 실행(Dart 앱) 쪽 대량체결 판정과 민감도를 맞추기
# 위해 배수 임계값을 8배→5배로 낮췄다(사용자 지시). 배수만 5배였던 1차 시도는
# 오탐 30/38 이었지만, 그건 금액 조건 없이 배수만으로 판정했을 때 얘기다. 지금은
# 금액 조건(체결대금 ≥ LARGE_TRADE_MIN_VALUE_KRW, 1억원)이 함께 AND 로 걸려 있어
# 상황이 다르다 — 저가주 소량 체결이 수십~수백 배로 찍혀도 대금이 작으면 금액
# 조건에서 걸러진다. 재조정 시점 재확인 결과는 커밋 로그·시세 검증 보고 참고.
# signal-selector/사용자가 며칠 운용 후 과다/과소 탐지가 확인되면 이 두 상수를
# 다시 조정할 것.
LARGE_TRADE_MIN_VALUE_KRW = 100_000_000  # 최소 체결대금(원) — 1억원 미만은 폐기


def _load_toss_config():
    """토스 Open API 설정 로드 — 환경변수(GitHub Actions secrets) 우선, 없으면
    로컬 파일(config/toss_config.json, gitignore됨·PC 로컬 테스트용)."""
    cid = os.environ.get("TOSS_CLIENT_ID")
    csec = os.environ.get("TOSS_CLIENT_SECRET")
    if (not cid or not csec) and TOSS_CONFIG_PATH.exists():
        try:
            fcfg = json.loads(TOSS_CONFIG_PATH.read_text(encoding="utf-8"))
            cid = cid or fcfg.get("client_id")
            csec = csec or fcfg.get("client_secret")
        except Exception:
            pass
    if not cid or not csec:
        return None
    return {"client_id": cid, "client_secret": csec}


def _get_toss_token(cfg, force=False):
    """토스 액세스 토큰 취득 — 디스크 캐시 재사용(만료 60초 여유), force=True 이거나
    만료됐을 때만 재발급. 재발급은 이전 토큰을 무효화하므로 호출부가 남발하지 않아야
    한다(호출부: _ensure_toss 1회, _toss_get 의 401 복구 1회뿐)."""
    now_ts = datetime.datetime.now(KST).timestamp()
    if not force and TOSS_TOKEN_PATH.exists():
        try:
            cached = json.loads(TOSS_TOKEN_PATH.read_text(encoding="utf-8"))
            if float(cached.get("expire", 0)) - now_ts > 60:
                return cached["access_token"]
        except Exception:
            pass
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        f"{TOSS_BASE}/oauth2/token", data=body, method="POST",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
    tok = d["access_token"]
    exp = now_ts + float(d.get("expires_in", 3600)) - 60
    TOSS_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOSS_TOKEN_PATH.write_text(
        json.dumps({"access_token": tok, "expire": exp}), encoding="utf-8")
    return tok


_ccnl_debug_printed = False  # KIS inquire-ccnl 필드명 1회성 검증 로그 출력 여부(임시)
_toss_cfg = None     # 모듈 레벨 캐시(False=설정 없음 확인됨, None=미확인)
_toss_token = None   # False=이번 실행 발급 실패(재시도 안 함), None=미확인


def _ensure_toss():
    """토스 설정/토큰 준비 → (cfg, token) 또는 (None, None). 실패해도 조용히
    생략(토스는 두 신호의 소스 중 하나일 뿐 — 없으면 KIS/미가용으로 폴백)."""
    global _toss_cfg, _toss_token
    if _toss_cfg is None:
        _toss_cfg = _load_toss_config() or False
    if _toss_cfg and _toss_token is None:
        try:
            _toss_token = _get_toss_token(_toss_cfg)
        except Exception as e:
            print(f"[토스] 토큰 발급 실패: {e}")
            _toss_token = False
    if _toss_cfg and _toss_token:
        return _toss_cfg, _toss_token
    return None, None


def _toss_get(path, token):
    """토스 GET 요청 — 401 이면 토큰을 딱 1회만 force 재발급해 재시도(연쇄 방지)."""
    global _toss_token

    def _do(tok):
        req = urllib.request.Request(
            f"{TOSS_BASE}{path}", headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    try:
        return _do(token)
    except urllib.error.HTTPError as e:
        if e.code == 401 and _toss_cfg:
            try:
                new_tok = _get_toss_token(_toss_cfg, force=True)
                _toss_token = new_tok
                return _do(new_tok)
            except Exception:
                return None
        return None
    except Exception:
        return None


def fetch_orderbook_toss(code, token):
    """토스 호가 조회(GET /api/v1/orderbook?symbol=) — 매도/매수 각 10단계 합.
    반환: {'best_ask','best_bid','ask_sum','bid_sum','timestamp'} 또는 None."""
    time.sleep(TOSS_CALL_GAP)
    d = _toss_get(f"/api/v1/orderbook?{urllib.parse.urlencode({'symbol': code})}", token)
    if not d:
        return None
    res = d.get("result") or {}
    asks = res.get("asks") or []
    bids = res.get("bids") or []
    if not asks or not bids:
        return None
    try:
        ask_sum = sum(int(a["volume"]) for a in asks)
        bid_sum = sum(int(b["volume"]) for b in bids)
        best_ask = float(asks[0]["price"])
        best_bid = float(bids[0]["price"])
    except (KeyError, TypeError, ValueError):
        return None
    if ask_sum <= 0 and bid_sum <= 0:
        return None  # 합 0(데이터 없음) → 폐기(날조 금지)
    return {"best_ask": best_ask, "best_bid": best_bid,
            "ask_sum": ask_sum, "bid_sum": bid_sum,
            "timestamp": res.get("timestamp")}


def fetch_trades_toss(code, token, count=50):
    """토스 최근 체결테이프 조회(GET /api/v1/trades?symbol=) — 최신순 최대 count건.
    반환: [{'price','volume','timestamp'}, ...] 또는 None."""
    time.sleep(TOSS_CALL_GAP)
    d = _toss_get(
        f"/api/v1/trades?{urllib.parse.urlencode({'symbol': code, 'count': count})}", token)
    if not d:
        return None
    res = d.get("result")
    if not isinstance(res, list) or not res:
        return None
    out = []
    for t in res:
        try:
            out.append({"price": float(t["price"]), "volume": int(t["volume"]),
                        "timestamp": t.get("timestamp")})
        except (KeyError, TypeError, ValueError):
            continue
    return out or None


def fetch_trades_kis(code, cfg, token):
    """KIS OpenAPI 주식현재가 체결(inquire-ccnl, tr_id=FHKST01010300)로 최근 체결틱
    (최근 30건 내외)을 조회한다 — 대량체결 감지의 1차 소스(2026-07-13 신규).

    ★배경★ 토스 체결테이프(fetch_trades_toss)는 GitHub Actions(클라우드 IP)에서
    OAuth 토큰 발급이 항상 403 Forbidden 으로 막혀(로컬 PC 에선 동일 키로 정상 동작 —
    IP 기반 차단으로 추정) large_trade_* 필드가 배포 이후 단 한 번도 채워지지 못했다
    (실측: 2026-07-09~07-13 feed.json 전체에 large_trade_detected 0건). KIS 는 이미
    같은 환경에서 시세·호가(ask_ratio)에 정상 동작 중이므로 이를 1차 소스로 쓴다.
    토스는 IP 차단이 풀리는 경우를 대비해 2차(폴백)로 유지.

    토큰은 호출부가 넘긴 캐시 토큰만 사용(여기서 신규 발급 없음 — KIS 토큰 중복발급
    금지 불변식 준수). 반환: [{'price','volume','timestamp'}, ...] 또는 None.
    """
    tr_id = "FHKST01010300"  # 주식현재가 체결(최근 체결틱)
    time.sleep(KIS_CALL_GAP)
    params = urllib.parse.urlencode({
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
    })
    req = urllib.request.Request(
        f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-ccnl?{params}",
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
    except Exception:
        return None
    rows = data.get("output") or []
    global _ccnl_debug_printed
    if rows and not _ccnl_debug_printed:
        # ★임시 검증 로그(2026-07-13)★ inquire-ccnl 실응답 필드명을 실측 확인하기
        # 위한 1회성 출력 — 검증 완료 후 제거 예정(data-collector 재검증 시 삭제).
        print(f"[debug-ccnl] {code} raw row0: {json.dumps(rows[0], ensure_ascii=False)}")
        _ccnl_debug_printed = True
    if not isinstance(rows, list) or not rows:
        return None
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    out = []
    for row in rows:
        try:
            price = _to_float(row.get("stck_prpr"))
            # 체결량(건별 틱 거래량) 필드명이 문서/버전에 따라 다를 수 있어 후보를
            # 순서대로 시도(누적거래량 acml_vol 은 의미가 달라 후보에서 제외 —
            # 잘못 섞으면 배수가 완전히 틀어진다). 전부 실패하면 이 건은 버린다.
            vol_raw = (row.get("cntg_vol") or row.get("cntg_qty")
                       or row.get("cntg_vol_cnt"))
            vol = _to_int(vol_raw)
            hhmmss = str(row.get("stck_cntg_hour", "")).strip().zfill(6)
            if price is None or vol is None or price <= 0 or vol <= 0:
                continue
            ts = None
            if len(hhmmss) == 6 and hhmmss.isdigit():
                ts = f"{today}T{hhmmss[0:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}+09:00"
            out.append({"price": price, "volume": vol, "timestamp": ts})
        except (TypeError, ValueError):
            continue
    return out or None


def detect_large_trade(trades, ob, now_kst):
    """최근 체결테이프에서 비정상적으로 큰 단일 체결(대량체결)을 찾는다.

    판정(AND 조건 — 실측 캘리브레이션 반영):
      ①배수: 최근 체결 중앙값(대상 자신 제외) 대비 LARGE_TRADE_MULTIPLE(기본 5배) 이상
      ②체결대금: 가격×수량 ≥ LARGE_TRADE_MIN_VALUE_KRW(기본 1억원) — 저가주 잔량
        노이즈(수백 배수인데 대금은 수백만원인 오탐)를 걸러낸다.
      ③최신성: 체결 시각이 LARGE_TRADE_LOOKBACK_MIN(기본 5분) 이내.
    셋 다 만족해야 detected=True. 방향은 그 체결가를 동시 조회한 호가창의 최우선
    매도/매수호가와 비교하는 Lee-Ready 방식으로 판별한다: 체결가>=매도1호가 →
    적극매수(buy), 체결가<=매수1호가 → 적극매도(sell), 그 사이(스프레드 내부)면
    방향 불명(None).

    표본이 LARGE_TRADE_MIN_SAMPLE 미만이면 판정을 보류한다(None — 날조 금지).
    """
    if not trades or len(trades) < LARGE_TRADE_MIN_SAMPLE:
        return None
    vols = [t.get("volume") for t in trades if t.get("volume") is not None]
    if len(vols) < LARGE_TRADE_MIN_SAMPLE:
        return None
    max_idx = max(range(len(trades)), key=lambda i: trades[i].get("volume", 0))
    max_t = trades[max_idx]
    base_vols = vols[:max_idx] + vols[max_idx + 1:]
    if not base_vols:
        return None
    median_vol = statistics.median(base_vols)
    if median_vol <= 0:
        return None
    multiple = round(max_t["volume"] / median_vol, 2)
    value_krw = round(max_t["volume"] * max_t["price"])
    detected = multiple >= LARGE_TRADE_MULTIPLE and value_krw >= LARGE_TRADE_MIN_VALUE_KRW

    # 최신성 확인 — 오래된 대량체결을 '지금 감지'로 오표기하지 않는다.
    ts_raw = max_t.get("timestamp")
    fresh = True
    if ts_raw:
        try:
            ts = datetime.datetime.fromisoformat(str(ts_raw))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=KST)
            age_min = (now_kst - ts).total_seconds() / 60.0
            fresh = -1 <= age_min <= LARGE_TRADE_LOOKBACK_MIN  # -1: 시계오차 여유
        except Exception:
            fresh = True  # 파싱 실패는 최신성 판정 보류(과도한 폐기 방지)
    if not fresh:
        detected = False

    direction = None
    if ob and detected:
        if max_t["price"] >= ob["best_ask"]:
            direction = "buy"
        elif max_t["price"] <= ob["best_bid"]:
            direction = "sell"

    return {
        "detected": bool(detected),
        "direction": direction,
        "multiple": multiple,
        "value_krw": value_krw,
        "asof": ts_raw or now_kst.replace(microsecond=0).isoformat(),
    }


def fetch_microstructure(code, market, trading_open, now_kst, kis_bid_rem, kis_ask_rem,
                          kis_cfg=None, kis_token=None, kis_best_bid=None, kis_best_ask=None):
    """호가불균형(orderbook_imbalance)·대량체결(large_trade_*) 통합 조회.

    소스 우선순위 — 호가불균형: ①KIS(이미 조회된 bid_rem/ask_rem 재사용, 신규 API
    호출 0회) ②토스(호가 10단계 합, 신규 호출) ③미가용.
    대량체결: ①KIS(inquire-ccnl, FHKST01010300 — 이미 조회된 ask_ratio 호출의
    askp1/bidp1 을 방향판정에 재사용, 신규 호출은 체결틱 1건뿐) ②토스(체결테이프)
    ③미가용.

    ★2026-07-13 변경★ 대량체결을 토스 전용으로 뒀던 최초 구현(2026-07-09)이
    배포 이후 feed.json 에 단 한 번도 값을 채우지 못한 게 확인됐다 — 토스 OAuth
    토큰 발급이 GitHub Actions(클라우드 IP)에서 항상 403 Forbidden(로컬 PC 는 동일
    키로 정상 동작 — IP 기반 차단으로 추정). KIS 는 같은 환경에서 시세·호가가 이미
    정상 동작 중이므로 대량체결도 KIS 를 1차로 승격했다. 토스는 IP 차단이 풀릴
    경우를 대비한 2차 폴백으로 유지(신규 호출 비용 있음 — 실패해도 조용히 생략).

    반환: (imbalance 또는 None, imb_asof 또는 None, imb_source, large_trade dict 또는 None)
    imb_source 는 항상 "kis"|"toss"|"unavailable" 중 하나.
    """
    if (market or "KR").upper() != "KR" or not trading_open:
        return None, None, "unavailable", None

    imb, imb_source = None, None
    if kis_bid_rem is not None and kis_ask_rem is not None and (kis_bid_rem + kis_ask_rem) > 0:
        imb = round((kis_bid_rem - kis_ask_rem) / (kis_bid_rem + kis_ask_rem), 4)
        imb_source = "kis"

    large_trade = None
    if kis_cfg and kis_token:
        trades = fetch_trades_kis(code, kis_cfg, kis_token)
        if trades:
            ob = None
            if kis_best_bid is not None and kis_best_ask is not None:
                ob = {"best_bid": kis_best_bid, "best_ask": kis_best_ask}
            large_trade = detect_large_trade(trades, ob, now_kst)

    if large_trade is None or imb is None:
        cfg, token = _ensure_toss()
        if cfg and token:
            ob = fetch_orderbook_toss(code, token)
            if imb is None and ob and (ob["ask_sum"] + ob["bid_sum"]) > 0:
                imb = round((ob["bid_sum"] - ob["ask_sum"]) / (ob["bid_sum"] + ob["ask_sum"]), 4)
                imb_source = "toss"
            if large_trade is None:
                trades = fetch_trades_toss(code, token)
                large_trade = detect_large_trade(trades, ob, now_kst)

    if imb is None:
        imb_source = "unavailable"
    imb_asof = now_kst.replace(microsecond=0, second=0).isoformat() if imb is not None else None
    return imb, imb_asof, imb_source, large_trade


def main():
    global WINDOW_OPEN, WINDOW_CLOSE
    force = "--force" in sys.argv[1:]
    now = datetime.datetime.now(KST)

    # control.json(앱·PC 설정) 반영 — 운영창·갱신간격.
    WINDOW_OPEN, WINDOW_CLOSE, interval_min = load_control()
    # 갱신 간격: cron 은 10분마다 깨우지만, interval_min 배수 분에만 실제 동작.
    if not force and (now.hour * 60 + now.minute) % interval_min != 0:
        print(f"[intraday] skip (off-interval {interval_min}m) @ {now.isoformat()}")
        return 0

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

    # ★정합성(INC-002·004)★ KR 종목 등락률은 네이버(cr+rf, 전일대비)를 권위값으로
    # 쓴다 — KIS 통합(UN)은 프리장에 0.0/스테일이라 feed 등락이 멈춰 보였다. 한 번
    # 배치로 받아 아래 루프에서 KR 종목 change_pct 를 덮어쓴다(실패분은 KIS 값 유지).
    kr_codes = [it.get("code", "")
                for sec in ("signals", "observations")
                for it in feed.get(sec, [])
                if (it.get("market", "KR") or "KR").upper() == "KR"]
    kr_chg_naver = fetch_kr_changes_naver(kr_codes)
    if kr_chg_naver:
        print(f"[intraday] KR 등락률 네이버 보정(rf 부호): {len(kr_chg_naver)}종목")

    # ── 호가 매수/매도 잔량 비율(ask_ratio) ──────────────────────────────────
    # 네이버 polling엔 호가 잔량이 없어 KIS 호가 API(캐시 토큰)로만 채운다. KR·장중·
    # KIS 토큰이 모두 있을 때만 조회하고, 없음/합0/범위밖이면 생략(직전값도 제거 —
    # 마감/미확보 시 스테일 호가 금지). 토큰은 캐시만 재사용(새 발급 없음).
    kis_cfg_h, kis_tok_h = _ensure_kis()
    ask_seen = {}
    ask_n = 0
    micro_seen = {}  # 호가불균형·대량체결 종목당 1회 캐시(key → (imb, asof, source, large_trade))
    micro_n = 0

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
            # KR 종목은 네이버 권위 등락률로 교체(있을 때). 미국 등은 KIS/야후 chg 유지.
            # ★프리마켓 0.00 오표기 방지(2026-07-03)★ 네이버 SERVICE_ITEM 은 KRX
            # 정규장(09:00) 개장 전엔 라이브 틱이 없어 전일종가 그대로에 cr=0 을
            # 고정 출력한다 — 08:00~09:00 프리마켓에 관심종목 전체가 동시에
            # change_pct=0.00 으로 찍히는 사고(실측 '변동없음'이 아닌 '미확보'를
            # 0 으로 날조)를 막는다. 이 시간대 네이버 값이 정확히 0.0 이면: KIS
            # UN(NXT) 실측 chg 가 이미 있으면 그걸 유지, 없으면 change_pct 자체를
            # 지워(null) 미확보로 남긴다. 09:00 이후엔 실제 보합(0.00)도 있으니
            # 가드하지 않는다.
            _premkt = session_key == "pre"
            if (market or "KR").upper() == "KR" and code in kr_chg_naver:
                nchg = kr_chg_naver[code]
                if _premkt and nchg == 0.0:
                    if not (chg is not None and chg != 0.0):
                        chg = None  # 미확보 — 기존 change_pct 제거(날조 금지)
                        item.pop("change_pct", None)
                else:
                    chg = nchg
            if price is None:
                continue
            if item.get("price") != price:
                changed += 1
            item["price"] = price
            if ext is not None:
                item["ext"] = ext
            if chg is not None:
                item["change_pct"] = round(chg, 2)
            # 호가 매수비중(ask_ratio) — KR·장중·KIS 토큰 있을 때만. 그 외엔 직전값
            # 제거(스테일 호가 금지). 종목당 1회만 조회(seen 캐시).
            if key in ask_seen:
                ar = ask_seen[key]
            else:
                ar = fetch_ask_ratio(code, market, kis_cfg_h, kis_tok_h, ok)
                ask_seen[key] = ar
            if ar is not None:
                bid_rem, ask_rem, ratio, best_bid, best_ask = ar
                item["ask_ratio"] = ratio
                item["bid_rem"] = bid_rem
                item["ask_rem"] = ask_rem
                item["ask_ratio_asof"] = now_iso
                ask_n += 1
            else:
                bid_rem, ask_rem, best_bid, best_ask = None, None, None, None
                for k in ("ask_ratio", "bid_rem", "ask_rem", "ask_ratio_asof"):
                    item.pop(k, None)

            # 호가불균형(orderbook_imbalance, -1~+1)·대량체결(large_trade_*) —
            # 종목당 1회만 조회(micro_seen 캐시). 호가불균형은 KIS bid_rem/ask_rem 이
            # 있으면 신규 호출 없이 파생(1차), 없으면 토스로 폴백(2차). 대량체결은
            # KIS 체결틱(inquire-ccnl) 1차·토스 체결테이프 2차(2026-07-13, 토스가
            # GitHub Actions IP 에서 상시 403 이라 KIS 로 승격 — fetch_microstructure
            # 참조). 둘 다 KR·장중 전용 — 그 외엔 직전값 제거(스테일 금지).
            if key in micro_seen:
                imb, imb_asof, imb_source, lt = micro_seen[key]
            else:
                imb, imb_asof, imb_source, lt = fetch_microstructure(
                    code, market, ok, now, bid_rem, ask_rem,
                    kis_cfg_h, kis_tok_h, best_bid, best_ask)
                micro_seen[key] = (imb, imb_asof, imb_source, lt)
                if imb is not None:
                    micro_n += 1
            if imb is not None:
                # 허수호가 급변 완화(과도한 복잡화는 피하되 최소 신호는 남김):
                # 직전 조회값 대비 급변(스윙>1.0, 즉 부호가 반대로 크게 뒤집힘)
                # 이면 orderbook_imbalance_stable=False 로 신뢰도 힌트만 남긴다
                # (값 자체는 날조 없이 실측 그대로 반영).
                prev_imb = item.get("orderbook_imbalance")
                item["orderbook_imbalance"] = imb
                item["orderbook_imbalance_asof"] = imb_asof
                item["orderbook_imbalance_source"] = imb_source
                item["orderbook_imbalance_stable"] = (
                    prev_imb is None or abs(imb - prev_imb) <= 1.0)
            else:
                item["orderbook_imbalance_source"] = imb_source or "unavailable"
                for k in ("orderbook_imbalance", "orderbook_imbalance_asof",
                          "orderbook_imbalance_stable"):
                    item.pop(k, None)
            if lt is not None:
                item["large_trade_detected"] = lt["detected"]
                item["large_trade_direction"] = lt["direction"]
                item["large_trade_multiple"] = lt["multiple"]
                item["large_trade_value_krw"] = lt["value_krw"]
                item["large_trade_asof"] = lt["asof"]
            else:
                for k in ("large_trade_detected", "large_trade_direction",
                          "large_trade_multiple", "large_trade_value_krw",
                          "large_trade_asof"):
                    item.pop(k, None)

    # 한국 지수(코스피·코스닥·코스피200) 갱신 — 장중/장외 동안 실시간.
    # KIS(고정밀) 우선, 실패/미설정이면 **네이버 지수 폴백**으로 갱신한다(cron 누락·
    # KIS 장애로 지수가 며칠씩 스테일되던 문제 방지 — KIS 없이도 항상 최신).
    cfg, token = _ensure_kis()
    idxs, idx_src = [], ""
    if cfg and token:
        idxs = fetch_kr_indices(cfg, token)
        if idxs:
            idx_src = "KIS"
    if not idxs:
        idxs = fetch_kr_indices_naver()
        if idxs:
            idx_src = "네이버"
    idx_fetched = False  # 이번 실행에서 지수를 실측으로 받았나(받았으면 신선도 갱신 보장)
    if idxs:
        idx_fetched = True
        # 이번에 못 받은 지수는 직전 값을 유지(누락으로 사라지지 않게).
        old_list = (feed.get("kr_context") or {}).get("indices") or []
        old_by = {x.get("symbol"): x for x in old_list}
        new_by = {x["symbol"]: x for x in idxs}
        # ★거짓 신선도 방지(INC-005)★ 일부를 직전값으로 메우면 전체 asof=now 로
        # 찍지 말고 stale=True 로 표시한다(어제 지수+오늘 asof 금지).
        merged, used_old = [], False
        for _, sym, _ in KR_INDEX_LIST:
            if sym in new_by:
                merged.append(new_by[sym])
            elif sym in old_by:
                merged.append(old_by[sym])
                used_old = True
        if old_list != merged:
            changed += 1
        feed["kr_context"] = {"asof": now_iso,
                              "basis": f"한국 지수({idx_src} 실측)",
                              "session": session_key,
                              "stale": used_old, "indices": merged}

    src_log = " ".join(f"{s}:{n}" for s, n in src_count.items()) or "없음"
    if miss:
        print(f"[intraday] 시세 미확보: {', '.join(miss)}")
    # ★정합성 신선도 보장(INC-007)★ 지수를 실측했으면 종목 가격이 안 변했어도
    # market_state.korea / kr_context.asof 를 반드시 신선화·기록한다. 이전엔 종목
    # changed==0 이면 여기서 early-return 해 메모리상 갱신한 지수/시장상태가 통째로
    # 버려졌고(파일 미기록), 결국 market_state.korea 가 며칠씩 옛 시각에 멈췄다.
    if changed == 0 and not idx_fetched:
        print(f"[intraday] 변경 없음 @ {now_iso} (force={force}, 소스 {src_log})")
        return 0

    # 변경 또는 지수 실측 시 시각·시장상태 갱신.
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
    # 예약/전체 분석 시각(analyzed_at)은 시세 갱신에 덮어쓰지 않는다(앱 '예약분석 경과'
    # 표기용). 아직 없으면(구버전 feed) 직전 generated_at 으로 1회 보정한다.
    feed.setdefault("analyzed_at", feed.get("generated_at", now_iso))
    feed["generated_at"] = now_iso

    FEED_PATH.write_text(
        json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[intraday] {changed}개 종목 시세 갱신 @ {now_iso} "
          f"[{session_label}] (소스 {src_log}, 호가비중 {ask_n}건, "
          f"호가불균형/대량체결 {micro_n}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
