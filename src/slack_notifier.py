"""Slack 봇 노티파이어 — Block Kit 기반 메시지 전송"""

from __future__ import annotations

import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .analyzer import PRIORITY_EMOJI, PRIORITY_ORDER


# ── 템플릿 로더 ─────────────────────────────────────────────────────────────

def _load_template(path: str) -> dict:
    """
    slack_template.md 파싱.
    # 메인 메시지 → main
    # 스레드: 제목  → threads 리스트
    각 섹션은 key: value 형식.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return _default_template()

    result = {"main": {}, "threads": []}
    current = None

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # 헤더 파싱
            if stripped.startswith("# 메인") or stripped.startswith("# main"):
                current = result["main"]
            elif stripped.startswith("# 스레드") or stripped.startswith("# thread"):
                title = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                new_thread = {"title": title, "sections": []}
                result["threads"].append(new_thread)
                current = new_thread
            continue

        if current is None:
            continue

        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "sections":
                current["sections"] = [s.strip() for s in value.split(",") if s.strip()]
            else:
                current[key] = value

    return result


def _default_template() -> dict:
    return {
        "main": {
            "intro": "오늘의 QA 현황 보고입니다.",
            "sections": ["progress", "bug_summary", "priority_breakdown", "exit_summary"],
            "footer": "담당자별 상세 현황과 배포 기준은 스레드를 확인해주세요. 👇",
        },
        "threads": [
            {"title": "담당자별 현황", "sections": ["assignee_detail"]},
            {"title": "배포 기준 체크리스트", "sections": ["exit_checklist"]},
        ],
    }


# ── 노티파이어 클래스 ─────────────────────────────────────────────────────────

class SlackNotifier:
    def __init__(self, token: str = None):
        self.client = WebClient(token=token or os.environ["SLACK_BOT_TOKEN"])

    # ── 공개 API ────────────────────────────────────────────────────────────

    def send_daily_report(
        self,
        data: dict,
        channel: str,
        user_map: dict = None,
        template_path: str = "slack_template.md",
        dashboard_path: str = None,
    ) -> None:
        user_map = user_map or {}
        template = _load_template(template_path)
        threads = template.get("threads", [])
        test_phase = data.get("test_phase", "")

        # 1) 메인 메시지 — 새 양식
        main_blocks = self._build_main_message(data, user_map, dashboard_path)
        fallback = self._summary_fallback(data)
        thread_ts = self._post(channel, main_blocks, text=fallback)

        if not thread_ts:
            return

        # 2) 대시보드 업로드
        dashboard_url = data.get("dashboard_url")
        if dashboard_url:
            link_text = "\u2022 <{url}|QA 대시보드 보기>".format(url=dashboard_url)
            link_blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": link_text}}]
            self._post_thread(channel, thread_ts, link_blocks, text=link_text)

    # ── 메인 메시지 빌더 (새 양식) ──────────────────────────────────────────

    def _build_main_message(
        self, data: dict, user_map: dict, dashboard_path: str = None,
    ) -> list[dict]:
        from datetime import datetime, timezone, timedelta

        kst = timezone(timedelta(hours=9))
        today = datetime.now(kst).strftime("%m/%d")
        project_name = data.get("project_name", "QA")
        test_phase = data.get("test_phase", "")
        progress_status = data.get("progress_status", {})
        pct = data.get("progress", {}).get("pct", 0)
        open_bug_count = data.get("open_bug_count", 0)
        open_bugs = data.get("open_bugs", [])
        by_assignee = data.get("by_assignee", {})
        view_urls = data.get("view_urls", {})

        # 아이콘
        icon = progress_status.get("icon", ":mulgae_love:")

        # 진행률 표시
        if pct == "?":
            pct_text = "?"
        else:
            pct_text = str(pct)

        # 잔여 이슈 링크 (전체 잔여이슈 뷰)
        qa_card = data.get("qa_card", {})
        card_id = qa_card.get("identifier", "")
        remaining_link = f"<https://linear.app/buzzvil/view|{open_bug_count}건>"

        # 우선순위별 건수
        urgent_count = sum(1 for b in open_bugs if b.get("priorityLabel") == "Urgent")
        high_count = sum(1 for b in open_bugs if b.get("priorityLabel") == "High")
        medium_count = sum(1 for b in open_bugs if b.get("priorityLabel") == "Medium")
        low_count = sum(1 for b in open_bugs if b.get("priorityLabel") in ("Low", "No priority"))

        # 우선순위 라인 (없는 경우 생략)
        priority_lines = ""
        if urgent_count > 0:
            priority_lines += f"\n    \u25E6 Urgent *:* {urgent_count}건"
        if high_count > 0:
            priority_lines += f"\n    \u25E6 High *:* {high_count}건"
        if medium_count > 0:
            priority_lines += f"\n    \u25E6 Medium : {medium_count}건"
        if low_count > 0:
            priority_lines += f"\n    \u25E6 Low : {low_count}건"

        # 대시보드 링크
        dash_text = f"`{dashboard_path}`" if dashboard_path else "생성 안 됨"

        # user_map 키(Liana) → linear_name(liana.kim) 역매핑 생성
        linear_name_to_slack: dict[str, str] = {}
        for uname, ucfg in user_map.items():
            if isinstance(ucfg, dict):
                ln = ucfg.get("linear_name", "").lower()
                sid = ucfg.get("slack_id", "")
                if ln and sid:
                    linear_name_to_slack[ln] = sid
                if sid:
                    linear_name_to_slack[uname.lower()] = sid

        # 메시지 조립
        phase_text = f"*`{test_phase}`* " if test_phase else ""
        qa_card = data.get("qa_card", {})
        card_url = qa_card.get("url", "")
        if not card_url:
            card_id = qa_card.get("identifier", "")
            if card_id:
                card_url = f"https://linear.app/buzzvil/issue/{card_id}"
        if card_url:
            project_link = f"<{card_url}|{project_name}>"
        else:
            project_link = project_name
        lines = f"*{project_link}* {phase_text}*진행 상황 ({today})*"
        lines += f"\n"

        # 담당QA 태그 (QA카드 assignee)
        qa_assignee = (qa_card.get("assignee") or qa_card.get("creator") or {})
        qa_assignee_name = qa_assignee.get("displayName") or qa_assignee.get("name") or ""
        if qa_assignee_name:
            qa_slack_id = linear_name_to_slack.get(qa_assignee_name.lower(), "")
            qa_mention = f"<@{qa_slack_id}>" if qa_slack_id else f"*{qa_assignee_name}*"
            lines += f"\n*담당QA* : {qa_mention}"
            lines += f"\n"
        tc_url = data.get("testcase_sheet_url", "")
        if tc_url and pct_text != "?":
            lines += f"\n{icon} *테스트 진행률* : <{tc_url}|*`{pct_text}`*>*%*"
        else:
            lines += f"\n{icon} *테스트 진행률* : *`{pct_text}`%*"
        total_url = view_urls.get("total", "")

        if total_url:
            lines += f"\n> \u2022 *잔여 이슈* : <{total_url}|*{open_bug_count}건*>"
        else:
            lines += f"\n> \u2022 *잔여 이슈* : *{open_bug_count}건*"
        if priority_lines:
            lines += priority_lines.replace("\n    ", "\n>     ")
        dev_done_count = data.get("dev_done_bug_count", 0)
        if dev_done_count > 0:
            dev_done_url = view_urls.get("dev_done", "")
            if dev_done_url:
                lines += f"\n> \u2022 *수정 확인 대기* : <{dev_done_url}|*{dev_done_count}건*>"
            else:
                lines += f"\n> \u2022 *수정 확인 대기* : *{dev_done_count}건*"
        if open_bug_count == 0 and dev_done_count == 0:
            lines += "\n\n미해결 잔여 이슈가 없어요 :among_thumbs_up:"

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": lines},
            },
        ]
        return blocks

    # ── 섹션 라우터 ─────────────────────────────────────────────────────────

    def _build_section_blocks(
        self,
        sections: list[str],
        data: dict,
        user_map: dict,
        intro: str = "",
        footer: str = "",
        title: str = "",
        dashboard_path: str = None,
    ) -> list[dict]:
        blocks: list[dict] = []

        if title:
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": title, "emoji": True},
            })

        if intro:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": intro},
            })

        if title or intro:
            blocks.append({"type": "divider"})

        for section in sections:
            new_blocks = self._build_one_section(section, data, user_map)
            if new_blocks:
                blocks.extend(new_blocks)
                blocks.append({"type": "divider"})

        # 마지막 divider 제거
        if blocks and blocks[-1].get("type") == "divider":
            blocks.pop()

        if footer or dashboard_path:
            footer_text = footer
            if dashboard_path:
                footer_text += f"  (`{dashboard_path}`)"
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": footer_text}],
            })

        return blocks

    def _build_one_section(
        self, section: str, data: dict, user_map: dict
    ) -> list[dict]:
        builders = {
            "progress":           self._section_progress,
            "bug_summary":        self._section_bug_summary,
            "priority_breakdown": self._section_priority,
            "assignee_summary":   self._section_assignee_summary,
            "assignee_detail":    self._section_assignee_detail,
            "exit_summary":       self._section_exit_summary,
            "exit_checklist":     self._section_exit_checklist,
        }
        fn = builders.get(section)
        if fn is None:
            return [{"type": "section", "text": {"type": "mrkdwn", "text": f"⚠ 알 수 없는 섹션: `{section}`"}}]
        return fn(data, user_map)

    # ── 섹션 빌더 ───────────────────────────────────────────────────────────

    def _section_progress(self, data: dict, _: dict) -> list[dict]:
        p = data.get("progress", {})
        pct = p.get("pct", 0.0)
        done = p.get("done", 0)
        total = p.get("total", 0)
        in_progress = p.get("in_progress", 0)
        not_started = p.get("not_started", 0)

        if pct == "?":
            bar = "░" * 10
            pct_text = "`?`  _(시트 접근 불가)_"
        else:
            bar = _text_bar(pct)
            pct_text = f"`{pct}%`"

        return [{
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*테스트 진행률*\n"
                        f"{bar}  {pct_text}\n"
                        f"완료 {done}  /  전체 {total}\n"
                        f"진행중 {in_progress}  ·  미시작 {not_started}"
                    ),
                },
            ],
        }]

    def _section_bug_summary(self, data: dict, _: dict) -> list[dict]:
        count = data.get("open_bug_count", 0)
        today = data.get("today_new_count", 0)
        icon = "🔴" if count > 0 else "✅"
        return [{
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*미해결 버그*\n{icon}  *{count}건*\n오늘 신규: {today}건",
                },
            ],
        }]

    def _section_priority(self, data: dict, _: dict) -> list[dict]:
        breakdown = data.get("priority_breakdown", [])
        items = "  ".join(
            f"{p['emoji']} {p['priority']}: *{p['count']}*"
            for p in breakdown
            if p["count"] > 0
        ) or "미해결 버그 없음 🎉"
        return [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*우선순위별 미해결 버그*\n{items}"},
        }]

    def _section_assignee_summary(self, data: dict, user_map: dict) -> list[dict]:
        by_assignee = data.get("by_assignee", {})
        lines = []
        for name, info in by_assignee.items():
            slack_uid = user_map.get(name)
            mention = f"<@{slack_uid}>" if slack_uid else f"*{name}*"
            bug_cnt = len(info.get("open_bugs", []))
            pct = info.get("qa_pct", 0.0)
            lines.append(f"{mention}  진행률 {pct}%  |  미해결 {bug_cnt}건")
        text = "\n".join(lines) if lines else "담당자 정보 없음"
        return [{"type": "section", "text": {"type": "mrkdwn", "text": f"*담당자별 요약*\n{text}"}}]

    def _section_assignee_detail(self, data: dict, user_map: dict) -> list[dict]:
        by_assignee = data.get("by_assignee", {})
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*담당자별 현황 및 확인 필요 이슈*"}},
        ]
        for name, info in by_assignee.items():
            open_bugs = info.get("open_bugs", [])
            qa_done = info.get("qa_done", 0)
            qa_total = info.get("qa_total", 0)
            qa_pct = info.get("qa_pct", 0.0)
            slack_uid = user_map.get(name)
            mention = f"<@{slack_uid}>" if slack_uid else f"*{name}*"
            bar = _text_bar(qa_pct, width=8)

            header = f"{mention}"
            if qa_total > 0:
                header += f"  |  테스트 {bar} `{qa_pct}%`  ({qa_done}/{qa_total})"
            header += f"  |  미해결 버그 *{len(open_bugs)}건*"

            bug_lines = ""
            for bug in open_bugs[:5]:
                emoji = PRIORITY_EMOJI.get(bug.get("priorityLabel", "No priority"), "")
                identifier = bug.get("identifier", "")
                title = bug.get("title", "")[:40]
                url = bug.get("url", "#")
                state = bug.get("state", {}).get("name", "")
                bug_lines += f"\n{emoji} <{url}|{identifier}>  {title}  _{state}_"
            if len(open_bugs) > 5:
                bug_lines += f"\n_... 외 {len(open_bugs) - 5}건_"

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{header}{bug_lines}"},
            })

        return blocks

    def _section_exit_summary(self, data: dict, _: dict) -> list[dict]:
        exit_status = data.get("exit_status", [])
        all_pass = all(c["pass"] for c in exit_status)
        icon = "✅" if all_pass else "❌"
        status_text = "배포 가능" if all_pass else "배포 불가 — 미충족 항목 확인 필요"
        fail_items = [c["label"] for c in exit_status if not c["pass"]]
        detail = ""
        if fail_items:
            detail = "\n미충족: " + ", ".join(fail_items)
        return [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*배포 기준*\n{icon}  *{status_text}*{detail}"},
        }]

    def _section_exit_checklist(self, data: dict, _: dict) -> list[dict]:
        exit_status = data.get("exit_status", [])
        deployment_checklist = data.get("deployment_checklist", [])

        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*QA 종료 기준 (자동 체크)*"}},
        ]

        criteria_lines = "\n".join(
            f"{'✅' if c['pass'] else '❌'}  {c['label']}  `{c['current']}`"
            for c in exit_status
        )
        if criteria_lines:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": criteria_lines},
            })

        if deployment_checklist:
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*배포 체크리스트 (수동 확인)*"},
            })
            checklist_lines = "\n".join(f"☑  {item}" for item in deployment_checklist)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": checklist_lines},
            })

        return blocks

    # ── 전송 헬퍼 ─────────────────────────────────────────────────────────

    def _post(self, channel: str, blocks: list[dict], text: str = "") -> str | None:
        """메시지 전송 후 thread_ts 반환"""
        try:
            resp = self.client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text=text,
                unfurl_links=False,
                unfurl_media=False,
            )
            ts = resp["ts"]
            print(f"  ✓ 채널 메시지 전송 완료: {channel} (ts={ts})")
            return ts
        except SlackApiError as e:
            print(f"  ✗ 채널 메시지 전송 실패: {e.response['error']}")
            raise

    def _post_thread(
        self, channel: str, thread_ts: str, blocks: list[dict], text: str = ""
    ) -> None:
        try:
            self.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text=text,
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  ✓ 스레드 댓글 전송 완료: {text or '(제목 없음)'}")
        except SlackApiError as e:
            print(f"  ✗ 스레드 댓글 전송 실패: {e.response['error']}")

    def _upload_file(
        self, channel: str, thread_ts: str, file_path: str
    ) -> None:
        """대시보드 HTML 파일을 스레드에 업로드"""
        try:
            self.client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=file_path,
                title="QA 모니터링 대시보드",
                initial_comment="대시보드 파일",
            )
            print(f"  ✓ 대시보드 파일 업로드 완료: {file_path}")
        except SlackApiError as e:
            print(f"  ✗ 대시보드 파일 업로드 실패: {e.response['error']}")

    def _summary_fallback(self, data: dict) -> str:
        pct = data.get("progress", {}).get("pct", 0)
        pct_str = "?" if pct == "?" else f"{pct}%"
        bugs = data.get("open_bug_count", 0)
        return f"QA 일일 리포트 | 진행률 {pct_str} | 미해결 버그 {bugs}건"

    # ── 권고사항 DM ──────────────────────────────────────────────────────

    def send_recommendation_dm(
        self, slack_id: str, qa_card_title: str, recommendations: dict
    ) -> None:
        """QA매니저에게 비크리티컬 처리 방안 + 권고사항을 DM으로 발송."""
        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"QA 분석 권고사항 — {qa_card_title[:60]}", "emoji": True},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "_이 메시지는 Claude Code의 데이터 기반 분석 의견입니다. QA매니저의 판단이 필요합니다._"}],
            },
            {"type": "divider"},
        ]

        # 비크리티컬 처리 방안
        non_critical = recommendations.get("non_critical_plan", [])
        if non_critical:
            lines = "\n".join(
                f"• *{item['category']}* ({item['count']}건)\n  {item['suggestion']}"
                for item in non_critical
            )
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*비크리티컬 이슈 처리 방안*\n{lines}"},
            })
            blocks.append({"type": "divider"})

        # 배포 권고사항
        advice = recommendations.get("advice", [])
        if advice:
            lines = "\n".join(f"• {a}" for a in advice)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*배포 권고사항*\n{lines}"},
            })

        blocks.append({"type": "divider"})
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "_데이터 기반 자동 분석 결과이며, 최종 판단은 QA매니저가 내려주세요._"}],
        })

        try:
            self.client.chat_postMessage(
                channel=slack_id,
                blocks=blocks,
                text=f"QA 권고사항: {qa_card_title}",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  ✓ 권고사항 DM 전송 완료: {slack_id}")
        except SlackApiError as e:
            print(f"  ✗ 권고사항 DM 전송 실패: {e.response['error']}")

    # ── 에러 DM 알림 ─────────────────────────────────────────────────────

    def send_error_dm(self, slack_id: str, errors: list[dict]) -> None:
        """
        에러 목록을 DM으로 전송한다.
        errors: [{"step": "단계명", "detail": "에러 상세"}, ...]
        """
        error_lines = "\n".join(
            f"• *{e['step']}*\n  `{e['detail']}`"
            for e in errors
        )
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "QA Monitor 실행 오류", "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"실행 중 *{len(errors)}건*의 오류가 발생했습니다.\n\n{error_lines}",
                },
            },
        ]
        try:
            self.client.chat_postMessage(
                channel=slack_id,
                blocks=blocks,
                text=f"QA Monitor 오류 {len(errors)}건 발생",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  ✓ 에러 DM 전송 완료: {slack_id}")
        except SlackApiError as e:
            print(f"  ✗ 에러 DM 전송 실패: {e.response['error']}")

    # ── TC시트 권한 안내 DM ──────────────────────────────────────────────

    def send_sheet_access_dm(self, slack_id: str, qa_card_title: str, card_url: str = "") -> None:
        """TC시트 접근 권한이 없을 때 QA카드 assignee에게 안내 DM을 보낸다."""
        SA_EMAIL = "qa-monitor-bot@qa-monitor-bot.iam.gserviceaccount.com"
        card_link = f"<{card_url}|{_slack_escape(qa_card_title)}>" if card_url else f"*{_slack_escape(qa_card_title)}*"
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "\U0001f4cb TC시트 접근 권한 안내", "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{card_link} 카드의 TC시트에 접근할 수 없어요 \U0001f511\n\n"
                        f"QA Monitor봇이 진행률을 읽으려면 TC시트 파일에 아래 계정을 *뷰어*로 추가해주세요:\n\n"
                        f"`{SA_EMAIL}`\n\n"
                        f"_구글시트 > 공유 > 위 이메일 추가 > 뷰어 권한_"
                    ),
                },
            },
        ]
        try:
            self.client.chat_postMessage(
                channel=slack_id,
                blocks=blocks,
                text=f"TC시트 접근 권한 안내: {qa_card_title}",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  \u2713 TC시트 권한 안내 DM 전송 완료: {slack_id}")
        except SlackApiError as e:
            print(f"  \u2717 TC시트 권한 안내 DM 전송 실패: {e.response['error']}")

    # ── TC시트 미첨부 안내 DM ────────────────────────────────────────────

    def send_sheet_missing_dm(self, slack_id: str, qa_card_title: str, card_url: str = "") -> None:
        """TC시트 링크가 QA카드에 첨부되지 않았을 때 assignee에게 안내 DM을 보낸다."""
        card_link = f"<{card_url}|{_slack_escape(qa_card_title)}>" if card_url else f"*{_slack_escape(qa_card_title)}*"
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "\U0001f4ce TC시트 첨부 안내", "emoji": True},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{card_link} 카드에 테스트케이스 시트 링크가 없어요 \U0001f440\n\n"
                        f"QA Monitor봇이 진행률을 읽으려면 QA카드 Attachments에 TC시트를 첨부해주세요.\n\n"
                        f"_Linear QA카드 > Attachments > 구글시트 링크 추가_\n"
                        f"_첨부 이름에 `테스트케이스` 또는 `testcase`를 포함해주세요._"
                    ),
                },
            },
        ]
        try:
            self.client.chat_postMessage(
                channel=slack_id,
                blocks=blocks,
                text=f"TC시트 첨부 안내: {qa_card_title}",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  \u2713 TC시트 미첨부 안내 DM 전송 완료: {slack_id}")
        except SlackApiError as e:
            print(f"  \u2717 TC시트 미첨부 안내 DM 전송 실패: {e.response['error']}")

    # ── 운영모니터링 DM ──────────────────────────────────────────────────

    def send_monitoring_dm(self, slack_id: str, monitoring_cards: list[dict]) -> None:
        """운영모니터링 대상 카드 목록을 DM으로 보낸다.
        monitoring_cards: [{"identifier": "SUP-1476", "title": "...", "url": "..."}, ...]
        """
        card_lines = "\n".join(
            f"\u2022 {c['identifier']} : <{c['url']}|{c['title']}>"
            for c in monitoring_cards
        )
        text = f":blob-bot: 운영환경 검증 대상 케이스가 존재합니다:heavy_exclamation_mark:\n{card_lines}"
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
        ]
        try:
            self.client.chat_postMessage(
                channel=slack_id,
                blocks=blocks,
                text=f"운영모니터링: {len(monitoring_cards)}건",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  ✓ 운영모니터링 DM 전송 완료: {slack_id}")
        except SlackApiError as e:
            print(f"  ✗ 운영모니터링 DM 전송 실패: {e.response['error']}")

    def send_missing_label_dm(
        self, slack_id: str, issues: list[dict], label_name: str,
        assignee_name: str = "",
    ) -> None:
        """QA 라벨이 누락된 할당 이슈 목록을 DM으로 보낸다."""
        def _format_issue(issue: dict) -> str:
            labels = [n["name"] for n in issue.get("labels", {}).get("nodes", [])]
            label_text = f" (`{'`, `'.join(labels)}`)" if labels else ""
            return f"\u2022 <{issue['url']}|{issue['identifier']}> : {issue['title']}{label_text}"

        issue_lines = "\n".join(_format_issue(issue) for issue in issues)
        who = assignee_name or "담당자"
        text = (
            f":warning: {who}에게 할당된 QA카드 중 *{label_name}* 라벨이 없는 건이 "
            f"*{len(issues)}건* 있습니다.\n\n{issue_lines}"
        )
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
        ]
        try:
            self.client.chat_postMessage(
                channel=slack_id,
                blocks=blocks,
                text=f"QA 라벨 누락: {len(issues)}건",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  ✓ QA 라벨 누락 안내 DM 전송 완료: {slack_id}")
        except SlackApiError as e:
            print(f"  ✗ QA 라벨 누락 안내 DM 전송 실패: {e.response['error']}")

    # ── PRD/Figma 변경 알림 ──────────────────────────────────────────────

    def send_change_alert_dm(
        self, slack_id: str, card_result: dict,
    ) -> None:
        """PRD 또는 Figma 변경 사항을 QA 담당자에게 DM으로 발송.
        card_result: watch_card_changes()의 반환값
        """
        card_id = card_result["card_id"]
        title = card_result["title"]
        card_url = card_result["card_url"]
        prd_change = card_result.get("prd_change")
        figma_changes = card_result.get("figma_changes", [])

        blocks: list[dict] = []

        # 헤더
        blocks.append({
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"\U0001f514 TC 작성 기간 변경 감지 — {card_id}",
                "emoji": True,
            },
        })
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{card_url}|{_slack_escape(title)}>*",
            },
        })
        blocks.append({"type": "divider"})

        # PRD (Linear description) 변경
        if prd_change:
            diff_text = prd_change["diff_text"]
            # Slack 코드블록 (최대 2900자 — Block Kit 제한)
            if len(diff_text) > 2900:
                diff_text = diff_text[:2900] + "\n... (생략)"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*PRD (Description) 변경*\n```\n{diff_text}\n```",
                },
            })

        # Figma 변경
        if figma_changes:
            for fc in figma_changes:
                changes = fc["changes"]
                figma_url = fc.get("url", "")

                added = [c for c in changes if c["type"] == "added"]
                removed = [c for c in changes if c["type"] == "removed"]
                modified = [c for c in changes if c["type"] == "modified"]

                lines = []
                if added:
                    lines.append("*[추가]*")
                    for c in added[:10]:
                        lines.append(f'  + "{c["name"]}" — {c["detail"]}')
                    if len(added) > 10:
                        lines.append(f"  _... 외 {len(added) - 10}건_")
                if removed:
                    lines.append("*[삭제]*")
                    for c in removed[:10]:
                        lines.append(f'  - "{c["name"]}"')
                    if len(removed) > 10:
                        lines.append(f"  _... 외 {len(removed) - 10}건_")
                if modified:
                    lines.append("*[변경]*")
                    for c in modified[:10]:
                        lines.append(f'  ~ "{c["name"]}" — {c["detail"]}')
                    if len(modified) > 10:
                        lines.append(f"  _... 외 {len(modified) - 10}건_")

                figma_text = "\n".join(lines)
                if figma_url:
                    figma_text += f"\n\n<{figma_url}|Figma에서 보기>"

                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Figma 디자인 변경*\n{figma_text}",
                    },
                })

        blocks.append({"type": "divider"})
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "_TC 작성 기간 중 감지된 변경입니다. TC 반영 여부를 확인해주세요._",
            }],
        })

        summary_parts = []
        if prd_change:
            summary_parts.append("PRD")
        if figma_changes:
            summary_parts.append("Figma")
        summary = " + ".join(summary_parts)

        try:
            self.client.chat_postMessage(
                channel=slack_id,
                blocks=blocks,
                text=f"{summary} 변경 감지: {card_id} {title}",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  \u2713 변경 알림 DM 전송 완료: {slack_id} ({summary})")
        except SlackApiError as e:
            print(f"  \u2717 변경 알림 DM 전송 실패: {e.response['error']}")

    # ── PRD/Figma 링크 누락 안내 ──────────────────────────────────────────

    def send_missing_links_dm(
        self, slack_id: str, missing_cards: list[dict],
    ) -> None:
        """TC 작성 기간인 QA카드에 PRD/Figma 링크가 없을 때 안내 DM (복수 카드 통합).
        missing_cards: [{"card_id": str, "title": str, "missing_prd": bool, "missing_figma": bool}, ...]
        """
        prd_cards = []
        figma_cards = []

        for card in missing_cards:
            card_id = card["card_id"]
            title = card["title"]
            card_url = f"https://linear.app/buzzvil/issue/{card_id}"
            card_link = f"<{card_url}|{card_id}: {_slack_escape(title)}>"
            if card["missing_prd"]:
                prd_cards.append(card_link)
            if card["missing_figma"]:
                figma_cards.append(card_link)

        guide = "변경 알림을 받으려면 QA카드 Attachments에 링크를 추가해주세요."
        if prd_cards:
            guide += "\n_PRD가 원래 없는 경우, Description 특이사항에 `PRD 없음`을 기재하면 이 알림이 발송되지 않습니다._"

        lines = []
        if prd_cards:
            lines.append("\u2022 *PRD 링크*")
            for cl in prd_cards:
                lines.append(f"    \u25E6 {cl}")
        if figma_cards:
            lines.append("\u2022 *Figma 디자인 링크*")
            for cl in figma_cards:
                lines.append(f"    \u25E6 {cl}")

        text = f":bell: *TC 작성 기간 링크 안내*\n{guide}\n\n" + "\n".join(lines)
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
        ]

        try:
            self.client.chat_postMessage(
                channel=slack_id,
                blocks=blocks,
                text=f"PRD/Figma 링크 안내: {len(missing_cards)}건",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  \u2713 링크 안내 DM 전송 완료: {slack_id} ({len(missing_cards)}건)")
        except SlackApiError as e:
            print(f"  \u2717 링크 안내 DM 전송 실패: {e.response['error']}")

    # ── 연결 테스트 ────────────────────────────────────────────────────────

    def test_connection(self) -> bool:
        try:
            resp = self.client.auth_test()
            print(f"  Slack 연결 성공: {resp['team']} / Bot: {resp['user']}")
            return True
        except SlackApiError as e:
            print(f"  Slack 연결 실패: {e.response['error']}")
            return False


def _slack_escape(text: str) -> str:
    """Slack mrkdwn 링크 내 특수문자 이스케이프 (&, <, >)"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text_bar(pct: float, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)
