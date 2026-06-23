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
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "🤖 _이 메시지는 QA Monitor봇이 자동 발송합니다. 테스트 운영 중이므로 내용이 부정확할 수 있습니다._"}],
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

    # ── 테스트 기간 미기재 안내 DM ──────────────────────────────────────────

    def send_missing_dates_dm(
        self, slack_id: str, cards_info: list[dict],
    ) -> None:
        """테스트 기간 미기재 QA카드 안내 DM.
        cards_info: [{"identifier": "SUP-2300", "title": "...", "url": "...", "missing": ["기능테스트", "리그레션테스트"]}, ...]
        """
        lines = [":bell: *테스트 기간 기재 안내*\n"]
        lines.append("QA카드 Description에 테스트 기간이 누락되어 있습니다.\n")
        for card in cards_info:
            missing_str = ", ".join(card["missing"])
            lines.append(f"• <{card['url']}|{card['identifier']} {card['title']}> — _{missing_str}_")
        lines.append("")
        lines.append("_`기능테스트: M/DD ~ M/DD` , `리그레션테스트: M/DD ~ M/DD` 형식으로 기재해주세요._")
        lines.append("_리그레션테스트가 없는 경우 `리그레션테스트: 없음` 으로 기재해주세요._")

        text = "\n".join(lines)
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
                text=f"테스트 기간 기재 안내: {len(cards_info)}건",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  ✓ 테스트 기간 안내 DM 전송 완료: {slack_id}")
        except SlackApiError as e:
            print(f"  ✗ 테스트 기간 안내 DM 전송 실패: {e.response['error']}")

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

    # ── 운영검증 DM ────────────────────────────────────────────────────

    def send_monitoring_dm(self, slack_id: str, monitoring_data: list[dict]) -> None:
        """운영검증 대상 이슈를 QA카드별로 그룹핑하여 DM 발송.
        monitoring_data: [{"card": {"identifier", "title", "url"}, "issues": [{"identifier", "title", "url"}, ...]}, ...]
        """
        lines = [":blob-bot: 운영환경 검증 대상 이슈가 있습니다:heavy_exclamation_mark:\n"]
        total_issues = 0
        for item in monitoring_data:
            issues = item["issues"]
            total_issues += len(issues)
            for issue in issues:
                lines.append(f"• <{issue['url']}|{issue['identifier']}> {issue['title']}")

        text = "\n".join(lines).rstrip()
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
                text=f"운영검증: {total_issues}건",
                unfurl_links=False,
                unfurl_media=False,
            )
            print(f"  ✓ 운영검증 DM 전송 완료: {slack_id}")
        except SlackApiError as e:
            print(f"  ✗ 운영검증 DM 전송 실패: {e.response['error']}")

    # ── PRD/Figma 변경 알림 ──────────────────────────────────────────────

    def _build_card_change_blocks(self, card_result: dict) -> list[dict]:
        """카드 1개의 변경 사항을 블록으로 구성."""
        card_id = card_result["card_id"]
        title = card_result["title"]
        card_url = card_result["card_url"]
        prd_change = card_result.get("prd_change")
        figma_changes = card_result.get("figma_changes", [])

        blocks: list[dict] = []

        # 카드 헤더
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"\u2022 *<{card_url}|{card_id}: {_slack_escape(title)}>*",
            },
        })

        # PRD 변경 — 메인 메시지에는 건수 요약만, 상세는 스레드 코멘트로
        if prd_change:
            diff_text = prd_change.get("diff_text", "")
            prd_url = prd_change.get("card_url", "")
            prd_link = f" (<{prd_url}|PRD 링크>)" if prd_url else ""
            version_info = prd_change.get("version_info", "")
            version_tag = f" ({version_info})" if version_info else ""

            # 건수 집계
            count_lines = []
            for line in diff_text.split("\n"):
                if line.startswith("\u2022 *"):  # • *수정* (N건) 등
                    count_lines.append(line)
            counts = "\n".join(count_lines) if count_lines else "변경 감지됨"

            summary = f"*PRD 변경*{version_tag}{prd_link}\n{counts}"
            summary += "\n_상세 내용은 스레드를 확인해주세요._"
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": summary,
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
                    for c in added:
                        lines.append(f'  + "{c["name"]}" \u2014 {c["detail"]}')
                if removed:
                    lines.append("*[삭제]*")
                    for c in removed:
                        lines.append(f'  - "{c["name"]}"')
                if modified:
                    lines.append("*[변경]*")
                    for c in modified:
                        lines.append(f'  ~ "{c["name"]}" \u2014 {c["detail"]}')

                figma_text = "\n".join(lines)
                figma_link = f" (<{figma_url}|디자인 링크>)" if figma_url else ""

                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Figma 디자인 변경*{figma_link}\n{figma_text}",
                    },
                })

        return blocks

    def send_change_alert_dm(
        self, slack_id: str, card_results: list[dict],
    ) -> None:
        """PRD/Figma 변경 사항을 QA 담당자에게 DM으로 발송 (복수 카드 통합).
        50블록 이내면 1건의 DM, 초과 시 카드별 분리 발송.
        card_results: [watch_card_changes()의 반환값, ...]
        """
        MAX_BLOCKS = 50

        # 헤더 + 푸터 블록
        header = {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "\U0001f514 PRD/\ub514\uc790\uc778 \ubcc0\uacbd \uc54c\ub9bc",
                "emoji": True,
            },
        }
        footer_blocks = [
            {"type": "divider"},
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": "_TC 작성 기간 중 감지된 변경입니다. TC 반영 여부를 확인해주세요._",
                }],
            },
        ]

        # 카드별 블록 생성
        card_block_groups = []
        for result in card_results:
            card_blocks = self._build_card_change_blocks(result)
            card_block_groups.append((result, card_blocks))

        # 전체 블록 수 계산 (헤더 1 + 카드별 블록 + 구분선 + 푸터 2)
        total = 1 + sum(len(b) + 1 for _, b in card_block_groups) + len(footer_blocks)

        if total <= MAX_BLOCKS:
            # 통합 발송
            blocks = [header]
            for i, (result, card_blocks) in enumerate(card_block_groups):
                if i > 0:
                    blocks.append({"type": "divider"})
                blocks.extend(card_blocks)
            blocks.extend(footer_blocks)

            card_ids = ", ".join(r["card_id"] for r in card_results)
            try:
                resp = self.client.chat_postMessage(
                    channel=slack_id,
                    blocks=blocks,
                    text=f"PRD/디자인 변경 알림: {card_ids}",
                    unfurl_links=False,
                    unfurl_media=False,
                )
                print(f"  \u2713 변경 알림 DM 전송 완료: {slack_id} ({len(card_results)}건 통합)")
                # 상세 내용이 요약과 다르면 스레드로 전체 발송
                main_ts = resp["ts"]
                self._post_prd_detail_thread(slack_id, main_ts, card_results)
            except SlackApiError as e:
                print(f"  \u2717 변경 알림 DM 전송 실패: {e.response['error']}")
        else:
            # 카드별 분리 발송
            for result, card_blocks in card_block_groups:
                blocks = [header]
                blocks.extend(card_blocks)
                blocks.extend(footer_blocks)
                try:
                    resp = self.client.chat_postMessage(
                        channel=slack_id,
                        blocks=blocks,
                        text=f"PRD/디자인 변경 알림: {result['card_id']}",
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                    print(f"  \u2713 변경 알림 DM 전송 완료: {slack_id} ({result['card_id']})")
                    main_ts = resp["ts"]
                    self._post_prd_detail_thread(slack_id, main_ts, [result])
                except SlackApiError as e:
                    print(f"  \u2717 변경 알림 DM 전송 실패: {e.response['error']}")

    def _post_prd_detail_thread(self, channel: str, thread_ts: str, card_results: list[dict]) -> None:
        """메인 메시지의 스레드에 PRD 변경 상세를 rich_text 블록으로 발송."""
        from src.change_watcher import format_changes_rich_text

        for result in card_results:
            prd_change = result.get("prd_change")
            if not prd_change:
                continue

            # changes 리스트가 없으면 diff_text 폴백
            changes = prd_change.get("changes")
            card_id = result.get("card_id", "")

            if changes:
                # rich_text 블록으로 변환
                blocks = format_changes_rich_text(changes)
                if not blocks:
                    continue
                # rich_text 블록은 크기가 클 수 있으므로 50블록 제한 내에서 발송
                try:
                    self.client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        blocks=blocks,
                        text=f"PRD 변경 상세: {card_id}",
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                except SlackApiError as e:
                    # rich_text 실패 시 mrkdwn 폴백
                    print(f"  \u26A0 rich_text 전송 실패 ({e.response['error']}), mrkdwn 폴백")
                    self._post_prd_detail_thread_mrkdwn(channel, thread_ts, prd_change, card_id)
            else:
                # changes 없으면 diff_text로 mrkdwn 폴백
                self._post_prd_detail_thread_mrkdwn(channel, thread_ts, prd_change, card_id)

    def _post_prd_detail_thread_mrkdwn(self, channel: str, thread_ts: str, prd_change: dict, card_id: str) -> None:
        """mrkdwn 폴백: diff_text를 분할하여 스레드 발송."""
        diff_text = prd_change.get("diff_text", "")
        if not diff_text:
            return
        chunks = []
        current = ""
        for line in diff_text.split("\n"):
            if len(current) + len(line) + 1 > 2800 and current:
                chunks.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        if current:
            chunks.append(current)
        for chunk in chunks:
            if len(chunk) > 2950:
                chunk = chunk[:2950] + "\n... (생략)"
            try:
                self.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": chunk}}],
                    text=f"PRD 변경 상세: {card_id}",
                    unfurl_links=False,
                    unfurl_media=False,
                )
            except SlackApiError as e:
                print(f"  \u2717 PRD 상세 스레드 전송 실패: {e.response['error']}")
                break


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
