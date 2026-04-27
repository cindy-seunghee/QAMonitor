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


def _is_holiday() -> bool:
    """오늘(KST)이 주말 또는 한국 공휴일이면 True"""
    from datetime import datetime, timezone, timedelta
    import holidays
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).date()
    if today.weekday() >= 5:
        return True
    return today in holidays.KR(years=today.year)


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

        # ── 운영모니터링 카드 수집 ──
        from src.qa_discoverer import parse_test_phases
        monitoring_cards = []
        normal_cards = []
        for qa_card in active:
            test_phases = parse_test_phases(qa_card)
            if test_phases["current_phase"] == "운영모니터링":
                card_id = qa_card['identifier']
                print(f"\n  {card_id}: {qa_card['title']} (운영모니터링)")
                monitoring_cards.append({
                    "identifier": card_id,
                    "title": qa_card['title'],
                    "url": f"https://linear.app/buzzvil/issue/{card_id}",
                })
            else:
                normal_cards.append(qa_card)

        # ── 운영모니터링 DM 일괄 발송 ──
        if monitoring_cards:
            manager_cfg = user_map.get(manager_name, {})
            manager_slack_id = manager_cfg.get("slack_id") if isinstance(manager_cfg, dict) else manager_cfg
            if manager_slack_id:
                try:
                    if not notifier:
                        notifier = SlackNotifier()
                    notifier.send_monitoring_dm(
                        slack_id=manager_slack_id,
                        monitoring_cards=monitoring_cards,
                    )
                except Exception as e:
                    print(f"  ✗ 운영모니터링 DM 실패: {e}")
                    errors.append({"step": "운영모니터링 DM", "detail": str(e)})

        for qa_card in normal_cards:
            card_id = qa_card['identifier']
            try:
                print(f"\n[3/4] {card_id}: {qa_card['title']}")
                data = prepare_qa_card_data(qa_card, config)

                last_dashboard_path = data["dashboard_path"]
                # buzz-html 업로드
                from src.html_uploader import upload_dashboard
                dashboard_url = upload_dashboard(
                    data["dashboard_path"],
                    filename=f"qa_dashboard_{card_id}.html",
                )
                data["dashboard_url"] = dashboard_url
                # 뷰 URL 연결
                card_view_urls = view_result.get("view_urls", {}).get(card_id, {})
                data["view_urls"] = card_view_urls
                print(f"      하위이슈 {len(data['all_issues'])}건 | 대시보드: {data['dashboard_path']}")
            except Exception as e:
                msg = f"{card_id} 데이터 준비 실패: {e}"
                print(f"  ✗ {msg}")
                errors.append({"step": f"데이터 준비 ({card_id})", "detail": str(e)})
                continue

            # 매니저 slack_id로 DM 발송
            manager_cfg = user_map.get(manager_name, {})
            manager_slack_id = manager_cfg.get("slack_id") if isinstance(manager_cfg, dict) else manager_cfg
            if manager_slack_id:
                try:
                    print(f"[4/4] Slack DM 전송: {card_id} → {manager_name}")
                    notifier = SlackNotifier()
                    notifier.send_daily_report(
                        data=data,
                        channel=manager_slack_id,
                        user_map=user_map,
                        template_path=slack_cfg.get("template_path", "slack_template.md"),
                        dashboard_path=data.get("dashboard_path"),
                    )
                except Exception as e:
                    msg = f"{card_id} Slack DM 전송 실패: {e}"
                    print(f"  ✗ {msg}")
                    errors.append({"step": f"Slack DM ({card_id})", "detail": str(e)})
            else:
                print(f"      ⚠ {manager_name} slack_id 미설정 — 전송 건너뜀")

            # ── QA매니저 DM: 권고사항 ──
            if manager_slack_id:
                if not notifier:
                    notifier = SlackNotifier()

                recommendations = data.get("recommendations")
                if recommendations:
                    try:
                        notifier.send_recommendation_dm(
                            slack_id=manager_slack_id,
                            qa_card_title=f"{card_id}: {qa_card['title']}",
                            recommendations=recommendations,
                        )
                    except Exception as e:
                        print(f"  ⚠ 권고사항 DM 실패: {e}")

            # TC시트 안내 DM → QA카드 assignee에게 발송
            progress_error = data.get("progress", {}).get("error", "")
            sheet_url = data.get("testcase_sheet_url")
            need_access_dm = "권한" in progress_error or "접근" in progress_error
            need_missing_dm = not sheet_url and data.get("progress", {}).get("source") != "google_sheet"

            if need_access_dm or need_missing_dm:
                qa_assignee = (qa_card.get("assignee") or qa_card.get("creator") or {})
                assignee_name = (qa_assignee.get("displayName") or qa_assignee.get("name") or "").lower()
                assignee_slack_id = None
                for uname, ucfg in user_map.items():
                    if isinstance(ucfg, dict):
                        ln = (ucfg.get("linear_name") or "").lower()
                        if ln and ln == assignee_name:
                            assignee_slack_id = ucfg.get("slack_id")
                            break
                if assignee_slack_id:
                    try:
                        if not notifier:
                            notifier = SlackNotifier()
                        card_url = f"https://linear.app/buzzvil/issue/{card_id}"
                        if need_access_dm:
                            notifier.send_sheet_access_dm(
                                slack_id=assignee_slack_id,
                                qa_card_title=qa_card["title"],
                                card_url=card_url,
                            )
                        else:
                            notifier.send_sheet_missing_dm(
                                slack_id=assignee_slack_id,
                                qa_card_title=qa_card["title"],
                                card_url=card_url,
                            )
                    except Exception as e:
                        print(f"  ⚠ TC시트 안내 DM 실패: {e}")

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

    # 뷰 동기화
    from src.qa_discoverer import sync_views
    try:
        sync_views(cards_by_manager)
    except Exception as e:
        print(f"  ⚠ 뷰 동기화 실패: {e}")

    slack_cfg = config.get("slack", {})
    user_map = resolve_user_map(slack_cfg.get("user_map") or {})

    # 매니저 slack_id로 DM 발송
    manager_cfg = user_map.get(assignee_name, {})
    manager_slack_id = manager_cfg.get("slack_id") if isinstance(manager_cfg, dict) else manager_cfg
    if not manager_slack_id:
        print(f"      ⚠ {assignee_name} slack_id 미설정 — 전송 건너뜀")
        return

    notifier = SlackNotifier()

    # ── 운영모니터링 카드는 별도 발송이므로 스킵 ──
    from src.qa_discoverer import parse_test_phases
    normal_cards = []
    for qa_card in active:
        test_phases = parse_test_phases(qa_card)
        if test_phases["current_phase"] == "운영모니터링":
            print(f"  → {qa_card['identifier']}: {qa_card['title']} (운영모니터링 — 스킵)")
            continue
        normal_cards.append(qa_card)

    for qa_card in normal_cards:
        card_id = qa_card['identifier']
        try:
            print(f"  → {card_id}: {qa_card['title']}")
            data = prepare_qa_card_data(qa_card, config)

            # 뷰 URL 가져오기
            from tools.manage_linear_views import get_existing_views
            existing = get_existing_views(card_id)
            card_view_urls = {}
            for name, v in existing.items():
                slug = v.get("slugId") or v["id"]
                url = f"https://linear.app/buzzvil/view/{slug}"
                if "전체" in name:
                    card_view_urls["total"] = url
                elif "내" in name:
                    card_view_urls["my"] = url
                elif "수정 확인 대기" in name:
                    card_view_urls["dev_done"] = url
                elif "협의 종료" in name:
                    card_view_urls["negotiated"] = url
            data["view_urls"] = card_view_urls

            # buzz-html 업로드
            from src.html_uploader import upload_dashboard
            dashboard_url = upload_dashboard(
                data["dashboard_path"],
                filename=f"qa_dashboard_{card_id}.html",
            )
            data["dashboard_url"] = dashboard_url

            slack_cfg = config.get("slack", {})
            notifier.send_daily_report(
                data=data,
                channel=manager_slack_id,
                user_map=user_map,
                template_path=slack_cfg.get("template_path", "slack_template.md"),
                dashboard_path=data.get("dashboard_path"),
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


def run_monitoring_for_assignee(config: dict, assignee_name: str) -> None:
    """특정 QAM의 운영모니터링 카드만 일괄 DM 발송."""
    from src.qa_discoverer import (
        discover_qa_cards, get_active_cards,
        parse_test_phases, resolve_user_map,
    )
    from src.slack_notifier import SlackNotifier

    errors: list[dict] = []

    print("─" * 50)
    print(f"[운영모니터링] {assignee_name} 메시지 준비")

    try:
        cards_by_manager = discover_qa_cards(config)
    except Exception as e:
        print(f"  ✗ QA카드 조회 실패: {e}")
        errors.append({"step": "QA카드 조회", "detail": str(e)})
        _notify_errors(config, errors)
        return

    cards = cards_by_manager.get(assignee_name, [])
    active = get_active_cards(cards)

    monitoring_cards = []
    for qa_card in active:
        test_phases = parse_test_phases(qa_card)
        if test_phases["current_phase"] == "운영모니터링":
            card_id = qa_card['identifier']
            print(f"  → {card_id}: {qa_card['title']}")
            monitoring_cards.append({
                "identifier": card_id,
                "title": qa_card['title'],
                "url": f"https://linear.app/buzzvil/issue/{card_id}",
            })

    if not monitoring_cards:
        print(f"  {assignee_name}: 운영모니터링 대상 카드 없음 — 발송 건너뜀")
        return

    slack_cfg = config.get("slack", {})
    user_map = resolve_user_map(slack_cfg.get("user_map") or {})
    manager_cfg = user_map.get(assignee_name, {})
    manager_slack_id = manager_cfg.get("slack_id") if isinstance(manager_cfg, dict) else manager_cfg
    if not manager_slack_id:
        print(f"      ⚠ {assignee_name} slack_id 미설정 — 전송 건너뜀")
        return

    notifier = SlackNotifier()

    try:
        notifier.send_monitoring_dm(
            slack_id=manager_slack_id,
            monitoring_cards=monitoring_cards,
        )
    except Exception as e:
        print(f"  ✗ 운영모니터링 DM 실패: {e}")
        errors.append({"step": "운영모니터링 DM", "detail": str(e)})

    # ── QA 라벨 누락 이슈 체크 ──
    try:
        from src.linear_client import LinearClient
        linear_name = manager_cfg.get("linear_name")
        if not linear_name:
            print(f"  ⚠ {assignee_name}의 linear_name 미설정 — 라벨 체크 건너뜀")
        else:
            qa_labels = config.get("linear", {}).get("qa_labels", ["QA"])
            missing = LinearClient().get_assigned_issues_without_label(linear_name, qa_labels)
            if missing:
                print(f"  ⚠ QA 라벨 누락 이슈 {len(missing)}건 감지")
                notifier.send_missing_label_dm(
                    slack_id=manager_slack_id,
                    issues=missing,
                    label_name=", ".join(qa_labels),
                    assignee_name=assignee_name,
                )
    except Exception as e:
        print(f"  ⚠ QA 라벨 누락 체크 실패: {e}")

    _notify_errors(config, errors)
    print("─" * 50)
    print(f"{assignee_name} 운영모니터링 전송 완료" + (f" (오류 {len(errors)}건)" if errors else "!"))


def run_single_card(config: dict, card_id: str) -> None:
    """특정 QA카드 1개만 실행."""
    from src.qa_discoverer import (
        discover_qa_cards, prepare_qa_card_data,
        resolve_user_map, sync_views,
    )
    from src.slack_notifier import SlackNotifier

    print("─" * 50)
    print(f"[단일 카드] {card_id}")

    cards_by_manager = discover_qa_cards(config)

    # 해당 카드 찾기
    qa_card = None
    card_manager = None
    for manager, cards in cards_by_manager.items():
        for c in cards:
            if c["identifier"] == card_id:
                qa_card = c
                card_manager = manager
                break
        if qa_card:
            break

    if not qa_card:
        print(f"  ✗ {card_id}를 찾을 수 없습니다.")
        return

    print(f"  매니저: {card_manager} | 상태: {qa_card['qa_status']}")

    # 뷰 동기화 (해당 카드만)
    sync_views({card_manager: [qa_card]})

    slack_cfg = config.get("slack", {})
    channel = slack_cfg.get("summary_channel") or os.environ.get("SLACK_CHANNEL_ID", "")
    user_map = resolve_user_map(slack_cfg.get("user_map") or {})

    # 뷰 URL 가져오기
    from tools.manage_linear_views import get_existing_views
    existing = get_existing_views(card_id)
    view_urls = {}
    for name, v in existing.items():
        slug = v.get("slugId") or v["id"]
        url = f"https://linear.app/buzzvil/view/{slug}"
        if "전체" in name:
            view_urls["total"] = url
        elif "내" in name:
            view_urls["my"] = url
        elif "수정 확인 대기" in name:
            view_urls["dev_done"] = url
        elif "협의 종료" in name:
            view_urls["negotiated"] = url

    data = prepare_qa_card_data(qa_card, config)
    data["view_urls"] = view_urls

    # buzz-html 업로드
    from src.html_uploader import upload_dashboard
    dashboard_url = upload_dashboard(
        data["dashboard_path"],
        filename=f"qa_dashboard_{card_id}.html",
    )
    data["dashboard_url"] = dashboard_url

    # 매니저 slack_id로 DM 발송
    manager_cfg = user_map.get(card_manager, {})
    manager_slack_id = manager_cfg.get("slack_id") if isinstance(manager_cfg, dict) else manager_cfg
    if manager_slack_id:
        notifier = SlackNotifier()
        notifier.send_daily_report(
            data=data,
            channel=manager_slack_id,
            user_map=user_map,
            template_path=slack_cfg.get("template_path", "slack_template.md"),
            dashboard_path=data.get("dashboard_path"),
        )
    else:
        print(f"      ⚠ {card_manager} slack_id 미설정 — 전송 건너뜀")

    print("─" * 50)
    print(f"{card_id} 완료!")


def watch_changes(config: dict, assignee_name: str = "") -> None:
    """TC 작성 기간 중 PRD/Figma 변경 감지 + Slack 알림 발송."""
    from src.qa_discoverer import (
        discover_qa_cards, get_active_cards,
        parse_test_phases, resolve_user_map,
    )
    from src.change_watcher import should_watch, watch_card_changes, check_missing_links
    from src.slack_notifier import SlackNotifier
    from datetime import datetime, timezone, timedelta

    errors: list[dict] = []
    kst = timezone(timedelta(hours=9))
    is_morning = datetime.now(kst).hour < 12

    print("─" * 50)
    target = assignee_name or "전체"
    print(f"[변경 감시] {target} — PRD/Figma 변경 체크 ({'오전' if is_morning else '오후'})")

    try:
        cards_by_manager = discover_qa_cards(config)
    except Exception as e:
        print(f"  ✗ QA카드 조회 실패: {e}")
        errors.append({"step": "QA카드 조회", "detail": str(e)})
        _notify_errors(config, errors)
        return

    slack_cfg = config.get("slack", {})
    user_map = resolve_user_map(slack_cfg.get("user_map") or {})
    notifier = None

    for manager_name, cards in cards_by_manager.items():
        # 특정 담당자만 실행
        if assignee_name and manager_name != assignee_name:
            continue

        active = get_active_cards(cards)
        if not active:
            continue

        # TC 작성 기간인 카드만 필터
        watch_cards = []
        for qa_card in active:
            test_phases = parse_test_phases(qa_card)
            if should_watch(test_phases):
                watch_cards.append(qa_card)
                print(f"  → {qa_card['identifier']}: {qa_card['title']} ({test_phases['current_phase']})")
            else:
                print(f"  → {qa_card['identifier']}: {qa_card['title']} ({test_phases['current_phase']}) — 감시 대상 아님")

        if not watch_cards:
            print(f"  {manager_name}: TC 작성 기간인 카드 없음 — 건너뜀")
            continue

        # 매니저 slack_id
        manager_cfg = user_map.get(manager_name, {})
        manager_slack_id = manager_cfg.get("slack_id") if isinstance(manager_cfg, dict) else manager_cfg

        # 오전: PRD/Figma 링크 누락 안내 (매니저별 1건으로 통합)
        if is_morning and manager_slack_id:
            missing_cards = []
            for qa_card in watch_cards:
                missing = check_missing_links(qa_card)
                if missing["missing_prd"] or missing["missing_figma"]:
                    missing_cards.append({
                        "card_id": qa_card["identifier"],
                        "title": qa_card.get("title", ""),
                        "missing_prd": missing["missing_prd"],
                        "missing_figma": missing["missing_figma"],
                    })
                    print(f"      링크 안내 대상: {qa_card['identifier']} — PRD={'누락' if missing['missing_prd'] else 'OK'}, Figma={'누락' if missing['missing_figma'] else 'OK'}")
            if missing_cards:
                if not notifier:
                    notifier = SlackNotifier()
                notifier.send_missing_links_dm(
                    slack_id=manager_slack_id,
                    missing_cards=missing_cards,
                )

        # 변경 감지 — 매니저별 결과 수집 후 통합 발송
        changed_results = []
        for qa_card in watch_cards:
            card_id = qa_card["identifier"]
            try:
                result = watch_card_changes(qa_card, config)
                has_change = result["prd_change"] or result["figma_changes"]
                if has_change:
                    print(f"      변경 감지: PRD={'O' if result['prd_change'] else 'X'}, Figma={len(result['figma_changes'])}건")
                    changed_results.append(result)
                else:
                    print(f"      변경 없음")
            except Exception as e:
                msg = f"{card_id} 변경 감지 실패: {e}"
                print(f"  ✗ {msg}")
                errors.append({"step": f"변경 감시 ({card_id})", "detail": str(e)})

        if changed_results and manager_slack_id:
            if not notifier:
                notifier = SlackNotifier()
            notifier.send_change_alert_dm(
                slack_id=manager_slack_id,
                card_results=changed_results,
            )
        elif changed_results:
            print(f"      ⚠ {manager_name} slack_id 미설정 — 알림 건너뜀")

    _notify_errors(config, errors)
    print("─" * 50)
    if errors:
        print(f"변경 감시 완료 (오류 {len(errors)}건)")
    else:
        print("변경 감시 완료!")


def delete_slack_message(msg_url: str) -> None:
    """Slack 메시지 링크에서 채널ID와 timestamp를 추출하여 메시지를 삭제한다."""
    import re
    from src.slack_notifier import SlackNotifier

    # URL 파싱: https://buzzvil.slack.com/archives/CV1STCM52/p1774113546901219
    m = re.search(r"/archives/([A-Z0-9]+)/p(\d+)", msg_url)
    if not m:
        print(f"  ✗ 잘못된 Slack 메시지 링크: {msg_url}")
        return

    channel_id = m.group(1)
    # Slack timestamp: p1774113546901219 → 1774113546.901219
    raw_ts = m.group(2)
    ts = raw_ts[:-6] + "." + raw_ts[-6:]

    notifier = SlackNotifier()
    try:
        # 스레드 답글 먼저 삭제
        replies = notifier.client.conversations_replies(channel=channel_id, ts=ts)
        messages = replies.get("messages", [])
        for msg in reversed(messages):
            if msg["ts"] != ts:  # 부모 메시지 제외
                try:
                    notifier.client.chat_delete(channel=channel_id, ts=msg["ts"])
                    print(f"  ✓ 스레드 답글 삭제: {msg['ts']}")
                except Exception as e:
                    print(f"  ⚠ 스레드 답글 삭제 실패: {e}")

        # 부모 메시지 삭제
        notifier.client.chat_delete(channel=channel_id, ts=ts)
        print(f"  ✓ 메시지 삭제 완료: {channel_id}/{ts}")
    except Exception as e:
        print(f"  ✗ 메시지 삭제 실패: {e}")


def main():
    parser = argparse.ArgumentParser(description="QA Monitor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-now", action="store_true", help="지금 바로 실행 (전체 요약)")
    group.add_argument("--run-for", metavar="NAME", help="특정 QAM 개인 메시지만 즉시 전송")
    group.add_argument("--run-monitoring", metavar="NAME", help="특정 QAM 운영모니터링 DM만 즉시 전송")
    group.add_argument("--run-card", metavar="CARD_ID", help="특정 QA카드만 실행 (예: SUP-1557)")
    group.add_argument("--watch-changes", nargs="?", const="", metavar="NAME",
                       help="PRD/Figma 변경 감시 (TC 작성 기간 카드 대상, NAME 생략 시 전체)")
    group.add_argument("--delete-msg", metavar="URL", help="봇이 보낸 Slack 메시지 삭제 (메시지 링크)")
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
            if _is_holiday():
                print("오늘은 주말 또는 공휴일입니다. 실행을 건너뜁니다.")
                return
            run(config)

        elif args.run_for:
            if _is_holiday():
                print("오늘은 주말 또는 공휴일입니다. 실행을 건너뜁니다.")
                return
            run_for_assignee(config, args.run_for)

        elif args.run_monitoring:
            if _is_holiday():
                print("오늘은 주말 또는 공휴일입니다. 실행을 건너뜁니다.")
                return
            run_monitoring_for_assignee(config, args.run_monitoring)

        elif args.watch_changes is not None:
            if _is_holiday():
                print("오늘은 주말 또는 공휴일입니다. 실행을 건너뜁니다.")
                return
            watch_changes(config, args.watch_changes)

        elif args.run_card:
            run_single_card(config, args.run_card)  # 수동 테스트용이므로 공휴일 체크 안 함

        elif args.delete_msg:
            delete_slack_message(args.delete_msg)

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
