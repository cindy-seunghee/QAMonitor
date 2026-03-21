#!/usr/bin/env python3
"""
QA Monitor — 메인 진입점

사용법:
  python main.py --run-now        활성 QA카드 기준 분석 + Slack 발송 + 대시보드 생성
  python main.py --run-for NAME   특정 QAM의 활성 QA카드만 즉시 발송
  python main.py --dashboard-only 대시보드만 생성 (Slack 발송 없음)
  python main.py --schedule       스케줄러 실행 (매일 지정 시각 자동 발송)
  python main.py --test-slack     Slack 연결 테스트
  python main.py --test-linear    Linear 연결 테스트
"""

import argparse
import os
import sys
import traceback
from dotenv import load_dotenv

load_dotenv()


def load_config(path: str = "config.yaml") -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _notify_errors(config: dict, errors: list[dict]) -> None:
    """에러가 있으면 error_notify 대상에게 DM 발송."""
    if not errors:
        return
    try:
        from src.qa_discoverer import resolve_user_map
        from src.slack_notifier import SlackNotifier

        slack_cfg = config.get("slack", {})
        notify_name = slack_cfg.get("error_notify", "").strip()
        if not notify_name:
            return

        user_map = resolve_user_map(slack_cfg.get("user_map") or {})
        user_cfg = user_map.get(notify_name, {})
        slack_id = user_cfg.get("slack_id") if isinstance(user_cfg, dict) else user_cfg
        if not slack_id:
            print(f"  ⚠ error_notify 대상 '{notify_name}'의 slack_id를 찾을 수 없습니다.")
            return

        notifier = SlackNotifier()
        notifier.send_error_dm(slack_id, errors)
    except Exception as e:
        print(f"  ⚠ 에러 DM 발송 중 추가 오류: {e}")


def run(config: dict) -> str:
    """QA매니저별 활성(In Progress) QA카드를 발견하고 Slack 메시지를 전송한다."""
    from src.qa_discoverer import (
        discover_qa_cards, get_active_cards, get_paused_cards,
        prepare_qa_card_data, resolve_user_map, sync_views,
    )
    from src.slack_notifier import SlackNotifier

    errors: list[dict] = []

    print("─" * 50)

    # ── 1) QA카드 디스커버리 ──
    print("[1/4] QA매니저별 QA카드 현황 조회")
    try:
        cards_by_manager = discover_qa_cards(config)
    except Exception as e:
        print(f"  ✗ QA카드 조회 실패: {e}")
        errors.append({"step": "QA카드 조회", "detail": str(e)})
        _notify_errors(config, errors)
        return ""

    # ── 2) 뷰 동기화 ──
    print("\n[2/4] 담당자별 Linear 뷰 동기화")
    try:
        view_result = sync_views(cards_by_manager)
        if view_result["created"]:
            for v in view_result["created"]:
                print(f"      ✓ 뷰 생성: {v}")
        if view_result["deleted"]:
            for v in view_result["deleted"]:
                print(f"      ✓ 뷰 삭제: {v}")
        if view_result["failed"]:
            for f in view_result["failed"]:
                msg = f"{f['action']} 실패 [{f['card']}]: {f['reason']}"
                print(f"      ✗ {msg}")
                errors.append({"step": "뷰 동기화", "detail": msg})
        if not view_result["created"] and not view_result["deleted"] and not view_result["failed"]:
            print("      변경 없음")
    except Exception as e:
        print(f"  ✗ 뷰 동기화 실패: {e}")
        errors.append({"step": "뷰 동기화", "detail": str(e)})

    # ── 3-4) 활성 카드 분석 + Slack 발송 ──
    slack_cfg = config.get("slack", {})
    channel = slack_cfg.get("summary_channel") or os.environ.get("SLACK_CHANNEL_ID", "")
    user_map = resolve_user_map(slack_cfg.get("user_map") or {})
    notifier = None
    last_dashboard_path = ""

    for manager_name, cards in cards_by_manager.items():
        active = get_active_cards(cards)
        paused = get_paused_cards(cards)
        print(f"\n  {manager_name}: 진행중 {len(active)}건, 중단 {len(paused)}건")

        if not active:
            print(f"    → 진행중인 QA카드 없음 — 발송 건너뜀")
            continue

        for qa_card in active:
            card_id = qa_card['identifier']
            try:
                print(f"\n[3/4] {card_id}: {qa_card['title']}")
                data = prepare_qa_card_data(qa_card, config)
                last_dashboard_path = data["dashboard_path"]
                # 뷰 URL 연결
                card_view_urls = view_result.get("view_urls", {}).get(card_id, {})
                data["view_urls"] = card_view_urls
                print(f"      하위이슈 {len(data['all_issues'])}건 | 대시보드: {data['dashboard_path']}")
            except Exception as e:
                msg = f"{card_id} 데이터 준비 실패: {e}"
                print(f"  ✗ {msg}")
                errors.append({"step": f"데이터 준비 ({card_id})", "detail": str(e)})
                continue

            if channel:
                try:
                    print(f"[4/4] Slack 메시지 전송: {card_id}")
                    notifier = SlackNotifier()
                    notifier.send_daily_report(
                        data=data,
                        channel=channel,
                        user_map=user_map,
                        template_path=slack_cfg.get("template_path", "slack_template.md"),
                        dashboard_path=data.get("dashboard_path"),
                    )
                except Exception as e:
                    msg = f"{card_id} Slack 전송 실패: {e}"
                    print(f"  ✗ {msg}")
                    errors.append({"step": f"Slack 전송 ({card_id})", "detail": str(e)})
            else:
                print("      ⚠ Slack 채널 미설정 — 전송 건너뜀")

            # ── QA매니저 DM: 권고사항 + 시트 접근 오류 알림 ──
            manager_cfg = user_map.get(manager_name, {})
            slack_id = manager_cfg.get("slack_id") if isinstance(manager_cfg, dict) else manager_cfg
            if slack_id:
                if not notifier:
                    notifier = SlackNotifier()

                # 권고사항 DM
                recommendations = data.get("recommendations")
                if recommendations:
                    try:
                        notifier.send_recommendation_dm(
                            slack_id=slack_id,
                            qa_card_title=f"{card_id}: {qa_card['title']}",
                            recommendations=recommendations,
                        )
                    except Exception as e:
                        print(f"  ⚠ 권고사항 DM 실패: {e}")

                # 시트 접근 오류 DM
                progress_error = data.get("progress", {}).get("error")
                if progress_error:
                    try:
                        notifier.send_error_dm(slack_id, [{
                            "step": f"테스트 진행률 ({card_id})",
                            "detail": progress_error,
                        }])
                    except Exception as e:
                        print(f"  ⚠ 시트 오류 DM 실패: {e}")

    # ── 에러 DM 발송 ──
    _notify_errors(config, errors)

    print("\n" + "─" * 50)
    if errors:
        print(f"완료 (오류 {len(errors)}건 발생)")
    else:
        print("완료!")
    return last_dashboard_path


