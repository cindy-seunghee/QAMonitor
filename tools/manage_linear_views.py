#!/usr/bin/env python3
"""
tools/manage_linear_views.py

QA 카드의 하위 이슈를 담당자별 Linear 필터 뷰로 생성/삭제합니다.

CLI 직접 실행:
  python tools/manage_linear_views.py --parent SUP-1145     # 뷰 생성
  python tools/manage_linear_views.py --cleanup SUP-1145    # 뷰 일괄 삭제

main.py에서도 함수를 import하여 자동 실행합니다.
"""

from __future__ import annotations

import argparse
import os
import sys

# CLI 직접 실행 시 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

TARGET_STATES = ["Todo", "Backlog", "In Progress", "In Review"]


def _client():
    from src.linear_client import LinearClient
    return LinearClient()


# ── 조회 ──────────────────────────────────────────────────────────────────

def get_issue_info(identifier: str) -> dict:
    """이슈 UUID + 팀 정보 + 하위 이슈(2단계) 조회"""
    query = """
    query($id: String!) {
        issue(id: $id) {
            id
            title
            team { id key }
            children(first: 250) {
                nodes {
                    id
                    assignee { id name displayName email }
                    children(first: 250) {
                        nodes { id }
                    }
                }
            }
        }
    }
    """
    return _client()._query(query, {"id": identifier})["issue"]


def _get_all_child_ids(issue: dict) -> list[str]:
    """1단계 + 2단계 하위 이슈의 UUID를 모두 수집"""
    parent_uuid = issue["id"]
    child_ids = [parent_uuid]
    for child in issue.get("children", {}).get("nodes", []):
        child_ids.append(child["id"])
        for grandchild in child.get("children", {}).get("nodes", []):
            child_ids.append(grandchild.get("id", ""))
    return child_ids


def get_valid_states(team_id: str) -> list[str]:
    """팀의 워크플로우에서 target 상태 이름만 추출"""
    query = """
    query($teamId: String!) {
        team(id: $teamId) {
            states { nodes { id name } }
        }
    }
    """
    states = _client()._query(query, {"teamId": team_id})["team"]["states"]["nodes"]
    return [s["name"] for s in states if s["name"] in TARGET_STATES]


def get_existing_views(parent_identifier: str) -> dict[str, dict]:
    """이미 생성된 '[parent_identifier]' 뷰를 이름 → {id, slugId} 매핑으로 반환"""
    query = """
    query($after: String) {
        customViews(first: 250, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes { id name slugId }
        }
    }
    """
    client = _client()
    views: dict[str, dict] = {}
    cursor = None
    while True:
        data = client._query(query, {"after": cursor})
        conn = data["customViews"]
        for v in conn["nodes"]:
            if f"[{parent_identifier}]" in v["name"]:
                views[v["name"]] = v
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return views


# ── 생성 ──────────────────────────────────────────────────────────────────

def create_view(name: str, team_id: str, filter_data: dict) -> dict:
    mutation = """
    mutation($input: CustomViewCreateInput!) {
        customViewCreate(input: $input) {
            success
            customView { id name slugId }
        }
    }
    """
    result = _client()._query(mutation, {
        "input": {
            "name": name,
            "teamId": team_id,
            "filterData": filter_data,
            "shared": True,
        }
    })["customViewCreate"]

    if not result["success"]:
        raise RuntimeError(f"뷰 생성 실패: {name}")
    return result["customView"]


