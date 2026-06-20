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


def fetch_ask_ratio_kis(code, cfg, token):
    """KIS 호가 API(inquire-asking-price-exp-ccn)로 매수/매도호가 총잔량을 조회해
    매수비중(ask_ratio, 0~1)을 계산한다. 반환 (bid_rem, ask_rem, ask_ratio) 또는 None.

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
        return bid, ask, ratio
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
            if (market or "KR").upper() == "KR" and code in kr_chg_naver:
                chg = kr_chg_naver[code]
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
                bid_rem, ask_rem, ratio = ar
                item["ask_ratio"] = ratio
                item["bid_rem"] = bid_rem
                item["ask_rem"] = ask_rem
                item["ask_ratio_asof"] = now_iso
                ask_n += 1
            else:
                for k in ("ask_ratio", "bid_rem", "ask_rem", "ask_ratio_asof"):
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
          f"[{session_label}] (소스 {src_log})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
