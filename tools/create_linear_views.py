#!/usr/bin/env python3
"""
tools/create_linear_views.py

QA 카드의 하위 이슈를 담당자별 Linear 필터 뷰로 생성합니다.
main.py와 독립적으로 실행됩니다.

Usage:
  python tools/create_linear_views.py --parent SUP-1145
"""

import argparse
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

LINEAR_API_URL = "https://api.linear.app/graphql"
TARGET_STATES = ["Todo", "Backlog", "In Progress", "In Review"]


def graphql(query: str, variables: dict = None) -> dict:
    api_key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not api_key:
        print("ERROR: LINEAR_API_KEY가 .env에 설정되지 않았습니다.")
        sys.exit(1)
    resp = requests.post(
        LINEAR_API_URL,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL 오류: {data['errors']}")
    return data["data"]


def get_issue_info(identifier: str) -> dict:
    """이슈 UUID + 팀 정보 + 하위 이슈 담당자 조회"""
    query = """
    query($id: String!) {
        issue(id: $id) {
            id
            title
            team { id key }
            children(first: 250) {
                nodes {
                    assignee { id name displayName email }
                }
            }
        }
    }
    """
    return graphql(query, {"id": identifier})["issue"]


def get_valid_states(team_id: str) -> list[str]:
    """팀의 워크플로우에서 target 상태 이름만 추출"""
    query = """
    query($teamId: String!) {
        team(id: $teamId) {
            states { nodes { id name } }
        }
    }
    """
    states = graphql(query, {"teamId": team_id})["team"]["states"]["nodes"]
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
    views: dict[str, dict] = {}
    cursor = None
    while True:
        data = graphql(query, {"after": cursor})
        conn = data["customViews"]
        for v in conn["nodes"]:
            if f"[{parent_identifier}]" in v["name"]:
                views[v["name"]] = v
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return views


def create_view(name: str, team_id: str, filter_data: dict) -> dict:
    mutation = """
    mutation($input: CustomViewCreateInput!) {
        customViewCreate(input: $input) {
            success
            customView { id name slugId }
        }
    }
    """
    result = graphql(mutation, {
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


def main():
    parser = argparse.ArgumentParser(description="담당자별 Linear 잔여이슈 필터 뷰 생성")
    parser.add_argument("--parent", required=True, help="QA 카드 identifier (예: SUP-1145)")
    args = parser.parse_args()

    print(f"\n{'─' * 50}")
    print(f"[1/4] 이슈 조회: {args.parent}")
    issue = get_issue_info(args.parent)
    parent_uuid = issue["id"]
    team_id = issue["team"]["id"]
    print(f"      UUID  : {parent_uuid}")
    print(f"      제목  : {issue['title']}")

    # 담당자 중복 제거 + No assignee 감지
    assignees: dict[str, dict] = {}
    has_no_assignee = False
    for child in issue["children"]["nodes"]:
        a = child.get("assignee")
        if a:
            if a["id"] not in assignees:
                assignees[a["id"]] = a
        else:
            has_no_assignee = True

    if not assignees and not has_no_assignee:
        print("      하위 이슈가 없습니다. 종료합니다.")
        return

    names = [a.get("displayName") or a["name"] for a in assignees.values()]
    if has_no_assignee:
        names.append("No assignee")
    print(f"      담당자: {', '.join(names)}")

    print(f"[2/4] 워크플로우 상태 확인")
    valid_states = get_valid_states(team_id)
    print(f"      적용 상태: {', '.join(valid_states)}")

    print(f"[3/4] 기존 뷰 확인")
    existing = get_existing_views(args.parent)
    if existing:
        print(f"      기존 뷰 {len(existing)}개 발견")
    else:
        print(f"      기존 뷰 없음")

    print(f"[4/5] 담당자별 뷰 생성")
    results = []
    created_count = 0
    skipped_count = 0

    # 담당자별 뷰
    for uid, assignee in assignees.items():
        display_name = assignee.get("displayName") or assignee["name"]
        view_name = f"{display_name} 잔여이슈 [{args.parent}]"

        if view_name in existing:
            slug = existing[view_name].get("slugId") or existing[view_name]["id"]
            view_url = f"https://linear.app/buzzvil/view/{slug}"
            results.append({"name": display_name, "url": view_url})
            print(f"      ● {display_name} (이미 존재 — 스킵)")
            skipped_count += 1
            continue

        filter_data = {
            "assignee": {"id": {"eq": uid}},
            "parent": {"id": {"eq": parent_uuid}},
            "state": {"name": {"in": valid_states}},
        }
        view = create_view(view_name, team_id, filter_data)
        slug = view.get("slugId") or view["id"]
        view_url = f"https://linear.app/buzzvil/view/{slug}"
        results.append({"name": display_name, "url": view_url})
        print(f"      ✓ {display_name} (신규 생성)")
        created_count += 1

    # No assignee 뷰
    if has_no_assignee:
        view_name = f"No assignee 잔여이슈 [{args.parent}]"

        if view_name in existing:
            slug = existing[view_name].get("slugId") or existing[view_name]["id"]
            view_url = f"https://linear.app/buzzvil/view/{slug}"
            results.append({"name": "No assignee", "url": view_url})
            print(f"      ● No assignee (이미 존재 — 스킵)")
            skipped_count += 1
        else:
            filter_data = {
                "assignee": {"null": True},
                "parent": {"id": {"eq": parent_uuid}},
                "state": {"name": {"in": valid_states}},
            }
            view = create_view(view_name, team_id, filter_data)
            slug = view.get("slugId") or view["id"]
            view_url = f"https://linear.app/buzzvil/view/{slug}"
            results.append({"name": "No assignee", "url": view_url})
            print(f"      ✓ No assignee (신규 생성)")
            created_count += 1

    print(f"\n[5/5] 완료 — 신규 {created_count}건, 스킵 {skipped_count}건")
    print(f"{'─' * 50}")
    for r in results:
        print(f"  {r['name']}: {r['url']}")
    print(f"{'─' * 50}\n")


if __name__ == "__main__":
    main()