def create_views_for_card(identifier: str) -> dict:
    """
    QA카드의 전체 잔여이슈 뷰 + 내 잔여이슈(Current User) 뷰를 생성한다.
    이미 있으면 스킵.
    Returns: {"created": [...], "skipped": [...], "views": [{"name", "url"}, ...]}
    """
    issue = get_issue_info(identifier)
    parent_uuid = issue["id"]
    team_id = issue["team"]["id"]

    # 1단계 + 2단계 하위 이슈의 parent UUID 수집
    child_parent_ids = _get_all_child_ids(issue)

    valid_states = get_valid_states(team_id)
    existing = get_existing_views(identifier)

    created = []
    skipped = []
    views = []

    # 기존 뷰가 있으면 삭제 후 재생성 (필터 업데이트를 위해)
    for view_name, view_info in existing.items():
        try:
            delete_view(view_info["id"])
        except Exception:
            pass

    # 전체 잔여이슈 뷰
    total_view_name = f"전체 잔여이슈 [{identifier}]"
    filter_data = {
        "parent": {"id": {"in": child_parent_ids}},
        "state": {"name": {"in": valid_states}},
    }
    view = create_view(total_view_name, team_id, filter_data)
    slug = view.get("slugId") or view["id"]
    views.append({"name": "전체", "url": f"https://linear.app/buzzvil/view/{slug}"})
    created.append("전체")

    # 내 잔여이슈 뷰 (Current User)
    my_view_name = f"내 잔여이슈 [{identifier}]"
    filter_data = {
        "assignee": {"isMe": {"eq": True}},
        "parent": {"id": {"in": child_parent_ids}},
        "state": {"name": {"in": valid_states}},
    }
    view = create_view(my_view_name, team_id, filter_data)
    slug = view.get("slugId") or view["id"]
    views.append({"name": "내 이슈", "url": f"https://linear.app/buzzvil/view/{slug}"})
    created.append("내 이슈")

    return {"created": created, "skipped": skipped, "views": views}


# ── 삭제 ──────────────────────────────────────────────────────────────────

def delete_view(view_id: str) -> bool:
    """커스텀 뷰 삭제. 성공 시 True 반환."""
    mutation = """
    mutation($id: String!) {
        customViewDelete(id: $id) {
            success
        }
    }
    """
    result = _client()._query(mutation, {"id": view_id})
    return result["customViewDelete"]["success"]


def delete_views_for_card(identifier: str) -> dict:
    """
    QA카드 관련 뷰를 일괄 삭제한다.
    Returns: {"success": [...], "failed": [{"name", "reason"}, ...]}
    """
    existing = get_existing_views(identifier)
    success = []
    failed = []

    for view_name, view_info in existing.items():
        try:
            ok = delete_view(view_info["id"])
            if ok:
                success.append(view_name)
            else:
                failed.append({"name": view_name, "reason": "API가 실패를 반환"})
        except Exception as e:
            failed.append({"name": view_name, "reason": str(e)})

    return {"success": success, "failed": failed}


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="담당자별 Linear 잔여이슈 필터 뷰 생성/삭제")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--parent", help="뷰 생성 — QA 카드 identifier (예: SUP-1145)")
    group.add_argument("--cleanup", metavar="PARENT", help="뷰 일괄 삭제 — QA 카드 identifier (예: SUP-1145)")
    args = parser.parse_args()

    if args.cleanup:
        print(f"\n{'─' * 50}")
        print(f"[1/2] [{args.cleanup}] 관련 뷰 조회")
        result = delete_views_for_card(args.cleanup)

        if not result["success"] and not result["failed"]:
            print(f"      삭제할 뷰가 없습니다.")
            print(f"{'─' * 50}\n")
            return

        print(f"[2/2] 뷰 삭제")
        for name in result["success"]:
            print(f"      ✓ 삭제 완료: {name}")
        for item in result["failed"]:
            print(f"      ✗ 삭제 실패: {item['name']} ({item['reason']})")

        print(f"\n{'─' * 50}")
        print(f"결과: 성공 {len(result['success'])}건 / 실패 {len(result['failed'])}건")
        if result["success"]:
            print(f"\n  [성공] 삭제된 뷰:")
            for name in result["success"]:
                print(f"    - {name}")
        if result["failed"]:
            print(f"\n  [실패] 삭제되지 않은 뷰:")
            for item in result["failed"]:
                print(f"    - {item['name']}: {item['reason']}")
        print(f"{'─' * 50}\n")
        return

    # --parent: 뷰 생성
    print(f"\n{'─' * 50}")
    print(f"뷰 생성: {args.parent}")
    result = create_views_for_card(args.parent)

    if not result["views"]:
        print("  하위 이슈가 없습니다.")
    else:
        for name in result["created"]:
            print(f"  ✓ {name} (신규 생성)")
        for name in result["skipped"]:
            print(f"  ● {name} (이미 존재 — 스킵)")
        print(f"\n결과: 신규 {len(result['created'])}건, 스킵 {len(result['skipped'])}건")
        print(f"{'─' * 50}")
        for v in result["views"]:
            print(f"  {v['name']}: {v['url']}")

    print(f"{'─' * 50}\n")


if __name__ == "__main__":
    main()
