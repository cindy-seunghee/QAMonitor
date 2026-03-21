#!/usr/bin/env python3
"""
QA Monitor — 메인 진입점

사용법:
  python main.py --run-now        지금 바로 분석 + Slack 발송 + 대시보드 생성
  python main.py --dashboard-only 대시보드만 생성 (Slack 발송 없음)
  python main.py --schedule       스케줄러 실행 (매일 지정 시각 자동 발송)
  python main.py --test-slack     Slack 연결 테스트
  python main.py --test-linear    Linear 연결 테스트
"""

import argparse
import os
import sys
import yaml
from dotenv import load_dotenv

load_dotenv()


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_issues(config: dict) -> list[dict]:
    """Linear에서 이슈 목록을 가져온다"""
    from src.linear_client import LinearClient

    client = LinearClient()
    cfg = config.get("linear", {})
    project_id = cfg.get("project_id", "").strip()
    cycle_id = cfg.get("cycle_id", "").strip()
    team_id = cfg.get("team_id", "").strip()

    if project_id:
        print(f"  Linear 프로젝트 이슈 조회 중... (project_id={project_id})")
        return client.get_project_issues(project_id)
    elif cycle_id:
        print(f"  Linear 사이클 이슈 조회 중... (cycle_id={cycle_id})")
        return client.get_cycle_issues(cycle_id)
    elif team_id:
        print(f"  Linear 팀 이슈 조회 중... (team_id={team_id})")
        return client.get_team_issues(team_id)
    else:
        print("오류: config.yaml에 team_id, project_id, cycle_id 중 하나를 입력하세요.")
        sys.exit(1)


def _resolve_user_map(raw: dict) -> dict:
    """
    user_map의 두 가지 형식을 통합.
      구형: "Cindy": "U03KW6G2TJ5"
      신형: "Cindy": {slack_id: "...", send_time: "15:00"}
    → 항상 dict 형식으로 반환.
    """
    resolved = {}
    for name, val in (raw or {}).items():
        if isinstance(val, str):
            resolved[name] = {"slack_id": val}
        else:
            resolved[name] = val
    return resolved


def _get_assignee_schedules(user_map: dict, base_time: str) -> dict[str, str]:
    """QAM별 send_time 추출. 미설정 시 base_time 사용."""
    return {
        name: cfg.get("send_time", base_time)
        for name, cfg in user_map.items()
    }


def _prepare_data(config: dict) -> dict:
    """Linear 조회 + 분석 + 대시보드 생성까지 공통 처리."""
    from src.analyzer import analyze
    from src.dashboard_generator import generate_dashboard, _load_checklist

    print("[1/3] Linear 이슈 조회")
    issues = fetch_issues(config)
    print(f"      총 {len(issues)}개 이슈 로드 완료")

    print("[2/3] 데이터 분석")
    data = analyze(issues, config)
    print(f"      QA 카드 {len(data['qa_cards'])}개 | 버그 {data['open_bug_count']}건 미해결")

    print("[3/3] HTML 대시보드 생성")
    dash_cfg = config.get("dashboard", {})
    output_dir = dash_cfg.get("output_dir", "output")
    checklist_path = dash_cfg.get("checklist_path", "deployment_checklist.md")
    data["max_bugs_display"] = dash_cfg.get("max_bugs_display", 50)
    data["trend_days"] = dash_cfg.get("trend_days", 14)
    data["deployment_checklist"] = _load_checklist(checklist_path)
    data["dashboard_path"] = generate_dashboard(data, output_dir, checklist_path)
    print(f"      저장됨: {data['dashboard_path']}")
    return data


