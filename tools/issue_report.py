"""Google Sheets 탭명에서 QA카드 번호를 추출하고, 하위 이슈를 분석하여 HTML 리포트 생성"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# 프로젝트 루트를 path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gspread
from google.oauth2.service_account import Credentials

from src.linear_client import LinearClient


# ── Google Sheets 연동 ─────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

SPREADSHEET_ID = "1MJFMYdjpwnjsjIIJ6E8AJ1mgf9ch-sAEJL9gotJMvsU"


def get_gspread_client():
    """서비스 계정으로 gspread 클라이언트 생성"""
    key_path = os.environ.get("GOOGLE_SA_KEY_PATH", "qa-monitor-bot-38328028056e.json")
    if os.path.exists(key_path):
        creds = Credentials.from_service_account_file(key_path, scopes=SCOPES)
    else:
        import json
        sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
        if not sa_json:
            raise RuntimeError("Google 서비스 계정 인증 정보 없음")
        info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def extract_card_ids_from_tabs(gc) -> list[str]:
    """스프레드시트의 각 탭 이름에서 [카드번호] 추출"""
    sh = gc.open_by_key(SPREADSHEET_ID)
    card_ids = []
    for ws in sh.worksheets():
        # 탭 이름에서 [SUP-1234] 같은 패턴 추출
        match = re.search(r"\[([A-Z]+-\d+)\]", ws.title)
        if match:
            card_ids.append(match.group(1))
    return card_ids


# ── Linear 이슈 조회 ──────────────────────────────────────────────────────────

def get_issue_by_identifier(client: LinearClient, identifier: str) -> dict | None:
    """identifier(예: SUP-1841)로 이슈 UUID 포함 정보 조회"""
    query = """
    query($filter: IssueFilter!) {
        issues(first: 1, filter: $filter) {
            nodes {
                id
                identifier
                title
                description
                state { name type }
                assignee { name displayName }
                labels { nodes { name } }
            }
        }
    }
    """
    # identifier에서 팀 키와 번호 분리
    team_key, number = identifier.rsplit("-", 1)
    data = client._query(query, {
        "filter": {"number": {"eq": int(number)}, "team": {"key": {"eq": team_key}}}
    })
    nodes = data["issues"]["nodes"]
    return nodes[0] if nodes else None


def get_all_child_issues(client: LinearClient, parent_id: str) -> list[dict]:
    """하위 이슈 조회 (description 포함)"""
    query = """
    query($parentId: String!, $after: String) {
        issue(id: $parentId) {
            children(first: 250, after: $after) {
                pageInfo { hasNextPage endCursor }
                nodes {
                    id
                    identifier
                    title
                    description
                    priority
                    priorityLabel
                    url
                    createdAt
                    updatedAt
                    state { name type }
                    assignee { name displayName }
                    labels { nodes { name color } }
                    children(first: 250) {
                        nodes {
                            id
                            identifier
                            title
                            description
                            priority
                            priorityLabel
                            url
                            createdAt
                            updatedAt
                            state { name type }
                            assignee { name displayName }
                            labels { nodes { name color } }
                        }
                    }
                }
            }
        }
    }
    """
    issues = []
    cursor = None
    while True:
        data = client._query(query, {"parentId": parent_id, "after": cursor})
        conn = data["issue"]["children"]
        for node in conn["nodes"]:
            grandchildren = node.pop("children", {}).get("nodes", [])
            issues.append(node)
            issues.extend(grandchildren)
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return issues


# ── 분석 ──────────────────────────────────────────────────────────────────────

def classify_platform(issue: dict) -> str:
    """이슈 제목/라벨에서 플랫폼 추출"""
    title = (issue.get("title") or "").lower()
    labels = [l["name"].lower() for l in issue.get("labels", {}).get("nodes", [])]
    all_text = title + " " + " ".join(labels)

    if "android" in all_text and "ios" in all_text:
        return "Both"
    if "android" in all_text:
        return "Android"
    if "ios" in all_text:
        return "iOS"
    if "web" in all_text or "fe" in all_text or "front" in all_text:
        return "Web"
    if "server" in all_text or "api" in all_text or "backend" in all_text or "be" in all_text:
        return "Server"
    return "미분류"


CAUSE_LABELS = [
    "구현 오류", "기능 정합성", "기타", "디자인 미준수", "디자인 변경",
    "배포", "세팅 실수", "외부 환경", "요구사항 미정의", "제품 이해",
    "테스트 실수", "테스트 환경", "OS", "PRD 미준수",
]


def classify_cause(issue: dict) -> str:
    """이슈 라벨에서 'QA 이슈 원인 분류' 하위 라벨 추출"""
    labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
    for label in labels:
        if label in CAUSE_LABELS:
            return label
    return "미분류"


def get_resolution_status(issue: dict) -> str:
    """해결 상태 — Linear 워크플로우 상태를 그대로 반환"""
    return issue.get("state", {}).get("name", "") or "미분류"


def analyze_issues(all_issues: dict[str, list[dict]]) -> dict:
    """전체 이슈 분석 통계"""
    stats = {
        "total": 0,
        "by_platform": defaultdict(int),
        "by_priority": defaultdict(int),
        "by_status": defaultdict(int),
        "by_cause": defaultdict(int),
        "by_card": {},
    }

    for card_id, issues in all_issues.items():
        card_stats = {
            "total": len(issues),
            "by_platform": defaultdict(int),
            "by_priority": defaultdict(int),
            "by_status": defaultdict(int),
            "by_cause": defaultdict(int),
        }
        for issue in issues:
            platform = classify_platform(issue)
            priority = issue.get("priorityLabel", "No priority")
            status = get_resolution_status(issue)
            cause = classify_cause(issue)

            stats["total"] += 1
            stats["by_platform"][platform] += 1
            stats["by_priority"][priority] += 1
            stats["by_status"][status] += 1
            stats["by_cause"][cause] += 1

            card_stats["by_platform"][platform] += 1
            card_stats["by_priority"][priority] += 1
            card_stats["by_status"][status] += 1
            card_stats["by_cause"][cause] += 1

        stats["by_card"][card_id] = card_stats

    return stats


# ── HTML 생성 ─────────────────────────────────────────────────────────────────

PRIORITY_COLORS = {
    "Urgent": "#ef4444",
    "High": "#f97316",
    "Medium": "#eab308",
    "Low": "#3b82f6",
    "No priority": "#9ca3af",
}

STATUS_COLORS = {
    "Backlog": "#94a3b8",
    "Todo": "#ef4444",
    "In Progress": "#f97316",
    "In Review": "#eab308",
    "개발자 QA DONE": "#a855f7",
    "Staging QA DONE": "#22d3ee",
    "Prodmini QA DONE": "#6366f1",
    "Prod QA DONE": "#3b82f6",
    "Done": "#22c55e",
    "Canceled": "#9ca3af",
    "Duplicate": "#9ca3af",
    "Can't Reproduce": "#9ca3af",
    "Not a Bug": "#9ca3af",
    "Won't Fix": "#9ca3af",
}

PLATFORM_COLORS = {
    "Android": "#3ddc84",
    "iOS": "#007aff",
    "Both": "#8b5cf6",
    "Web": "#f59e0b",
    "Server": "#6366f1",
    "미분류": "#9ca3af",
}


def generate_html(all_issues: dict[str, list[dict]], card_titles: dict[str, str], stats: dict) -> str:
    """분석 결과를 HTML로 생성 — QA카드별 섹션 (차트 + 테이블)"""
    import json as _json

    priority_order_list = ["Urgent", "High", "Medium", "Low", "No priority"]
    priority_sort = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3, "No priority": 4}
    cause_bar_colors = ['#3b82f6','#ef4444','#22c55e','#f59e0b','#8b5cf6','#ec4899','#14b8a6','#6366f1','#f43f5e','#06b6d4','#84cc16','#fb923c','#9ca3af','#475569']

    # QA카드별 섹션 + 차트 JS
    card_sections = ""
    chart_js = ""
    card_idx = 0

    for card_id, issues in all_issues.items():
        title = card_titles.get(card_id, card_id)
        card_stat = stats["by_card"].get(card_id, {})

        # 우선순위 높은 순 정렬
        issues = sorted(issues, key=lambda i: priority_sort.get(i.get("priorityLabel", "No priority"), 9))

        # 카드별 차트 데이터
        c_priority = {k: card_stat.get("by_priority", {}).get(k, 0) for k in priority_order_list if card_stat.get("by_priority", {}).get(k, 0) > 0}
        c_status = dict(card_stat.get("by_status", {}))
        c_platform = dict(card_stat.get("by_platform", {}))
        c_cause = dict(sorted(card_stat.get("by_cause", {}).items(), key=lambda x: -x[1]))

        # 우선순위별 그룹화
        grouped = defaultdict(list)
        for issue in issues:
            p = issue.get("priorityLabel", "No priority")
            grouped[p].append(issue)

        priority_groups = ""
        for p_name in priority_order_list:
            group_issues = grouped.get(p_name)
            if not group_issues:
                continue
            p_color = PRIORITY_COLORS.get(p_name, "#9ca3af")
            group_rows = ""
            for issue in group_issues:
                platform = classify_platform(issue)
                priority = issue.get("priorityLabel", "No priority")
                status = get_resolution_status(issue)
                cause = classify_cause(issue)
                assignee = ""
                if issue.get("assignee"):
                    assignee = issue["assignee"].get("displayName") or issue["assignee"].get("name") or ""
                s_color = STATUS_COLORS.get(status, "#6b7280")
                pl_color = PLATFORM_COLORS.get(platform, "#9ca3af")
                group_rows += f"""
                <tr>
                    <td><a href="{issue.get('url', '#')}" target="_blank">{issue.get('identifier', '')}</a></td>
                    <td class="title-cell">{issue.get('title', '')}</td>
                    <td><span class="badge" style="background:{pl_color}">{platform}</span></td>
                    <td><span class="badge" style="background:{s_color}">{status}</span></td>
                    <td>{cause}</td>
                    <td>{assignee}</td>
                </tr>"""

            is_high = p_name in ("Urgent", "High")
            priority_groups += f"""
            <details {"open" if is_high else ""}>
                <summary><span class="badge" style="background:{p_color}">{p_name}</span> {len(group_issues)}건</summary>
                <table>
                    <thead><tr><th>ID</th><th>제목</th><th>플랫폼</th><th>상태</th><th>원인</th><th>담당자</th></tr></thead>
                    <tbody>{group_rows}</tbody>
                </table>
            </details>"""

        # 섹션 HTML
        card_sections += f"""
    <div class="card-section">
        <h2 class="section-title">{card_id} — {title} <span class="issue-count">{len(issues)}건</span></h2>
        <div class="chart-grid">
            <div class="chart-card">
                <h3>우선순위</h3>
                <div class="chart-container-bar"><canvas id="priority_{card_idx}"></canvas></div>
            </div>
            <div class="chart-card">
                <h3>해결 상태</h3>
                <div class="chart-container-bar"><canvas id="status_{card_idx}"></canvas></div>
            </div>
            <div class="chart-card">
                <h3>플랫폼</h3>
                <div class="chart-container-bar"><canvas id="platform_{card_idx}"></canvas></div>
            </div>
            <div class="chart-card">
                <h3>이슈 원인</h3>
                <div class="chart-container-bar"><canvas id="cause_{card_idx}"></canvas></div>
            </div>
        </div>
        <div class="table-wrapper">
            {priority_groups}
        </div>
    </div>"""

        # 차트 JS
        chart_js += f"""