def run_for_assignee(config: dict, assignee_name: str) -> None:
    """특정 QAM의 활성 QA카드에 대해서만 개인 채널 메시지 전송."""
    from src.qa_discoverer import (
        discover_qa_cards, get_active_cards,
        prepare_qa_card_data, resolve_user_map,
    )
    from src.slack_notifier import SlackNotifier

    errors: list[dict] = []

    print("─" * 50)
    print(f"[QAM 개인] {assignee_name} 메시지 준비")

    try:
        cards_by_manager = discover_qa_cards(config)
    except Exception as e:
        print(f"  ✗ QA카드 조회 실패: {e}")
        errors.append({"step": "QA카드 조회", "detail": str(e)})
        _notify_errors(config, errors)
        return

    cards = cards_by_manager.get(assignee_name, [])
    active = get_active_cards(cards)

    if not active:
        print(f"  {assignee_name}: 진행중인 QA카드 없음 — 발송 건너뜀")
        return

    slack_cfg = config.get("slack", {})
    channel = slack_cfg.get("summary_channel") or os.environ.get("SLACK_CHANNEL_ID", "")
    user_map = resolve_user_map(slack_cfg.get("user_map") or {})

    if not channel:
        print("      ⚠ Slack 채널 미설정")
        return

    notifier = SlackNotifier()
    for qa_card in active:
        card_id = qa_card['identifier']
        try:
            print(f"  → {card_id}: {qa_card['title']}")
            data = prepare_qa_card_data(qa_card, config)
            notifier.send_assignee_message(
                data=data,
                channel=channel,
                assignee_name=assignee_name,
                user_map=user_map,
            )
        except Exception as e:
            msg = f"{card_id} 처리 실패: {e}"
            print(f"  ✗ {msg}")
            errors.append({"step": f"{assignee_name} — {card_id}", "detail": str(e)})

    _notify_errors(config, errors)

    print("─" * 50)
    if errors:
        print(f"{assignee_name} 메시지 전송 완료 (오류 {len(errors)}건)")
    else:
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

    try:
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
            from src.qa_discoverer import (
                discover_qa_cards, get_active_cards, prepare_qa_card_data,
            )
            print("[1/2] QA카드 조회")
            cards_by_manager = discover_qa_cards(config)
            print("[2/2] 대시보드 생성")
            for manager_name, cards in cards_by_manager.items():
                for qa_card in get_active_cards(cards):
                    data = prepare_qa_card_data(qa_card, config)
                    print(f"  {qa_card['identifier']}: {data['dashboard_path']}")

        elif args.schedule:
            from src.qa_discoverer import resolve_user_map, get_assignee_schedules
            from src.scheduler import start_scheduler

            scheduler_cfg = config.get("scheduler", {})
            base_time = scheduler_cfg.get("send_time", "09:00")
            timezone = scheduler_cfg.get("timezone", "Asia/Seoul")
            slack_cfg = config.get("slack", {})
            user_map = resolve_user_map(slack_cfg.get("user_map") or {})
            assignee_schedules = get_assignee_schedules(user_map, base_time)

            print("스케줄 등록:")
            start_scheduler(
                run_summary_fn=lambda: run(config),
                run_assignee_fn=lambda name: run_for_assignee(config, name),
                base_time=base_time,
                assignee_schedules=assignee_schedules,
                timezone=timezone,
            )

    except Exception as e:
        print(f"\n✗ 예상치 못한 오류: {e}")
        traceback.print_exc()
        _notify_errors(config, [{"step": "QA Monitor 실행", "detail": str(e)}])
        sys.exit(1)


if __name__ == "__main__":
    main()
