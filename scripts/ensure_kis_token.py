#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KIS 액세스 토큰을 하루 1회 발급해 config/.kis_token.json 에 저장(전용 잡 전용).

★중요★ KIS 토큰은 발급 때마다 KIS가 보안 SMS를 보낸다. 그래서 토큰 발급은 **오직 이
스크립트(하루 1회 전용 워크플로)에서만** 한다. 다른 모든 스크립트/워크플로
(intraday_refresh·analyze_technical·price_alert_watch 등)는 이 캐시 파일을 **읽기만**
하고, 캐시가 없으면 네이버로 폴백한다(KIS_TOKEN_ISSUE!=1 이면 발급 안 함).

워크플로가 actions/cache(키 `kis-token-<KST날짜>`)로 이 파일을 잡 간 보존하므로,
하루의 첫 전용 잡이 1회 발급·저장하고 그날의 모든 잡은 그 캐시를 재사용한다 → 1일 1회.

표준 라이브러리만 사용. 키는 환경변수(KIS_APP_KEY/KIS_APP_SECRET).
"""
import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = REPO_ROOT / "config" / ".kis_token.json"
# 실전 도메인(시세 조회와 동일). 모의 전용으로 쓰려면 별도 설정 필요.
KIS_BASE = "https://openapi.koreainvestment.com:9443"


def main():
    ak = os.environ.get("KIS_APP_KEY")
    sk = os.environ.get("KIS_APP_SECRET")
    if not ak or not sk:
        print("[kis-token] KIS 키 없음 — 발급 생략(서버는 네이버 시세만 사용)")
        return
    now = datetime.datetime.now(KST).timestamp()
    # 캐시가 1시간 이상 남아 있으면 발급 생략(중복 발급·SMS 방지).
    if TOKEN_PATH.exists():
        try:
            c = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
            if c.get("expires_at", 0) > now + 3600:
                print("[kis-token] 캐시 유효 — 발급 생략")
                return
        except Exception:
            pass
    body = json.dumps({
        "grant_type": "client_credentials",
        "appkey": ak,
        "appsecret": sk,
    }).encode("utf-8")
    req = urllib.request.Request(
        KIS_BASE + "/oauth2/tokenP", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.load(r)
    except Exception as e:
        print(f"[kis-token] 발급 요청 실패: {e}")
        sys.exit(0)  # 실패해도 잡 자체는 성공 처리(네이버 폴백으로 동작)
    tok = resp.get("access_token")
    if tok:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        # KIS 토큰 24h 유효 — expires_in 값 불신, 23h(82800s) 고정.
        TOKEN_PATH.write_text(
            json.dumps({"access_token": tok, "expires_at": now + 82800},
                       ensure_ascii=False),
            encoding="utf-8")
        print("[kis-token] 발급·저장 완료(23h 캐시)")
    else:
        print(f"[kis-token] 발급 실패 응답: {resp}")


if __name__ == "__main__":
    main()
