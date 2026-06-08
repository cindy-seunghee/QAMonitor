"""Linear GraphQL API 클라이언트"""

import os
import requests
from datetime import datetime, timedelta, timezone


LINEAR_API_URL = "https://api.linear.app/graphql"


class LinearClient:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ["LINEAR_API_KEY"]
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        })

    def _query(self, query: str, variables: dict = None) -> dict:
        resp = self.session.post(
            LINEAR_API_URL,
            json={"query": query, "variables": variables or {}},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"Linear API 오류: {data['errors']}")
        return data["data"]

    # ── 이슈 조회 ──────────────────────────────────────────────────────────

    ISSUE_FIELDS = """
        id
        identifier
        title
        description
        priority
        priorityLabel
        url
        createdAt
        updatedAt
        completedAt
        canceledAt
        dueDate
        state {
            id
            name
            type
        }
        assignee {
            id
            name
            displayName
            email
        }
        labels {
            nodes {
                id
                name
                color
            }
        }
    """

    def get_project_issues(self, project_id: str) -> list[dict]:
        query = f"""
        query($projectId: String!, $after: String) {{
            project(id: $projectId) {{
                name
                issues(first: 250, after: $after) {{
                    pageInfo {{ hasNextPage endCursor }}
                    nodes {{ {self.ISSUE_FIELDS} }}
                }}
            }}
        }}
        """
        return self._paginate_issues(query, {"projectId": project_id}, ["project", "issues"])

    def get_cycle_issues(self, cycle_id: str) -> list[dict]:
        query = f"""
        query($cycleId: String!, $after: String) {{
            cycle(id: $cycleId) {{
                name
                startsAt
                endsAt
                issues(first: 250, after: $after) {{
                    pageInfo {{ hasNextPage endCursor }}
                    nodes {{ {self.ISSUE_FIELDS} }}
                }}
            }}
        }}
        """
        return self._paginate_issues(query, {"cycleId": cycle_id}, ["cycle", "issues"])

    def get_team_issues(self, team_id: str) -> list[dict]:
        """팀 전체 이슈 (최근 60일)"""
        since = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        query = f"""
        query($teamId: String!, $after: String) {{
            team(id: $teamId) {{
                issues(
                    first: 250,
                    after: $after,
                    filter: {{ createdAt: {{ gte: "{since}" }} }}
                ) {{
                    pageInfo {{ hasNextPage endCursor }}
                    nodes {{ {self.ISSUE_FIELDS} }}
                }}
            }}
        }}
        """
        return self._paginate_issues(query, {"teamId": team_id}, ["team", "issues"])

    def _paginate_issues(self, query: str, variables: dict, path: list[str]) -> list[dict]:
        issues = []
        cursor = None
        while True:
            vars_ = {**variables, "after": cursor}
            data = self._query(query, vars_)
            node = data
            for key in path[:-1]:
                node = node[key]
            connection = node[path[-1]]
            issues.extend(connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"]:
                break
            cursor = connection["pageInfo"]["endCursor"]
        return issues

    # ── 팀 / 프로젝트 메타데이터 ───────────────────────────────────────────

    def get_team_members(self, team_id: str) -> list[dict]:
        query = """
        query($teamId: String!) {
            team(id: $teamId) {
                members {
                    nodes {
                        id
                        name
                        displayName
                        email
                    }
                }
            }
        }
        """
        data = self._query(query, {"teamId": team_id})
        return data["team"]["members"]["nodes"]

    def get_workflow_states(self, team_id: str) -> list[dict]:
        query = """
        query($teamId: String!) {
            team(id: $teamId) {
                states {
                    nodes {
                        id
                        name
                        type
                        color
                    }
                }
            }
        }
        """
        data = self._query(query, {"teamId": team_id})
        return data["team"]["states"]["nodes"]

    def get_team_info(self, team_id: str) -> dict:
        query = """
        query($teamId: String!) {
            team(id: $teamId) {
                id
                name
                key
            }
        }
        """
        return self._query(query, {"teamId": team_id})["team"]

    def get_projects(self, team_id: str) -> list[dict]:
        query = """
        query($teamId: String!) {
            team(id: $teamId) {
                projects {
                    nodes {
                        id
                        name
                        state
                        startDate
                        targetDate
                    }
                }
            }
        }
        """
        data = self._query(query, {"teamId": team_id})
        return data["team"]["projects"]["nodes"]

    def get_cycles(self, team_id: str) -> list[dict]:
        query = """
        query($teamId: String!) {
            team(id: $teamId) {
                cycles(first: 10) {
                    nodes {
                        id
                        name
                        number
                        startsAt
                        endsAt
                        completedAt
                    }
                }
            }
        }
        """
        data = self._query(query, {"teamId": team_id})
        return data["team"]["cycles"]["nodes"]

    # ── QA 카드 디스커버리 ─────────────────────────────────────────────────

    def get_qa_cards(self, qa_labels: list[str]) -> list[dict]:
        """QA 라벨이 붙은 이슈(QA 카드) 목록 조회 — 생성자·상태 포함"""
        labels_filter = ", ".join(f'"{l}"' for l in qa_labels)
        query = f"""
        query($after: String) {{
            issues(
                first: 250
                after: $after
                filter: {{ labels: {{ name: {{ in: [{labels_filter}] }} }} }}
            ) {{
                pageInfo {{ hasNextPage endCursor }}
                nodes {{
                    id
                    identifier
                    title
                    description
                    state {{ name type }}
                    assignee {{ id name displayName }}
                    creator {{ id name displayName }}
                    attachments {{ nodes {{ title url }} }}
                }}
            }}
        }}
        """
        issues: list[dict] = []
        cursor = None
        while True:
            data = self._query(query, {"after": cursor})
            conn = data["issues"]
            issues.extend(conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
        return issues

    def get_issue_by_identifier(self, identifier: str):
        """이슈 식별자(예: SUP-1841)로 이슈 제목 등 기본 정보를 조회"""
        query = f"""
        query {{
            issue(id: "{identifier}") {{
                id
                identifier
                title
                description
                state {{ name }}
            }}
        }}
        """
        data = self._query(query)
        return data.get("issue")

    def get_child_issues(self, parent_id: str) -> list[dict]:
        """부모 이슈(UUID)의 하위 이슈를 2단계까지 조회"""
        query = f"""
        query($parentId: String!, $after: String) {{
            issue(id: $parentId) {{
                children(first: 250, after: $after) {{
                    pageInfo {{ hasNextPage endCursor }}
                    nodes {{
                        {self.ISSUE_FIELDS}
                        children(first: 250) {{
                            nodes {{ {self.ISSUE_FIELDS} }}
                        }}
                    }}
                }}
            }}
        }}
        """
        issues: list[dict] = []
        cursor = None
        while True:
            data = self._query(query, {"parentId": parent_id, "after": cursor})
            conn = data["issue"]["children"]
            for node in conn["nodes"]:
                # 2단계 하위이슈 추출 후 제거
                grandchildren = node.pop("children", {}).get("nodes", [])
                issues.append(node)
                issues.extend(grandchildren)
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
        return issues

    def get_assigned_issues_without_label(
        self, assignee_name: str, required_labels: list[str]
    ) -> list[dict]:
        """assignee에게 할당됐지만 required_labels 중 하나도 없는 활성 이슈 조회"""
        query = """
        query($assigneeName: String!, $after: String) {
            issues(
                first: 250
                after: $after
                filter: {
                    assignee: { name: { eq: $assigneeName } }
                    state: { type: { nin: ["completed", "canceled"] } }
                }
            ) {
                pageInfo { hasNextPage endCursor }
                nodes {
                    id
                    identifier
                    title
                    url
                    state { name type }
                    labels { nodes { name } }
                    assignee { id name displayName }
                }
            }
        }
        """
        all_issues: list[dict] = []
        cursor = None
        while True:
            data = self._query(query, {"assigneeName": assignee_name, "after": cursor})
            conn = data["issues"]
            all_issues.extend(conn["nodes"])
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]

        # required_labels 중 하나라도 있으면 제외, "운영검증" 라벨은 QA 라벨 누락 대상에서 제외
        required_lower = {l.lower() for l in required_labels}
        exclude_labels = {"운영검증"}
        missing = []
        for issue in all_issues:
            label_names = {n["name"] for n in issue.get("labels", {}).get("nodes", [])}
            if label_names & exclude_labels:
                continue
            label_names_lower = {n.lower() for n in label_names}
            if not label_names_lower & required_lower:
                missing.append(issue)
        return missing

    def get_viewer(self) -> dict:
        query = """
        query {
            viewer {
                id
                name
                displayName
                email
            }
        }
        """
        return self._query(query)["viewer"]