def run(config: dict) -> str:
    """채널 전체 요약 메시지 전송. 대시보드 경로를 반환."""
    from src.slack_notifier import SlackNotifier

    print("─" * 50)
    data = _prepare_data(config)

    print("[4/4] Slack 채널 요약 메시지 전송")
    slack_cfg = config.get("slack", {})
    channel = slack_cfg.get("summary_channel") or os.environ.get("SLACK_CHANNEL_ID", "")
    user_map = _resolve_user_map(slack_cfg.get("user_map") or {})

    if not channel:
        print("      ⚠ Slack 채널 미설정 — 전송 건너뜀")
    else:
        notifier = SlackNotifier()
        notifier.send_daily_report(
            data=data,
            channel=channel,
            user_map=user_map,
            template_path=slack_cfg.get("template_path", "slack_template.md"),
            dashboard_path=data.get("dashboard_path"),
        )

    print("─" * 50)
    print("완료!")
    return data.get("dashboard_path", "")


def run_for_assignee(config: dict, assignee_name: str) -> None:
    """특정 QAM의 개인 채널 메시지만 전송."""
    from src.slack_notifier import SlackNotifier

    print("─" * 50)
    print(f"[QAM 개인] {assignee_name} 메시지 준비")
    data = _prepare_data(config)

    slack_cfg = config.get("slack", {})
    channel = slack_cfg.get("summary_channel") or os.environ.get("SLACK_CHANNEL_ID", "")
    user_map = _resolve_user_map(slack_cfg.get("user_map") or {})

    if not channel:
        print("      ⚠ Slack 채널 미설정")
        return

    notifier = SlackNotifier()
    notifier.send_assignee_message(
        data=data,
        channel=channel,
        assignee_name=assignee_name,
        user_map=user_map,
    )
    print("─" * 50)
    print(f"{assignee_name} 메시지 전송 완료!")


def main():
    parser = argparse.ArgumentParser(description="QA Monitor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-now", action="store_true", help="지금 바로 실행 (전체 요약)")
    group.add_argument("--run-for", metavar="NAME", help="특정 QAM 개인 메시지만 즉시 전송")
    group.add_argument("--dashboard-only", action="store_true", help="대시보드만 생성")
    group.add_argument("--schedule", action="store_true", help="스케줄러 시작")
    group.add_argument("--test-slack", action="store_true", help="Slack 연결 테스트")
    group.add_argument("--test-linear", action="store_true", help="Linear 연결 테스트")
    parser.add_argument("--config", default="config.yaml", help="설정 파일 경로")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.test_slack:
        from src.slack_notifier import SlackNotifier
        SlackNotifier().test_connection()

    elif args.test_linear:
        from src.linear_client import LinearClient
        client = LinearClient()
        me = client.get_viewer()
        print(f"Linear 연결 성공: {me['name']} ({me['email']})")

    elif args.run_now:
        run(config)

    elif args.run_for:
        run_for_assignee(config, args.run_for)

    elif args.dashboard_only:
        from src.analyzer import analyze
        from src.dashboard_generator import generate_dashboard
        print("[1/2] Linear 이슈 조회")
        issues = fetch_issues(config)
        print("[2/2] 대시보드 생성")
        data = analyze(issues, config)
        dash_cfg = config.get("dashboard", {})
        output_dir = dash_cfg.get("output_dir", "output")
        checklist_path = dash_cfg.get("checklist_path", "deployment_checklist.md")
        data["max_bugs_display"] = dash_cfg.get("max_bugs_display", 50)
        data["trend_days"] = dash_cfg.get("trend_days", 14)
        path = generate_dashboard(data, output_dir, checklist_path)
        print(f"완료: {path}")

    elif args.schedule:
        scheduler_cfg = config.get("scheduler", {})
        base_time = scheduler_cfg.get("send_time", "09:00")
        timezone = scheduler_cfg.get("timezone", "Asia/Seoul")
        slack_cfg = config.get("slack", {})
        user_map = _resolve_user_map(slack_cfg.get("user_map") or {})
        assignee_schedules = _get_assignee_schedules(user_map, base_time)

        print("스케줄 등록:")
        from src.scheduler import start_scheduler
        start_scheduler(
            run_summary_fn=lambda: run(config),
            run_assignee_fn=lambda name: run_for_assignee(config, name),
            base_time=base_time,
            assignee_schedules=assignee_schedules,
            timezone=timezone,
        )


if __name__ == "__main__":
    main()
