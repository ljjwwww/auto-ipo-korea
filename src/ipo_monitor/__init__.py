"""One-shot public RSS collection and opt-in candidate extraction."""

import argparse
from contextlib import closing
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
import re
import sqlite3
import sys
import tomllib
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

import feedparser
import httpx


class CollectionError(Exception):
    """An actionable, safe-to-display collection failure."""


def read_feed(source: str) -> tuple[str, bytes]:
    if source.startswith(("http://", "https://")):
        url = urlsplit(source)
        if url.username or url.password or url.query or url.fragment:
            raise CollectionError("공개 RSS URL에는 인증정보, 쿼리, fragment를 사용하지 마세요.")
        try:
            response = httpx.get(source, timeout=20, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise CollectionError(f"HTTP 오류: {exc.response.status_code}") from None
        except httpx.RequestError:
            raise CollectionError("네트워크 오류: 연결, DNS, TLS 또는 시간 초과를 확인하세요.") from None
        return source, response.content
    if "://" in source:
        raise CollectionError("HTTP(S) URL 또는 로컬 파일 경로를 사용하세요.")
    path = Path(source).expanduser().resolve()
    try:
        return path.as_uri(), path.read_bytes()
    except OSError:
        raise CollectionError("RSS 파일을 읽을 수 없습니다. 경로와 권한을 확인하세요.") from None


def published_time(raw: str) -> str | None:
    """Never adopt feedparser's inferred UTC for timezone-less dates."""
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            value = parsedate_to_datetime(raw)
        except (ValueError, TypeError, OverflowError):
            return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(ZoneInfo("Asia/Seoul")).isoformat()


def parse_feed(data: bytes) -> list[dict]:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise CollectionError("RSS 파싱 오류: 올바른 XML인지 확인하세요.") from None
    if root.tag == "rss":
        valid_root = root.find("channel") is not None
    else:
        valid_root = root.tag in {
            "{http://www.w3.org/2005/Atom}feed",
            "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF",
        }
    if not valid_root:
        raise CollectionError("RSS 구조 오류: RSS channel 또는 Atom feed가 필요합니다.")
    parsed = feedparser.parse(data)
    if parsed.get("bozo") or not parsed.get("version"):
        raise CollectionError("RSS 파싱 오류: 올바른 RSS/Atom XML인지 확인하세요.")
    posts = []
    for entry in parsed.entries:
        link = entry.get("link", "").strip()
        entry_id = (entry.get("id") or link).strip()
        if not entry_id or not link or not entry.get("title", "").strip():
            raise CollectionError("RSS 항목 오류: 제목, 원문 링크, ID 또는 대체 URL이 필요합니다.")
        raw = entry.get("published", "")
        posts.append(dict(entry_id=entry_id, title=entry.title, link=link,
                          published_raw=raw, published_at=published_time(raw),
                          summary=entry.get("summary", "")))
    return posts


def save_posts(db_path: Path, feed_id: str, posts: list[dict]) -> tuple[bool, list[dict]]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE IF NOT EXISTS feeds (id TEXT PRIMARY KEY, collected_at TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS posts (
            feed_id TEXT NOT NULL REFERENCES feeds(id), entry_id TEXT NOT NULL,
            title TEXT NOT NULL, link TEXT NOT NULL, published_raw TEXT NOT NULL,
            published_at TEXT, summary TEXT NOT NULL,
            PRIMARY KEY (feed_id, entry_id))""")
        # Serialize first-run detection and inserts across overlapping scheduled runs.
        conn.execute("BEGIN IMMEDIATE")
        first = conn.execute("SELECT 1 FROM feeds WHERE id = ?", (feed_id,)).fetchone() is None
        conn.execute("""INSERT INTO feeds VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET collected_at=excluded.collected_at""",
                     (feed_id, datetime.now(ZoneInfo("Asia/Seoul")).isoformat()))
        new = []
        for post in posts:
            cursor = conn.execute("""INSERT INTO posts
                (feed_id, entry_id, title, link, published_raw, published_at, summary)
                VALUES (:feed_id, :entry_id, :title, :link, :published_raw, :published_at, :summary)
                ON CONFLICT(feed_id, entry_id) DO NOTHING""", {"feed_id": feed_id, **post})
            if cursor.rowcount:
                new.append(post)
            else:
                conn.execute("""UPDATE posts SET title=:title,link=:link,
                    published_raw=:published_raw,published_at=:published_at,summary=:summary
                    WHERE feed_id=:feed_id AND entry_id=:entry_id""", {"feed_id": feed_id, **post})
    return first, new


def terminal_text(value: str) -> str:
    # Treat feed text as untrusted data, including terminal escape sequences.
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "pipeline-results":
        from .pipeline import list_results
        parser = argparse.ArgumentParser(description="AI 호출 없이 현재 입력의 단계별 결과 조회")
        parser.add_argument("--db",type=Path,default=Path("data/ipo-monitor.sqlite3"))
        return list_results(parser.parse_args(argv[1:]).db)
    if argv and argv[0] == "pipeline":
        import os
        from .extraction import DEFAULT_MODEL
        from .pipeline import command
        parser = argparse.ArgumentParser(description="RSS AI 분류와 불확실한 글의 본문 재분석")
        parser.add_argument("--config",type=Path,default=Path("config/sources.toml"))
        parser.add_argument("--db",type=Path,default=Path("data/ipo-monitor.sqlite3"))
        parser.add_argument("--skip-collect",action="store_true",help="이미 저장된 글만 분석")
        parser.add_argument("--backend",choices=["fixture","openai"],default="openai")
        parser.add_argument("--fixture",type=Path)
        parser.add_argument("--body-fixture",type=Path)
        parser.add_argument("--model",default=os.environ.get("IPO_MODEL",DEFAULT_MODEL))
        parser.add_argument("--allow-api",action="store_true")
        parser.add_argument("--limit",type=int,help="이번 실행에서 볼 글 수 (미지정 시 전체, 각 글 최대 2회 분석)")
        parser.add_argument("--refresh-body",action="store_true",help="성공 보관한 본문도 다시 읽기")
        return command(parser.parse_args(argv[1:]))
    if argv and argv[0] == "collect":
        parser = argparse.ArgumentParser(description="설정된 공개 RSS를 한 번 수집합니다.")
        parser.add_argument("--config", type=Path, default=Path("config/sources.toml"))
        parser.add_argument("--db", type=Path, default=Path("data/ipo-monitor.sqlite3"))
        args = parser.parse_args(argv[1:])
        try:
            with args.config.open("rb") as f:
                config = tomllib.load(f)
            sources = config.get("sources")
            if not isinstance(sources, list) or not sources:
                raise ValueError("sources 목록이 비어 있습니다")
            for source in sources:
                if (not isinstance(source, dict) or not isinstance(source.get("name"), str)
                    or not source["name"].strip() or not isinstance(source.get("url"), str)
                    or not source["url"].startswith(("https://", "http://"))
                    or type(source.get("enabled")) is not bool):
                    raise ValueError("각 출처에 name, HTTP(S) url, enabled 불리언이 필요합니다")
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            print(f"설정 오류: {exc}", file=sys.stderr)
            return 1
        enabled = [source for source in sources if source["enabled"]]
        if not enabled:
            print("활성 출처가 없습니다.")
            return 0
        failed = 0
        for source in enabled:
            print(f"출처: {terminal_text(source['name'])}")
            failed += main(["--feed", source["url"], "--db", str(args.db)]) != 0
        print(f"수집 결과: {len(enabled)}개 출처, 실패 {failed}개")
        return int(bool(failed))
    if argv and argv[0] in {"extract", "candidates"}:
        import os
        from .extraction import DEFAULT_MODEL, run_command

        parser = argparse.ArgumentParser(description="글별 공모주 후보 추출/조회 (공식 정보 미검증)")
        parser.add_argument("command", choices=["extract", "candidates"])
        parser.add_argument("--db", type=Path, default=Path("data/ipo-monitor.sqlite3"))
        if argv[0] == "extract":
            parser.add_argument("--backend", choices=["fixture", "openai"], default="openai")
            parser.add_argument("--fixture", type=Path, help="테스트 고정 응답 JSON (실제 분석 아님)")
            parser.add_argument("--model", default=os.environ.get("IPO_MODEL", DEFAULT_MODEL))
            parser.add_argument("--allow-api", action="store_true", help="유료 API 실호출 명시 승인")
        return run_command(parser.parse_args(argv))
    parser = argparse.ArgumentParser(
        description="공개 RSS를 한 번 수집하여 SQLite에 저장합니다.",
        epilog="설정 출처 수집: ipo-monitor collect --help; 후보 추출/조회: ipo-monitor extract --help 또는 ipo-monitor candidates --help")
    parser.add_argument("--feed", required=True, help="공개 RSS URL 또는 로컬 RSS 파일")
    parser.add_argument("--db", type=Path, default=Path("data/ipo-monitor.sqlite3"), help="SQLite 경로")
    args = parser.parse_args(argv)
    try:
        feed_id, data = read_feed(args.feed)
        posts = parse_feed(data)
        first, new = save_posts(args.db, feed_id, posts)
    except CollectionError as exc:
        print(f"수집 실패: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError):
        print("저장 실패: DB 경로, 권한, 잠금 또는 스키마를 확인하세요.", file=sys.stderr)
        return 1
    print(f"{'기준 목록' if first else '새 글'}: {len(new)}건")
    if not posts:
        print("빈 피드: 유효한 피드에 게시글이 없습니다.")
    for post in new:
        when = post["published_at"] or (
            f"{post['published_raw']} (시간대 미확인 또는 해석 불가)" if post["published_raw"] else "게시 시각 없음")
        print(terminal_text(f"- {post['title']}\n  {post['link']}\n  {when}"))
    return 0
