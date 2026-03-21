"""QA카드 디스커버리 — 매니저별 QA카드 조회·분류·데이터 준비"""

from __future__ import annotations


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

    data = analyze(issues, config)
    data["project_name"] = qa_card["title"]
    data["qa_card"] = qa_card

    dash_cfg = config.get("dashboard", {})
    checklist_path = dash_cfg.get("checklist_path", "deployment_checklist.md")
    data["max_bugs_display"] = dash_cfg.get("max_bugs_display", 50)
    data["trend_days"] = dash_cfg.get("trend_days", 14)
    data["deployment_checklist"] = _load_checklist(checklist_path)
    data["dashboard_path"] = generate_dashboard(
        data, dash_cfg.get("output_dir", "output"), checklist_path
    )
    return data
