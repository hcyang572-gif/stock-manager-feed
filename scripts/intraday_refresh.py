#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
장중 경량 시세 갱신 — feed.json 의 신호/관찰 종목 현재가만 야후에서 다시 받아 갱신.

- 신규 종목 발굴(풀 스캔)은 하지 않는다(그건 아침 풀 발굴 1회가 담당). 여기서는
  이미 선정된 종목의 price/asof 만 실측으로 최신화한다(수치 날조 없음).
- 한국 개장일·시간 가드: 주말·공휴일·연말휴장 제외, 07:30~18:30 KST 에서만 동작.
- 값이 바뀐 게 없으면 파일을 건드리지 않는다(워크플로가 변경 없을 때 커밋 생략).

GitHub Actions(.github/workflows/intraday-refresh.yml)가 30분마다 호출한다.
로컬 점검: `python scripts/intraday_refresh.py --force`(시간 가드 무시).
"""
import datetime
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
FEED_PATH = Path(__file__).resolve().parent.parent / "feed.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def is_trading_now(now_kst):
    """(개장일·시간) → (bool, 사유). 주말·공휴일·연말휴장·시간외 제외."""
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
    if minutes < 7 * 60 + 30 or minutes > 18 * 60 + 30:
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


def fetch_price(symbol):
    """현재가 조회. 실패 시 None."""
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


def main():
    force = "--force" in sys.argv[1:]
    now = datetime.datetime.now(KST)
    ok, reason = is_trading_now(now)
    if not ok and not force:
        print(f"[intraday] skip ({reason}) @ {now.isoformat()}")
        return 0

    feed = json.loads(FEED_PATH.read_text(encoding="utf-8"))
    changed = 0
    seen = {}  # code+market → price 캐시(중복 종목 1회만 조회)
    for section in ("signals", "observations"):
        for item in feed.get(section, []):
            code = item.get("code", "")
            market = item.get("market", "KR")
            key = f"{market}:{code}"
            if key in seen:
                price = seen[key]
            else:
                sym = resolve_symbol(code, market)
                price = fetch_price(sym) if sym else None
                seen[key] = price
            if price is None:
                continue
            if item.get("price") != price:
                item["price"] = price
                changed += 1

    if changed == 0:
        print(f"[intraday] 변경 없음 @ {now.isoformat()} (force={force})")
        return 0

    # 변경 있을 때만 시각·시장상태 갱신.
    now_iso = now.replace(microsecond=0, second=0).isoformat()
    mt = now.hour * 60 + now.minute
    korea = feed.setdefault("market_state", {}).setdefault("korea", {})
    korea["status"] = "open" if (9 * 60 <= mt <= 15 * 60 + 30) else "closed"
    korea["basis"] = "장중 실시간(30분 자동갱신)"
    korea["asof"] = now_iso
    feed["generated_at"] = now_iso

    FEED_PATH.write_text(
        json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[intraday] {changed}개 종목 시세 갱신 @ {now_iso}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
