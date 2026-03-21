"""QA카드 디스커버리 — 매니저별 QA카드 조회·분류·데이터 준비"""

from __future__ import annotations

import csv
import io
import re
import requests


# QA 카드 상태 분류
QA_STATUS_MAP = {
    "In Progress": "진행중",
    "In Review": "중단",
}


def resolve_user_map(raw: dict) -> dict:
    """
    user_map의 두 가지 형식을 통합.
      구형: "Cindy": "U03KW6G2TJ5"
      신형: "Cindy": {slack_id: "...", send_time: "15:00", linear_name: "..."}
    """
    resolved = {}
    for name, val in (raw or {}).items():
        if isinstance(val, str):
            resolved[name] = {"slack_id": val}
        else:
            resolved[name] = val
    return resolved


def get_assignee_schedules(user_map: dict, base_time: str) -> dict[str, str]:
    """QAM별 send_time 추출. 미설정 시 base_time 사용."""
    return {
        name: cfg.get("send_time", base_time)
        for name, cfg in user_map.items()
    }


def discover_qa_cards(config: dict) -> dict[str, list[dict]]:
    """
    QA매니저별 QA카드를 조회하고 상태를 분류한다.

    Returns:
        {manager_name: [qa_card, ...]}
        각 qa_card에 qa_status 필드 추가 ("진행중" / "중단" / 원래 상태명)
    """
    from src.linear_client import LinearClient

    client = LinearClient()
    qa_labels = config.get("linear", {}).get("qa_labels", ["QA"])
    user_map = resolve_user_map(config.get("slack", {}).get("user_map") or {})

    all_qa_cards = client.get_qa_cards(qa_labels)

    result: dict[str, list[dict]] = {name: [] for name in user_map}

    for card in all_qa_cards:
        creator = card.get("creator") or {}
        creator_name = (creator.get("name") or "").lower()
        creator_display = (creator.get("displayName") or "").lower()

        state_name = card["state"]["name"]
        card["qa_status"] = QA_STATUS_MAP.get(state_name, state_name)

        for manager_name, manager_cfg in user_map.items():
            linear_name = (manager_cfg.get("linear_name") or "").lower()
            if linear_name and linear_name in (creator_name, creator_display):
                result[manager_name].append(card)
                break
            elif not linear_name and manager_name.lower() in creator_name:
                result[manager_name].append(card)
                break

    return result


def get_active_cards(cards: list[dict]) -> list[dict]:
    """진행중인 QA카드만 필터"""
    return [c for c in cards if c["qa_status"] == "진행중"]


def get_paused_cards(cards: list[dict]) -> list[dict]:
    """중단된 QA카드만 필터"""
    return [c for c in cards if c["qa_status"] == "중단"]


# ── 구글시트 테스트 진행률 ────────────────────────────────────────────────

def _find_testcase_sheet_url(qa_card: dict) -> str | None:
    """QA카드 Attachments에서 '테스트케이스' 구글시트 URL을 찾는다."""
    attachments = qa_card.get("attachments", {}).get("nodes", [])
    for att in attachments:
        title = (att.get("title") or "").strip()
        url = att.get("url") or ""
        if "테스트케이스" in title and "docs.google.com/spreadsheets" in url:
            return url
    return None


def _parse_sheet_id_and_gid(url: str) -> tuple[str, str]:
    """구글시트 URL에서 spreadsheet ID와 gid를 추출한다."""
    # ID: /d/{sheet_id}/
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    sheet_id = m.group(1) if m else ""
    # gid: gid=12345
    m2 = re.search(r"gid=(\d+)", url)
    gid = m2.group(1) if m2 else "0"
    return sheet_id, gid


def _cell_to_index(cell: str) -> tuple[int, int]:
    """'K14' → (row=13, col=10) 0-based index"""
    m = re.match(r"([A-Z]+)(\d+)", cell.upper())
    if not m:
        return 0, 0
    col_str, row_str = m.group(1), m.group(2)
    col = 0
    for c in col_str:
        col = col * 26 + (ord(c) - ord("A"))
    return int(row_str) - 1, col


def fetch_test_progress(qa_card: dict, cell: str = "K14") -> float | None:
    """
    QA카드의 테스트케이스 구글시트에서 진행률(%)을 읽어온다.
    Returns: 진행률 (예: 58.4) 또는 None (읽기 실패)
    """
    url = _find_testcase_sheet_url(qa_card)
    if not url:
        return None

    sheet_id, gid = _parse_sheet_id_and_gid(url)
    if not sheet_id:
        return None

    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    try:
        resp = requests.get(csv_url, timeout=15)
        if not resp.ok:
            print(f"      ⚠ 구글시트 접근 실패: HTTP {resp.status_code}")
            return None

        row_idx, col_idx = _cell_to_index(cell)
        reader = csv.reader(io.StringIO(resp.text))
        for i, row in enumerate(reader):
            if i == row_idx:
                if col_idx < len(row):
                    raw = row[col_idx].strip().replace("%", "")
                    try:
                        return float(raw)
                    except ValueError:
                        print(f"      ⚠ 진행률 파싱 실패: '{row[col_idx]}'")
                        return None
                break
    except Exception as e:
        print(f"      ⚠ 구글시트 읽기 오류: {e}")
    return None


# ── QA카드 Description 파싱 ──────────────────────────────────────────────

