"""
Google News RSS -> Telegram channel.
- keywords.txt 의 각 키워드로 검색
- 새로운 기사만 텔레그램 채널로 송출 (제목 + 링크)
- seen.json 으로 중복 방지
"""
import html
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).parent
KEYWORDS_FILE = ROOT / "keywords.txt"
SEEN_FILE = ROOT / "seen.json"

MAX_SEEN = 2000             # seen.json 에 보관할 최대 ID 개수
SEND_DELAY_SEC = 0.6        # 텔레그램 rate limit 회피용 메시지 간격
PER_KEYWORD_LIMIT = 30      # 키워드당 한 번에 처리할 최대 기사 수
REQUEST_TIMEOUT = 30
LOOKBACK_HOURS = 2          # 발행 시각 기준 이 시간 안의 기사만 후보 (cron 지연 흡수용)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def load_keywords() -> list[str]:
    if not KEYWORDS_FILE.exists():
        print(f"keywords.txt 없음: {KEYWORDS_FILE}", file=sys.stderr)
        return []
    out = []
    for raw in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def load_seen() -> list[str]:
    if not SEEN_FILE.exists():
        return []
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_seen(seen_list: list[str]) -> None:
    trimmed = seen_list[-MAX_SEEN:]
    SEEN_FILE.write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_recent(entry) -> bool:
    parsed = entry.get("published_parsed")
    if not parsed:
        # published 시각이 없으면 ID dedup 에 맡김 (안전한 통과)
        return True
    published = datetime(*parsed[:6], tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    return published >= cutoff


def fetch_news(keyword: str):
    q = urllib.parse.quote(keyword)
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    feed = feedparser.parse(url)
    recent = [e for e in feed.entries if is_recent(e)]
    return recent[:PER_KEYWORD_LIMIT]


def send_telegram(text: str) -> None:
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        api,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()


def article_id(entry) -> str:
    # Google News 는 보통 entry.id 가 안정적인 GUID. 없으면 link 사용.
    return entry.get("id") or entry.get("link") or ""


def main() -> int:
    if not BOT_TOKEN or not CHAT_ID:
        print("환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 필요", file=sys.stderr)
        return 1

    keywords = load_keywords()
    if not keywords:
        print("키워드가 비어있음. keywords.txt 확인.", file=sys.stderr)
        return 1

    is_first_run = not SEEN_FILE.exists()
    seen_order = load_seen()
    seen_set = set(seen_order)

    sent_count = 0
    discovered_count = 0

    for kw in keywords:
        try:
            entries = fetch_news(kw)
        except Exception as ex:
            print(f"[{kw}] fetch 실패: {ex}", file=sys.stderr)
            continue

        for entry in entries:
            uid = article_id(entry)
            if not uid or uid in seen_set:
                continue

            discovered_count += 1
            title = html.escape(entry.get("title", "(제목 없음)"))
            link = entry.get("link", "")

            if is_first_run:
                # 첫 실행: 폭주 방지를 위해 기록만 하고 송출 안 함
                seen_set.add(uid)
                seen_order.append(uid)
                continue

            msg = f"<b>[{html.escape(kw)}]</b>\n{title}\n{link}"
            try:
                send_telegram(msg)
                seen_set.add(uid)
                seen_order.append(uid)
                sent_count += 1
                time.sleep(SEND_DELAY_SEC)
            except Exception as ex:
                print(f"송출 실패 [{kw}] {uid}: {ex}", file=sys.stderr)

    save_seen(seen_order)

    if is_first_run:
        print(f"첫 실행: {discovered_count}건을 seen.json 에 시드. 다음 실행부터 송출.")
    else:
        print(f"발견 {discovered_count}건 / 송출 {sent_count}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
