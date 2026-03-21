"""QA 데이터 분석 모듈"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import defaultdict


PRIORITY_ORDER = {
    "Urgent": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
    "No priority": 4,
}

PRIORITY_EMOJI = {
    "Urgent": "🔴",
    "High": "🟠",
    "Medium": "🟡",
    "Low": "🔵",
    "No priority": "⚪",
}


def _get_label_names(issue: dict) -> list[str]:
    return [label["name"] for label in issue.get("labels", {}).get("nodes", [])]


def _is_qa_card(issue: dict, qa_labels: list[str], qa_skip_states: list[str]) -> bool:
    labels = _get_label_names(issue)
    state_name = issue.get("state", {}).get("name", "")
    if state_name in qa_skip_states:
        return False
    return any(label in qa_labels for label in labels)


def _is_bug(issue: dict, bug_labels: list[str]) -> bool:
    labels = _get_label_names(issue)
    return any(label in bug_labels for label in labels)


def _is_done(issue: dict, done_states: list[str]) -> bool:
    state_name = issue.get("state", {}).get("name", "")
    return state_name in done_states


def _is_open_bug(issue: dict, bug_labels: list[str], open_states: list[str]) -> bool:
    return _is_bug(issue, bug_labels) and issue.get("state", {}).get("name", "") in open_states


def _assignee_key(issue: dict) -> str:
    a = issue.get("assignee")
    if not a:
        return "미지정"
    return a.get("displayName") or a.get("name") or "미지정"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── 메인 분석 함수 ───────────────────────────────────────────────────────────

def analyze(issues: list[dict], config: dict) -> dict:
    """전체 분석 결과를 딕셔너리로 반환"""
    cfg_linear = config.get("linear", {})
    qa_labels = cfg_linear.get("qa_labels", ["QA", "Test Case"])
    qa_done_states = cfg_linear.get("qa_done_states", ["Done", "Passed", "Verified"])
    qa_skip_states = cfg_linear.get("qa_skip_states", ["Cancelled", "N/A"])
    bug_labels = cfg_linear.get("bug_labels", ["Bug", "Defect"])
    bug_open_states = cfg_linear.get("bug_open_states", ["Triage", "Todo", "In Progress", "In Review", "Reopened"])

    exit_cfg = config.get("exit_criteria", {})

    qa_cards = [i for i in issues if _is_qa_card(i, qa_labels, qa_skip_states)]
    bug_issues = [i for i in issues if _is_bug(i, bug_labels)]
    open_bugs = [i for i in bug_issues if _is_open_bug(i, bug_labels, bug_open_states)]

    progress = _calc_progress(qa_cards, qa_done_states)
    by_assignee = _group_by_assignee(qa_cards, open_bugs, qa_done_states)
    priority_breakdown = _priority_breakdown(open_bugs)
    status_breakdown = _status_breakdown(issues)
    trend = _calc_trend(issues, days=14)
    exit_status = _check_exit_criteria(open_bugs, progress, exit_cfg)
    today_new = _today_new_issues(bug_issues)

    release_date_str = config.get("project", {}).get("release_date", "")
    dday = _calc_dday(release_date_str)

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "project_name": config.get("project", {}).get("name", "QA Project"),
        "release_date": release_date_str,
        "dday": dday,
        "progress": progress,
        "open_bugs": open_bugs,
        "open_bug_count": len(open_bugs),
        "today_new_count": today_new,
        "by_assignee": by_assignee,
        "priority_breakdown": priority_breakdown,
        "status_breakdown": status_breakdown,
        "trend": trend,
        "exit_status": exit_status,
        "all_issues": issues,
        "qa_cards": qa_cards,
        "bug_issues": bug_issues,
    }


def _calc_progress(qa_cards: list[dict], done_states: list[str]) -> dict:
    total = len(qa_cards)
    done = sum(1 for i in qa_cards if _is_done(i, done_states))
    in_progress = sum(
        1 for i in qa_cards
        if i.get("state", {}).get("type") in ("started",)
        and not _is_done(i, done_states)
    )
    not_started = total - done - in_progress
    pct = round(done / total * 100, 1) if total > 0 else 0.0

    return {
        "total": total,
        "done": done,
        "in_progress": in_progress,
        "not_started": not_started,
        "pct": pct,
    }


def _group_by_assignee(
    qa_cards: list[dict],
    open_bugs: list[dict],
    done_states: list[str],
) -> dict[str, dict]:
    result: dict[str, dict] = {}

    # QA 카드 집계
    for issue in qa_cards:
        name = _assignee_key(issue)
        assignee_info = issue.get("assignee") or {}
        if name not in result:
            result[name] = {
                "name": name,
                "email": assignee_info.get("email", ""),
                "linear_id": assignee_info.get("id", ""),
                "qa_total": 0,
                "qa_done": 0,
                "qa_pct": 0.0,
                "open_bugs": [],
            }
        result[name]["qa_total"] += 1
        if _is_done(issue, done_states):
            result[name]["qa_done"] += 1

    # 완료율 계산
    for name, data in result.items():
        t = data["qa_total"]
        data["qa_pct"] = round(data["qa_done"] / t * 100, 1) if t > 0 else 0.0

    # 오픈 버그 집계 (버그 담당자 기준)
    for bug in open_bugs:
        name = _assignee_key(bug)
        if name not in result:
            result[name] = {
                "name": name,
                "email": (bug.get("assignee") or {}).get("email", ""),
                "linear_id": (bug.get("assignee") or {}).get("id", ""),
                "qa_total": 0,
                "qa_done": 0,
                "qa_pct": 0.0,
                "open_bugs": [],
            }
        result[name]["open_bugs"].append(bug)

    # 오픈 버그 우선순위 정렬
    for data in result.values():
        data["open_bugs"].sort(
            key=lambda i: PRIORITY_ORDER.get(i.get("priorityLabel", "No priority"), 99)
        )

    return dict(sorted(result.items()))


def _priority_breakdown(open_bugs: list[dict]) -> list[dict]:
    counts: dict[str, int] = defaultdict(int)
    for bug in open_bugs:
        label = bug.get("priorityLabel") or "No priority"
        counts[label] += 1

    return [
        {
            "priority": p,
            "count": counts.get(p, 0),
            "emoji": PRIORITY_EMOJI.get(p, ""),
        }
        for p in ["Urgent", "High", "Medium", "Low", "No priority"]
    ]


def _status_breakdown(issues: list[dict]) -> list[dict]:
    counts: dict[str, int] = defaultdict(int)
    colors: dict[str, str] = {}
    for issue in issues:
        state = issue.get("state", {})
        name = state.get("name", "Unknown")
        counts[name] += 1
        if name not in colors:
            colors[name] = state.get("color", "#888888")

    return [
        {"status": k, "count": v, "color": colors.get(k, "#888888")}
        for k, v in sorted(counts.items(), key=lambda x: -x[1])
    ]


def _calc_trend(issues: list[dict], days: int = 14) -> dict:
    """일별 신규 이슈 생성 수 및 해결 수"""
    now = datetime.now(timezone.utc)
    date_labels = []
    created_per_day: dict[str, int] = defaultdict(int)
    resolved_per_day: dict[str, int] = defaultdict(int)

    for d in range(days - 1, -1, -1):
        day = (now - timedelta(days=d)).strftime("%m/%d")
        date_labels.append(day)

    for issue in issues:
        created = _parse_dt(issue.get("createdAt"))
        if created:
            day_str = created.strftime("%m/%d")
            if day_str in date_labels:
                created_per_day[day_str] += 1

        completed = _parse_dt(issue.get("completedAt"))
        if completed:
            day_str = completed.strftime("%m/%d")
            if day_str in date_labels:
                resolved_per_day[day_str] += 1

    return {
        "labels": date_labels,
        "created": [created_per_day.get(d, 0) for d in date_labels],
        "resolved": [resolved_per_day.get(d, 0) for d in date_labels],
    }


def _check_exit_criteria(
    open_bugs: list[dict], progress: dict, exit_cfg: dict
) -> list[dict]:
    urgent_count = sum(1 for b in open_bugs if b.get("priorityLabel") == "Urgent")
    high_count = sum(1 for b in open_bugs if b.get("priorityLabel") == "High")
    medium_count = sum(1 for b in open_bugs if b.get("priorityLabel") == "Medium")

    urgent_max = exit_cfg.get("urgent_bug_max", 0)
    high_max = exit_cfg.get("high_bug_max", 0)
    medium_max = exit_cfg.get("medium_bug_max", 5)
    completion_min = exit_cfg.get("test_completion_min_pct", 100)
    pass_min = exit_cfg.get("test_pass_min_pct", 95)

    criteria = [
        {
            "label": f"Urgent 버그 {urgent_max}개 이하",
            "current": f"{urgent_count}개",
            "pass": urgent_count <= urgent_max,
        },
        {
            "label": f"High 버그 {high_max}개 이하",
            "current": f"{high_count}개",
            "pass": high_count <= high_max,
        },
        {
            "label": f"Medium 버그 {medium_max}개 이하",
            "current": f"{medium_count}개",
            "pass": medium_count <= medium_max,
        },
        {
            "label": f"테스트 완료율 {completion_min}% 이상",
            "current": f"{progress['pct']}%",
            "pass": progress["pct"] >= completion_min,
        },
    ]
    return criteria


def _today_new_issues(bug_issues: list[dict]) -> int:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    for issue in bug_issues:
        created = _parse_dt(issue.get("createdAt"))
        if created and created >= today_start:
            count += 1
    return count


def _calc_dday(release_date_str: str) -> str | None:
    if not release_date_str:
        return None
    try:
        release = datetime.strptime(release_date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        diff = (release - today).days
        if diff > 0:
            return f"D-{diff}"
        elif diff == 0:
            return "D-Day"
        else:
            return f"D+{abs(diff)}"
    except ValueError:
        return None
