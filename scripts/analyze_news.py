#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
뉴스 종합분석 — Claude API + 웹검색(web_search 서버툴)으로 feed.json 의 신호 종목에
**48시간 촉매/뉴스**를 조사해 병합한다(catalyst_verified=true).

- analyze_technical.py 가 먼저 차트로 만든 feed.json(signals/observations)을 읽고,
  신호 종목(+상위 관찰 일부)에 대해 최근 뉴스·공시·수주·이벤트를 웹에서 조사한다.
- 결과를 feed.json 의 top-level `catalyst` 블록 + 각 signal 의 catalyst/catalyst_verified
  필드로 병합한다. 출처(URL)·기준시각(asof)을 함께 남긴다(날조 금지 — 출처 없으면 neutral).
- ANTHROPIC_API_KEY 환경변수 필요(GitHub Actions secret). 없으면 안내 후 종료(차트 feed 유지).

모델: 환경변수 CLAUDE_MODEL(기본 claude-sonnet-4-6 — 웹검색·비용 균형). 더 저렴하게는
claude-haiku-4-5, 더 강하게는 claude-opus-4-8 로 바꿀 수 있다.

GitHub Actions(analyze-news.yml)에서 analyze_technical.py 다음에 실행된다.
"""
import datetime
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parent.parent
FEED_PATH = REPO_ROOT / "feed.json"

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6").strip() or "claude-sonnet-4-6"
# 신호 외에 상위 관찰 종목도 N개까지 함께 조사(촉매가 관찰을 신호로 끌어올릴 근거가 될 수 있음).
MAX_OBS = 3
# 웹검색 횟수 상한(비용 가드).
MAX_WEB_USES = 10


def _load_feed():
    if not FEED_PATH.exists():
        print("[news] feed.json 없음 — 중단(차트 분석 먼저 필요)")
        return None
    try:
        return json.loads(FEED_PATH.read_text(encoding="utf-8-sig"))
    except Exception as ex:
        print(f"[news] feed.json 읽기 실패: {ex}")
        return None


def _targets(feed):
    """조사 대상 [(code, name)] — 신호 전부 + 상위 관찰 MAX_OBS."""
    out, seen = [], set()
    for s in feed.get("signals", []):
        code = str(s.get("code", "")).strip()
        if code and code not in seen:
            out.append((code, s.get("name", code)))
            seen.add(code)
    for o in feed.get("observations", [])[:MAX_OBS]:
        code = str(o.get("code", "")).strip()
        if code and code not in seen:
            out.append((code, o.get("name", code)))
            seen.add(code)
    return out


SCHEMA_HINT = (
    '{"market_summary": "시장 전반 촉매 요약 1~2문장", '
    '"items": [{"code": "종목코드", "name": "종목명", '
    '"headline": "촉매 한 줄 제목(없으면 \'뚜렷한 촉매 없음\')", '
    '"detail": "48시간 매매 관점에서 왜 중요한지 1~2문장", '
    '"sentiment": "positive|neutral|negative", '
    '"sources": [{"title": "출처 제목", "url": "https://..."}]}]}'
)


def _build_prompt(targets, date_str):
    lines = "\n".join(f"- {name} ({code})" for code, name in targets)
    return (
        f"당신은 한국 주식 초단기(48시간 이내 매수→매도) 트레이딩의 촉매 분석가입니다.\n"
        f"오늘은 {date_str}(KST)입니다. 아래 한국 종목들에 대해 **최근 48시간 이내**의 "
        f"뉴스·공시·실적·수주·정책·이벤트(촉매)를 웹에서 검색해 정리하세요.\n\n"
        f"대상 종목:\n{lines}\n\n"
        f"규칙(중요):\n"
        f"1. 반드시 web_search 로 실제 출처를 확인하고, **출처 URL이 있는 사실만** 보고하세요. "
        f"추측·날조 금지. 근거를 못 찾으면 headline='뚜렷한 촉매 없음', sentiment='neutral', sources=[] 로 두세요.\n"
        f"2. 48시간 내 주가에 영향을 줄 촉매(임박 실적/공시 D-day, 신규 수주, 정책 수혜, "
        f"신제품, 목표주가 변경, 업황 등) 위주로 간결하게.\n"
        f"3. sentiment 는 48h 단기 주가 방향 관점(호재=positive, 악재=negative, 중립/혼조=neutral).\n"
        f"4. 모든 대상 종목을 items 에 포함하세요(촉매 없어도 neutral 로).\n\n"
        f"마지막 답변은 **다른 말 없이** 아래 형식의 JSON 하나만 ```json 코드블록```으로 출력하세요:\n"
        f"```json\n{SCHEMA_HINT}\n```"
    )


def _extract_json(text):
    """모델 최종 텍스트에서 JSON 추출(```json``` 우선, 없으면 첫 { ~ 마지막 })."""
    if not text:
        return None
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = m.group(1) if m else None
    if blob is None:
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j != -1 and j > i:
            blob = text[i:j + 1]
    if not blob:
        return None
    try:
        return json.loads(blob)
    except Exception:
        return None


def _call_claude(targets, date_str):
    """web_search 툴로 촉매 조사 → 파싱된 dict 또는 None."""
    try:
        import anthropic
    except Exception as ex:
        print(f"[news] anthropic SDK 없음(pip install anthropic): {ex}")
        return None
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용
    tools = [{"type": "web_search_20250305", "name": "web_search",
              "max_uses": MAX_WEB_USES}]
    messages = [{"role": "user", "content": _build_prompt(targets, date_str)}]
    final_text = ""
    for _ in range(6):  # pause_turn(서버툴 루프 한계) 재개 대비
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=8000, tools=tools, messages=messages)
        except Exception as ex:
            print(f"[news] Claude API 호출 실패: {ex}")
            return None
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if text:
            final_text = text
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    data = _extract_json(final_text)
    if data is None:
        print("[news] 모델 응답에서 JSON 파싱 실패")
    return data


def _norm_sentiment(s):
    s = str(s or "").strip().lower()
    return s if s in ("positive", "negative", "neutral") else "neutral"


def merge_into_feed(feed, data, now_iso):
    """조사 결과를 feed 에 병합(catalyst 블록 + 신호 enrich)."""
    items = data.get("items", []) if isinstance(data, dict) else []
    by_code = {}
    for it in items:
        code = str(it.get("code", "")).strip()
        if not code:
            continue
        srcs = []
        for s in (it.get("sources") or [])[:4]:
            url = str(s.get("url", "")).strip()
            if url.startswith("http"):
                srcs.append({"title": str(s.get("title", "")).strip()[:120], "url": url})
        by_code[code] = {
            "headline": str(it.get("headline", "")).strip()[:120],
            "detail": str(it.get("detail", "")).strip()[:400],
            "sentiment": _norm_sentiment(it.get("sentiment")),
            "sources": srcs,
        }

    feed["catalyst"] = {
        "asof": now_iso,
        "verified": True,
        "model": MODEL,
        "summary": str(data.get("market_summary", "")).strip()[:400],
        "items": by_code,
    }

    verified_cnt = 0
    for s in feed.get("signals", []):
        info = by_code.get(str(s.get("code", "")).strip())
        if not info:
            continue
        has_src = bool(info["sources"])
        if info["headline"]:
            s["catalyst"] = (info["headline"] + (" — " + info["detail"]
                                                 if info["detail"] else "")).strip()
        if has_src:
            s["catalyst_verified"] = True
            verified_cnt += 1
            # 근거 앞에 촉매 요약을 덧붙이고, 미검증 경고 톤 태그를 검증 태그로 교체.
            ev = s.get("evidence", "")
            s["evidence"] = (f"촉매: {info['headline']}. " + ev).strip()
            tags = [t for t in s.get("tags", []) if "미검증" not in t]
            if "촉매검증" not in tags:
                tags = ["촉매검증"] + tags
            s["tags"] = tags
            # 종목별 리스크 노트 첫 줄(촉매 미확인 경고)을 출처 안내로 교체.
            rn = s.get("risk_notes", [])
            if rn and "미확인" in rn[0]:
                rn[0] = "촉매 확인됨 — 그래도 진입 전 원문 출처·시초가 갭 직접 확인."
                s["risk_notes"] = rn

    # 관찰 종목에도 촉매를 부착(앱이 관찰 사유에 노출 가능).
    for o in feed.get("observations", []):
        info = by_code.get(str(o.get("code", "")).strip())
        if info and info["headline"] and info["headline"] != "뚜렷한 촉매 없음":
            o["catalyst"] = (info["headline"] + (" — " + info["detail"]
                                                 if info["detail"] else "")).strip()

    # feed 헤더·출처·리스크를 뉴스 반영 상태로 갱신.
    feed["data_source"] = (
        "온디맨드 기술 분석(KIS 현재가 + yfinance 일봉) + 미국증시 전일 환경 보정 "
        f"+ 뉴스/촉매 종합분석(Claude 웹검색, {MODEL}).")
    summ = feed.get("summary", {})
    top = summ.get("top_signal")
    summ["headline"] = (
        f"뉴스+차트 종합분석 갱신({datetime.datetime.now(KST).strftime('%m-%d %H:%M')} KST) — "
        f"촉매 검증 {verified_cnt}건. " + (f"주도주 {top}." if top else ""))
    feed["summary"] = summ
    rn = feed.get("risk_notes", [])
    rn = [x for x in rn if "catalyst_verified=false" not in x and "뉴스/촉매 미반영" not in x]
    note = "뉴스/촉매 종합분석 반영(Claude 웹검색). 보도는 시점에 따라 정정될 수 있으니 진입 전 원문 확인."
    if data.get("market_summary"):
        note = f"시장 촉매 요약: {str(data['market_summary']).strip()[:200]} / " + note
    feed["risk_notes"] = [note] + rn
    return verified_cnt


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[news] ANTHROPIC_API_KEY 미설정 — 뉴스 분석 건너뜀(차트 feed 유지). "
              "feed 레포 Settings > Secrets 에 ANTHROPIC_API_KEY 등록 필요.")
        return 0
    feed = _load_feed()
    if feed is None:
        return 0
    targets = _targets(feed)
    if not targets:
        print("[news] 분석 대상 종목 없음(신호/관찰 비어있음) — 건너뜀")
        return 0
    now = datetime.datetime.now(KST).replace(microsecond=0, second=0)
    now_iso = now.isoformat()
    date_str = now.strftime("%Y-%m-%d")
    print(f"[news] {MODEL} 로 {len(targets)}개 종목 촉매 조사 시작…")
    data = _call_claude(targets, date_str)
    if data is None:
        print("[news] 촉매 조사 실패 — 차트 feed 유지(미변경)")
        return 0
    verified = merge_into_feed(feed, data, now_iso)
    FEED_PATH.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    print(f"[news] 완료 — 촉매 검증 {verified}건 / 대상 {len(targets)}개 @ {now_iso}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
