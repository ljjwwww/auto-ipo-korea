"""Explicitly approved 3-article live experiment, at most 6 API requests/run."""
import argparse
import os
from pathlib import Path
import sqlite3
import sys

from ipo_monitor import main, parse_feed, read_feed, save_posts
from ipo_monitor.extraction import ExtractionError

IDS = {'224473034613', '224471910652', '224463746936'}
FEED = 'https://rss.blog.naver.com/hycj1025.xml'


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--key-file', type=Path, default=Path('data/openai-api-key.txt'))
    parser.add_argument('--db', type=Path, default=Path('data/live-check.sqlite3'))
    parser.add_argument('--allow-api', action='store_true', required=True)
    args = parser.parse_args()
    try:
        # Do not mix experimental subjects with a populated operational database.
        if args.db.exists():
            with sqlite3.connect(args.db) as conn:
                rows = conn.execute('SELECT feed_id,link FROM posts').fetchall()
            if any(feed != FEED or link.split('?')[0].rstrip('/').split('/')[-1] not in IDS for feed,link in rows):
                print('실험 대상 외 글이 있는 DB입니다. 별도 DB 경로를 사용하세요.',file=sys.stderr)
                return 1
        key = os.environ.get('OPENAI_API_KEY','').strip()
        if not key:
            key = args.key_file.read_text(encoding='utf-8').strip()
        if not key:
            raise ValueError
        os.environ['OPENAI_API_KEY'] = key
        feed_id,data = read_feed(FEED)
        posts = [p for p in parse_feed(data) if p['link'].split('?')[0].rstrip('/').split('/')[-1] in IDS]
        if len(posts) != 3:
            print('승인한 글 3건이 현재 RSS에 모두 없습니다. API를 호출하지 않습니다.',file=sys.stderr)
            return 1
        save_posts(args.db,feed_id,posts)
        print('승인 대상 3건만 분석합니다. 이번 실행 최대 6회 API 요청, 자동 재시도 없음.')
        return main(['pipeline','--skip-collect','--db',str(args.db),'--model',args.model,'--allow-api','--limit','3'])
    except (OSError,ValueError,sqlite3.Error,ExtractionError):
        print('실험 준비 실패: 키 설정·RSS 접속·실험 DB를 확인하세요. 비밀 값은 출력하지 않습니다.',file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