# 월 이름 → 숫자 매핑
_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_flexible(text: str, default_year: int = None) -> str | None:
    """
    다양한 날짜 형식을 YYYY-MM-DD로 변환.
    지원: '2026-04-10', '4/10', '3/9', 'Mar 27th', 'Apr 1st'
    """
    from datetime import datetime
    text = text.strip().rstrip("?")

    # YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # M/D (예: 3/9, 4/10)
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", text)
    if m:
        year = default_year or datetime.now().year
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    # Mar 27th, Apr 1st 등
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2})", text)
    if m:
        month_str = m.group(1).lower()[:3]
        day = int(m.group(2))
        month = _MONTH_MAP.get(month_str)
        if month:
            year = default_year or datetime.now().year
            return f"{year}-{month:02d}-{day:02d}"

    return None


def parse_release_date(qa_card: dict) -> str | None:
    """QA카드 Description에서 '릴리즈' 날짜를 추출한다. YYYY-MM-DD 반환."""
    description = qa_card.get("description") or ""
    for line in description.splitlines():
        stripped = line.strip().lstrip("*").strip()
        # '릴리즈 : ...' 또는 '릴리즈: ...' 패턴
        if re.match(r"릴리즈\s*:", stripped):
            date_part = stripped.split(":", 1)[1].strip()
            parsed = _parse_date_flexible(date_part)
            if parsed:
                return parsed
        # '배포 : ...' 패턴도 지원
        if re.match(r"(sdk\s*)?배포\s*:", stripped, re.IGNORECASE):
            date_part = stripped.split(":", 1)[1].strip()
            parsed = _parse_date_flexible(date_part)
            if parsed:
                return parsed
    return None


VIEW_TARGET_STATES = ["Backlog", "Todo", "In Progress", "In Review"]


def sync_views(cards_by_manager: dict[str, list[dict]]) -> dict:
    """
    QA카드 상태에 따라 뷰를 자동 생성/삭제한다.
    - Backlog/Todo/In Progress/In Review → 뷰 생성 (이미 있으면 스킵)
    - Done → 뷰 삭제 (없으면 스킵)

    Returns: {"created": [...], "deleted": [...], "failed": [...]}
    """
    from tools.manage_linear_views import create_views_for_card, delete_views_for_card

    all_cards = [c for cards in cards_by_manager.values() for c in cards]
    # 중복 제거 (같은 카드가 여러 매니저에 걸릴 수 있음)
    seen = set()
    unique_cards = []
    for c in all_cards:
        if c["identifier"] not in seen:
            seen.add(c["identifier"])
            unique_cards.append(c)

    created = []
    deleted = []
    failed = []

    for card in unique_cards:
        state = card["state"]["name"]
        identifier = card["identifier"]

        if state in VIEW_TARGET_STATES:
            try:
                result = create_views_for_card(identifier)
                for name in result["created"]:
                    created.append(f"{name} [{identifier}]")
            except Exception as e:
                failed.append({"card": identifier, "action": "생성", "reason": str(e)})

        elif state == "Done":
            try:
                result = delete_views_for_card(identifier)
                for name in result["success"]:
                    deleted.append(name)
                for item in result["failed"]:
                    failed.append({"card": identifier, "action": "삭제", "reason": item["reason"]})
            except Exception as e:
                failed.append({"card": identifier, "action": "삭제", "reason": str(e)})

    return {"created": created, "deleted": deleted, "failed": failed}


def prepare_qa_card_data(qa_card: dict, config: dict) -> dict:
    """
    하나의 QA카드에 대해 하위 이슈 조회 → 분석 → 대시보드 생성까지 수행.
    Slack 전송에 필요한 data dict를 반환한다.
    """
    from src.linear_client import LinearClient
    from src.analyzer import analyze
    from src.dashboard_generator import generate_dashboard, _load_checklist

    client = LinearClient()
    issues = client.get_child_issues(qa_card["id"])

    # Description에서 릴리즈 날짜 추출
    release_date = parse_release_date(qa_card)
    if release_date:
        config = {**config, "project": {**config.get("project", {}), "release_date": release_date}}
        print(f"      릴리즈 날짜 (Description): {release_date}")

    data = analyze(issues, config)
    data["project_name"] = qa_card["title"]
    data["qa_card"] = qa_card

    # 구글시트에서 테스트 진행률 읽기
    progress_cell = config.get("linear", {}).get("test_progress_cell", "K14")
    sheet_progress = fetch_test_progress(qa_card, cell=progress_cell)
    if sheet_progress is not None:
        data["progress"]["pct"] = sheet_progress
        data["progress"]["source"] = "google_sheet"
        print(f"      테스트 진행률 (구글시트): {sheet_progress}%")
    else:
        data["progress"]["source"] = "linear"

    # 테스트케이스 시트 URL도 저장
    tc_url = _find_testcase_sheet_url(qa_card)
    if tc_url:
        data["testcase_sheet_url"] = tc_url

    dash_cfg = config.get("dashboard", {})
    checklist_path = dash_cfg.get("checklist_path", "deployment_checklist.md")
    data["max_bugs_display"] = dash_cfg.get("max_bugs_display", 50)
    data["trend_days"] = dash_cfg.get("trend_days", 14)
    data["deployment_checklist"] = _load_checklist(checklist_path)
    data["dashboard_path"] = generate_dashboard(
        data, dash_cfg.get("output_dir", "output"), checklist_path
    )
    return data