new Chart(document.getElementById('priority_{card_idx}'), {{
    type: 'bar', data: {{ labels: {_json.dumps(list(c_priority.keys()))}, datasets: [{{ data: {list(c_priority.values())}, backgroundColor: {[PRIORITY_COLORS.get(k, '#9ca3af') for k in c_priority.keys()]} }}] }}, options: barOpts }});
new Chart(document.getElementById('status_{card_idx}'), {{
    type: 'bar', data: {{ labels: {_json.dumps(list(c_status.keys()))}, datasets: [{{ data: {list(c_status.values())}, backgroundColor: {[STATUS_COLORS.get(k, '#6b7280') for k in c_status.keys()]} }}] }}, options: barOpts }});
new Chart(document.getElementById('platform_{card_idx}'), {{
    type: 'bar', data: {{ labels: {_json.dumps(list(c_platform.keys()))}, datasets: [{{ data: {list(c_platform.values())}, backgroundColor: {[PLATFORM_COLORS.get(k, '#9ca3af') for k in c_platform.keys()]} }}] }}, options: barOpts }});
new Chart(document.getElementById('cause_{card_idx}'), {{
    type: 'bar', data: {{ labels: {_json.dumps(list(c_cause.keys()))}, datasets: [{{ data: {list(c_cause.values())}, backgroundColor: {cause_bar_colors[:len(c_cause)]} }}] }}, options: barOpts }});
