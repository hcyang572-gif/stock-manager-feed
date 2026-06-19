#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
시세 정합성 검증 게이트(코드화된 verifier) — quote-integrity-verifier 에이전트의
탐지 규칙(INC-001~005)을 코드로 승격해 **상시 자동 실행**되게 한다.

쓰임:
  1) analyze_technical.main() 이 feed 작성 직후 verify_feed(feed)를 호출 — 매 분석마다
     자동 검사하고 경고를 feed['integrity'] 에 기록(앱·로그가 본다). '수동 검증'을
     파이프라인 상시 게이트로 만든 것.
  2) 단독 실행: `python scripts/verify_quotes.py [--remote]` — 라이브/로컬 feed 를
     받아 검사 결과를 출력하고, 치명 경고가 있으면 exit 1(CI 게이트).

검사 규칙(원장 .claude/quote-integrity-ledger.md 와 1:1):
- 이상치: 지수 |등락|>20%, 종목 >45% (데이터 오류 의심·INC-001/005)
- 신선도: us_context/kr_context.asof 가 마지막 영업일보다 과도하게 과거 → stale
- stale 플래그: ctx.stale==True 면 경고
- 누락→0 의심: change_pct 가 정확히 0.0 인 종목 비율이 과반이면 경고(INC-003)
- NaN/비유한 수치(JSON 안전·INC-002 흔적)
※ 부호(rf) 자체는 수집 단계에서 확정하므로 여기선 이상치·신선도·누락에 집중.
"""
import datetime
import json
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = REPO_ROOT / "feed.json"
REMOTE_FEED = ("https://raw.githubusercontent.com/hcyang572-gif/"
               "stock-manager-feed/main/feed.json")

INDEX_CAP = 20.0   # 지수 일중 |등락| 상한(%)
STOCK_CAP = 45.0   # 종목 일중 |등락| 상한(%)


def _last_business_day(d):
    while d.weekday() >= 5:   # 토·일 → 금요일로
        d -= datetime.timedelta(days=1)
    return d


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def verify_feed(feed, now=None):
    """feed dict 검사 → 경고 dict {'critical':[...], 'warn':[...]}.
    critical = JSON/표시 안전을 해치거나 명백한 오류, warn = 신선도·의심."""
    now = now or datetime.datetime.now(KST)
    today = now.date()
    last_bd = _last_business_day(today)
    crit, warn = [], []

    def _check_ctx(name, ctx):
        if not isinstance(ctx, dict):
            return
        for q in (ctx.get("indices") or []):
            cp = q.get("change_pct")
            if cp is None:
                continue
            if not isinstance(cp, (int, float)) or not math.isfinite(cp):
                crit.append(f"{name} {q.get('name')} 등락 비유한값({cp})")
            elif abs(cp) > INDEX_CAP:
                crit.append(f"{name} {q.get('name')} 이상치 {cp:+g}% (>{INDEX_CAP}%)")
        if ctx.get("stale"):
            warn.append(f"{name} stale=true (직전값 보존 의심)")
        asof = _parse_date(ctx.get("asof"))
        if asof and (last_bd - asof).days > 1:
            warn.append(f"{name} asof {asof} 가 마지막 영업일({last_bd})보다 과거(신선도)")

    _check_ctx("us_context", feed.get("us_context"))
    _check_ctx("kr_context", feed.get("kr_context"))

    # ★INC-007★ market_state.korea.asof 신선도 — 이 블록은 intraday_refresh 만
    # 갱신해 왔는데 GitHub cron 스킵 시 며칠씩 옛 시각에 멈췄다(2026-06-19: korea
    # status=open 인데 asof 가 06-17 13:28). kr_context 만 검사하면 못 잡으므로
    # 여기서 market_state.{korea,us}.asof 도 같은 신선도 규칙으로 검사한다.
    for mk in ("korea", "us"):
        st = (feed.get("market_state") or {}).get(mk) or {}
        asof = _parse_date(st.get("asof"))
        status = str(st.get("status") or "")
        # us 는 KR 전용 빌드에서 asof=null/closed 가 정상이므로 asof 가 있을 때만 검사.
        if asof and (last_bd - asof).days > 1:
            warn.append(f"market_state.{mk} asof {asof} 가 마지막 영업일"
                        f"({last_bd})보다 과거(신선도, status={status})")

    # bigtech (미국 종목)
    bt = (feed.get("us_context") or {}).get("bigtech") or []
    for q in bt:
        cp = q.get("change_pct")
        if isinstance(cp, (int, float)) and math.isfinite(cp) and abs(cp) > STOCK_CAP:
            crit.append(f"빅테크 {q.get('name')} 이상치 {cp:+g}%")

    # 신호·관찰 — 이상치·비유한·0.0 누락 의심
    rows = (feed.get("signals") or []) + (feed.get("observations") or [])
    kr = [r for r in rows if (r.get("market") or "KR").upper() == "KR"]
    zero = 0
    for r in rows:
        cp = r.get("change_pct")
        if cp is None:
            continue
        if not isinstance(cp, (int, float)) or not math.isfinite(cp):
            crit.append(f"{r.get('name')} 등락 비유한값({cp})")
        elif abs(cp) > STOCK_CAP:
            crit.append(f"{r.get('name')} 등락 이상치 {cp:+g}%")
        elif cp == 0.0:
            zero += 1
    # 거래일인데 KR 종목 절반 이상이 정확히 0.0 → 누락→0 의심(INC-003)
    if kr and now.weekday() < 5 and 8 * 60 <= now.hour * 60 + now.minute < 20 * 60:
        if zero >= max(3, len(kr) // 2):
            warn.append(f"KR 종목 {zero}/{len(rows)} 가 등락 0.00% — 누락→0 의심(INC-003)")

    return {"critical": crit, "warn": warn}


def _load_remote():
    import urllib.request
    req = urllib.request.Request(REMOTE_FEED, headers={"User-Agent": "verify"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def main():
    remote = "--remote" in sys.argv[1:]
    feed = _load_remote() if remote else json.loads(
        FEED_PATH.read_text(encoding="utf-8-sig"))
    res = verify_feed(feed)
    src = "원격" if remote else "로컬"
    print(f"[verify] {src} feed 검사 — critical {len(res['critical'])}·warn {len(res['warn'])}")
    for c in res["critical"]:
        print(f"  ❌ {c}")
    for w in res["warn"]:
        print(f"  ⚠️ {w}")
    if not res["critical"] and not res["warn"]:
        print("  ✅ 이상 없음")
    sys.exit(1 if res["critical"] else 0)


if __name__ == "__main__":
    main()
