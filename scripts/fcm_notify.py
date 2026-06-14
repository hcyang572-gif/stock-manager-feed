#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FCM 토픽 푸시 발송 — 분석 완료/가격 도달 등 앱이 종료/백그라운드여도 알림.

앱은 토픽 'analysis' 를 구독하고, 이 모듈이 그 토픽으로 브로드캐스트 알림을
보낸다(FCM HTTP v1). CLI(분석 완료 알림)와 라이브러리(price_alert_watch) 양쪽에서 쓴다.

필요 환경변수(GitHub Secret):
  FCM_SERVICE_ACCOUNT  Firebase 서비스 계정 키(JSON 전체 문자열)
CLI 인자: --title "..." --body "..."  (--topic 기본 analysis)

서비스 계정/키가 없으면 조용히 건너뛴다(분석/감시 자체는 영향 없음).
의존: google-auth (워크플로에서 pip install). 실패는 흡수.
"""
import argparse
import json
import os
import sys
import urllib.request


def _send(project_id, token, topic, title, body, data=None):
    # FCM data 값은 모두 문자열이어야 한다. 미지정 시 기본 type=analysis_done.
    payload = {"type": "analysis_done"} if not data else \
        {k: str(v) for k, v in data.items()}
    msg = {
        "message": {
            "topic": topic,
            "notification": {"title": title, "body": body},
            "android": {"priority": "high"},
            "data": payload,
        }
    }
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    req = urllib.request.Request(
        url, data=json.dumps(msg).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode("utf-8", "ignore")


def _credentials_and_project():
    """환경변수 FCM_SERVICE_ACCOUNT → (refresh 된 creds, project_id). 미설정/실패 시 (None, None)."""
    raw = os.environ.get("FCM_SERVICE_ACCOUNT", "").strip()
    if not raw:
        return None, None
    info = json.loads(raw)
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
    creds.refresh(Request())
    return creds, info.get("project_id")


def send_message(title, body, topic="analysis", data=None):
    """토픽으로 알림 1건 발송. data(dict)를 주면 FCM data 페이로드로 실어 앱이
    분류·기록할 수 있다. 성공 True / 미설정·실패 False(예외 흡수)."""
    try:
        creds, project_id = _credentials_and_project()
        if not creds:
            print("[fcm] FCM_SERVICE_ACCOUNT 미설정 - 발송 건너뜀")
            return False
        status, resp = _send(project_id, creds.token, topic, title, body, data)
        print(f"[fcm] 발송 status={status} {resp[:200]}")
        return 200 <= status < 300
    except Exception as ex:
        print(f"[fcm] 발송 실패(무시): {ex}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="새 분석 결과 도착")
    ap.add_argument("--body", default="호창이가 새 매수 신호를 올렸어요. 탭해서 확인하세요.")
    ap.add_argument("--topic", default="analysis")
    args = ap.parse_args()
    send_message(args.title, args.body, args.topic)
    return 0


if __name__ == "__main__":
    sys.exit(main())
