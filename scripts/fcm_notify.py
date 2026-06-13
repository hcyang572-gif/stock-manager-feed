#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FCM 토픽 푸시 발송 — 분석 완료(=새 신호 갱신) 시 앱이 종료/백그라운드여도 알림.

앱은 토픽 'analysis' 를 구독하고, feed 갱신 후 이 스크립트가 그 토픽으로
브로드캐스트 알림을 보낸다(종목별 상태 불필요 — 단순·확실). FCM HTTP v1 사용.

필요 환경변수(GitHub Secret):
  FCM_SERVICE_ACCOUNT  Firebase 서비스 계정 키(JSON 전체 문자열)
인자: --title "..." --body "..."  (--topic 기본 analysis)

서비스 계정/키가 없으면 조용히 건너뛴다(분석 자체는 영향 없음).
의존: google-auth (워크플로에서 pip install). 실패는 흡수.
"""
import argparse
import json
import os
import sys
import urllib.request


def _send(project_id, token, topic, title, body):
    msg = {
        "message": {
            "topic": topic,
            "notification": {"title": title, "body": body},
            "android": {"priority": "high"},
            "data": {"type": "analysis_done"},
        }
    }
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    req = urllib.request.Request(
        url, data=json.dumps(msg).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode("utf-8", "ignore")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="새 분석 결과 도착")
    ap.add_argument("--body", default="호창이가 새 매수 신호를 올렸어요. 탭해서 확인하세요.")
    ap.add_argument("--topic", default="analysis")
    args = ap.parse_args()

    raw = os.environ.get("FCM_SERVICE_ACCOUNT", "").strip()
    if not raw:
        print("[fcm] FCM_SERVICE_ACCOUNT 미설정 — 푸시 건너뜀")
        return 0
    try:
        info = json.loads(raw)
        project_id = info.get("project_id")
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        creds.refresh(Request())
        status, resp = _send(project_id, creds.token, args.topic,
                             args.title, args.body)
        print(f"[fcm] 발송 status={status} {resp[:200]}")
        return 0
    except Exception as ex:
        print(f"[fcm] 발송 실패(무시): {ex}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