"""
        card_idx += 1

    # 카드별 요약 테이블
    card_summary_rows = ""
    for card_id, card_stat in stats["by_card"].items():
        title = card_titles.get(card_id, card_id)
        card_summary_rows += f"""
        <tr>
            <td><strong>{card_id}</strong></td>
            <td class="title-cell">{title}</td>
            <td>{card_stat['total']}</td>
            <td>{'、'.join(f'{k}:{v}' for k, v in sorted(card_stat['by_platform'].items(), key=lambda x: -x[1]))}</td>
            <td>{'、'.join(f'{k}:{v}' for k, v in sorted(card_stat['by_status'].items(), key=lambda x: -x[1]))}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QA 이슈 분석 리포트</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8fafc; color: #1e293b; padding: 24px; }}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ font-size: 24px; margin-bottom: 8px; color: #0f172a; }}
.subtitle {{ color: #64748b; margin-bottom: 24px; font-size: 14px; }}
.card-section {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 28px; }}
.section-title {{ font-size: 18px; color: #0f172a; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 2px solid #e2e8f0; }}
.issue-count {{ font-size: 14px; color: #64748b; font-weight: 400; margin-left: 8px; }}
.chart-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }}
.chart-card {{ background: #f8fafc; border-radius: 8px; padding: 14px; }}
.chart-card h3 {{ font-size: 12px; color: #64748b; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }}
.chart-container-bar {{ position: relative; height: 180px; }}
.table-wrapper {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ background: #f1f5f9; padding: 10px 12px; text-align: left; font-weight: 600; color: #475569; border-bottom: 2px solid #e2e8f0; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #f1f5f9; }}
tr:hover {{ background: #f8fafc; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; color: white; font-size: 11px; font-weight: 600; }}
.title-cell {{ max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
a {{ color: #2563eb; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
details {{ margin-bottom: 8px; }}
summary {{ cursor: pointer; padding: 8px 12px; background: #f8fafc; border-radius: 6px; font-size: 14px; font-weight: 600; user-select: none; }}
summary:hover {{ background: #f1f5f9; }}
details[open] summary {{ margin-bottom: 4px; }}
details table {{ margin-top: 4px; }}
.summary-wrapper {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 28px; overflow-x: auto; }}
.summary-wrapper h2 {{ font-size: 16px; margin-bottom: 12px; color: #0f172a; }}
</style>
</head>
<body>
<div class="container">
    <h1>QA 이슈 분석 리포트</h1>
    <p class="subtitle">생성일: 2026-05-05 | 총 {stats['total']}건 (QA카드 {len(all_issues)}개)</p>

    <!-- QA카드별 요약 -->
    <div class="summary-wrapper">
        <h2>QA카드별 요약</h2>
        <table>
            <thead><tr><th>카드</th><th>제목</th><th>이슈 수</th><th>플랫폼</th><th>상태</th></tr></thead>
            <tbody>{card_summary_rows}</tbody>
        </table>
    </div>

    <!-- QA카드별 섹션 -->
    {card_sections}
</div>

<script>
const barOpts = {{ responsive: true, maintainAspectRatio: false, indexAxis: 'y', plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }} }};
{chart_js}
</script>
</body>
</html>"""
    return html


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    print("Google Sheets에서 QA카드 번호 추출 중...")
    gc = get_gspread_client()
    card_ids = extract_card_ids_from_tabs(gc)
    print(f"  발견된 QA카드: {card_ids}")

    if not card_ids:
        print("QA카드 번호를 찾을 수 없습니다.")
        return

    client = LinearClient()
    all_issues: dict[str, list[dict]] = {}
    card_titles: dict[str, str] = {}

    for card_id in card_ids:
        print(f"\n{card_id} 조회 중...")
        card = get_issue_by_identifier(client, card_id)
        if not card:
            print(f"  ⚠ {card_id} 찾을 수 없음")
            continue

        card_titles[card_id] = card.get("title", "")
        print(f"  제목: {card['title']}")

        children = get_all_child_issues(client, card["id"])
        # Not a Bug 제외
        EXCLUDE_STATES = {"Not a Bug"}
        children = [i for i in children if i.get("state", {}).get("name", "") not in EXCLUDE_STATES]
        print(f"  하위 이슈: {len(children)}건")
        all_issues[card_id] = children

    if not all_issues:
        print("조회된 이슈가 없습니다.")
        return

    # 분석
    stats = analyze_issues(all_issues)
    print(f"\n총 이슈: {stats['total']}건")
    print(f"플랫폼: {dict(stats['by_platform'])}")
    print(f"우선순위: {dict(stats['by_priority'])}")
    print(f"해결 상태: {dict(stats['by_status'])}")
    print(f"이슈 원인: {dict(stats['by_cause'])}")

    # HTML 생성
    html = generate_html(all_issues, card_titles, stats)
    output_path = Path(__file__).resolve().parent.parent / "output" / "issue_report.html"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"\nHTML 리포트 생성: {output_path}")


if __name__ == "__main__":
    main()
