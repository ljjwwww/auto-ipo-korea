"""Read the public IPO page as source observations, not investment decisions."""

from datetime import date
from html.parser import HTMLParser
import re

import httpx

from . import CollectionError

SITE_URL = "https://letsgetitstock.com/ipo"
MAX_BYTES = 2 * 1024 * 1024
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_HIDDEN = {"script", "style", "noscript", "template"}


class _Node:
    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children = []
        self.closed = False

    def text(self):
        if self.tag in _HIDDEN:
            return ""
        return " ".join(" ".join(child if isinstance(child, str) else child.text()
                                  for child in self.children).split())

    def find(self, tag):
        for child in self.children:
            if isinstance(child, _Node) and child.tag not in _HIDDEN:
                if child.tag == tag:
                    yield child
                yield from child.find(tag)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag in _VOID:
            node.closed = True
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                # An ancestor ending does not make an unfinished child valid.
                self.stack[index].closed = True
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _label(raw):
    """Keep display markers separate without assigning their meaning."""
    value = re.sub(r"^(?:청약|상장)\)\s*", "", raw).strip()
    match = re.search(r"((?:⭐\ufe0f?|❌\ufe0f?)+)$", value)
    marker = match.group(1) if match else ""
    name = value[:match.start()].rstrip() if match else value
    if not name:
        raise CollectionError("사이트 파싱 오류: 종목명이 없는 달력 항목입니다.")
    return name, marker, marker.count("⭐") if "⭐" in marker else None


def _event_date(cell):
    value = cell.attrs.get("data-date", "")
    if not value:
        match = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", cell.attrs.get("aria-label", ""))
        if match:
            value = "-".join(match.groups())
    try:
        year, month, day = (int(part) for part in value.split("-"))
        return date(year, month, day).isoformat()
    except (ValueError, TypeError):
        raise CollectionError("사이트 파싱 오류: 달력 날짜를 확인할 수 없습니다.") from None


def parse_site(html):
    """Return per-display-name records from a recognized calendar and IPO tables."""
    page = _Page()
    page.feed(html)
    page.close()
    grids = [node for node in page.root.find("div") if node.attrs.get("role") == "grid"]
    if not grids or any(not grid.closed for grid in grids):
        raise CollectionError("사이트 파싱 오류: 완결된 공모주 달력을 찾을 수 없습니다.")
    grouped = {}

    def stock(name):
        if name not in grouped:
            grouped[name] = {"item_id": name, "title": name, "url": SITE_URL,
                             "published_raw": "", "published_at": None,
                             "payload": {"stock_name": name, "events": [],
                                         "rating_observations": [], "table_rows": []}}
        return grouped[name]["payload"]

    for grid in grids:
        for cell in grid.find("button"):
            if cell.attrs.get("role") != "gridcell":
                continue
            if not cell.closed:
                raise CollectionError("사이트 파싱 오류: 달력 항목이 잘렸습니다.")
            for label in cell.find("span"):
                kind = label.attrs.get("data-tone")
                if label.attrs.get("data-category") != "ipo" or kind not in {"subscription", "listing"}:
                    continue
                if not label.closed:
                    raise CollectionError("사이트 파싱 오류: 달력 표기가 잘렸습니다.")
                raw = label.text()
                name, marker, stars = _label(raw)
                event = {"date": _event_date(cell), "kind": kind, "raw_label": raw,
                         "raw_marker": marker, "stars": stars}
                payload = stock(name)
                if event not in payload["events"]:
                    payload["events"].append(event)
                    payload["rating_observations"].append(dict(event))

    # The site places each IPO table in a section labelled with its display title.
    for section in page.root.find("section"):
        heading = next(section.find("h2"), None)
        section_name = section.attrs.get("aria-label") or (heading.text() if heading else "")
        for table in section.find("table"):
            headers = [node.text() for row in table.find("thead") for node in row.find("th")]
            if not headers or headers[0] != "종목명":
                continue
            if not table.closed or not section.closed or len(set(headers)) != len(headers):
                raise CollectionError("사이트 파싱 오류: 공모주 표 구조가 변경되거나 잘렸습니다.")
            for tbody in table.find("tbody"):
                if not tbody.closed:
                    raise CollectionError("사이트 파싱 오류: 공모주 표가 잘렸습니다.")
                for row in tbody.find("tr"):
                    cells = [node for node in row.children if isinstance(node, _Node) and node.tag in {"th", "td"}]
                    if not row.closed or len(cells) != len(headers) or any(not cell.closed for cell in cells):
                        raise CollectionError("사이트 파싱 오류: 공모주 표 열 개수가 맞지 않습니다.")
                    strong = next(cells[0].find("strong"), None)
                    if strong is None or not strong.text():
                        raise CollectionError("사이트 파싱 오류: 공모주 표의 종목명을 찾을 수 없습니다.")
                    raw_name = strong.text()
                    name, marker, stars = _label(raw_name)
                    data = {"section": section_name, "raw_name": raw_name,
                            "name_cell": cells[0].text(),
                            "fields": {header: cell.text() for header, cell in zip(headers, cells)}}
                    payload = stock(name)
                    if data not in payload["table_rows"]:
                        payload["table_rows"].append(data)
                    if marker:
                        payload["rating_observations"].append({"kind": "table", "raw_label": raw_name,
                                                               "raw_marker": marker, "stars": stars})
    if not grouped and not any(re.search(r"(?:일정|종목)(?:이|가)?\s*없", grid.text()) for grid in grids):
        raise CollectionError("사이트 파싱 오류: 빈 달력인지 구조 변경인지 확인할 수 없습니다.")
    for record in grouped.values():
        record["payload"]["events"].sort(key=lambda event: (event["date"], event["kind"], event["raw_label"]))
        record["payload"]["rating_observations"].sort(key=lambda event: (event.get("date", ""), event["kind"], event["raw_label"]))
    return [grouped[name] for name in sorted(grouped)]


def collect(source, previous):
    """Fetch only the configured public page; previous observations are immutable upstream."""
    if source.get("url") != SITE_URL:
        raise CollectionError("사이트 URL 오류: 지정된 공개 공모주 페이지만 지원합니다.")
    try:
        with httpx.stream("GET", SITE_URL, timeout=20, follow_redirects=False) as response:
            if response.is_redirect:
                raise CollectionError("사이트 접근 오류: 리다이렉트는 따르지 않습니다.")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise CollectionError("사이트 접근 오류: HTML 응답이 아닙니다.")
            chunks = []
            size = 0
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise CollectionError("사이트 접근 오류: 응답 크기 제한을 초과했습니다.")
                chunks.append(chunk)
            try:
                html = b"".join(chunks).decode(response.encoding or "utf-8", errors="strict")
            except (UnicodeError, LookupError):
                raise CollectionError("사이트 접근 오류: 문자 인코딩을 읽을 수 없습니다.") from None
    except httpx.HTTPStatusError as exc:
        raise CollectionError(f"사이트 HTTP 오류: {exc.response.status_code}") from None
    except httpx.RequestError:
        raise CollectionError("사이트 네트워크 오류: 연결 또는 시간 초과를 확인하세요.") from None
    return parse_site(html)
