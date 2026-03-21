"""APScheduler 기반 일일 스케줄러 — 채널 요약 + QAM별 개인 시각 지원"""

from collections import defaultdict
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger


def start_scheduler(
    run_summary_fn,
    run_assignee_fn,
    base_time: str,
    assignee_schedules: dict[str, str],
    timezone: str = "Asia/Seoul",
) -> None:
    """
    run_summary_fn()        : 채널 전체 요약 메시지 전송
    run_assignee_fn(name)   : 특정 QAM 개인 메시지 전송
    base_time               : 전체 요약 발송 시각 (HH:MM)
    assignee_schedules      : {"Cindy": "15:00", "Liana": "16:00"}
    """
    scheduler = BlockingScheduler(timezone=timezone)

    # ── 1) 전체 요약 잡 ──────────────────────────────────────────────────
    h, m = base_time.split(":")
    scheduler.add_job(
        run_summary_fn,
        CronTrigger(hour=int(h), minute=int(m), timezone=timezone),
        id="qa_summary",
        name=f"QA 채널 요약 ({base_time})",
        replace_existing=True,
        misfire_grace_time=300,
    )
    print(f"  [요약]  매일 {base_time} 채널 요약 발송")

    # ── 2) QAM별 개인 잡 (같은 시각이면 하나로 묶기) ───────────────────
    time_to_names: dict[str, list[str]] = defaultdict(list)
    for name, send_time in assignee_schedules.items():
        time_to_names[send_time].append(name)

    for send_time, names in time_to_names.items():
        h2, m2 = send_time.split(":")
        job_id = f"qa_assignee_{send_time.replace(':', '')}"
        captured_names = list(names)

        def make_job(ns):
            def job():
                for n in ns:
                    run_assignee_fn(n)
            return job

        scheduler.add_job(
            make_job(captured_names),
            CronTrigger(hour=int(h2), minute=int(m2), timezone=timezone),
            id=job_id,
            name=f"QAM 개인 메시지 ({send_time}) — {', '.join(names)}",
            replace_existing=True,
            misfire_grace_time=300,
        )
        print(f"  [개인]  매일 {send_time} → {', '.join(names)}")

    print(f"\n스케줄러 시작 (타임존: {timezone}). 종료하려면 Ctrl+C")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n스케줄러가 종료되었습니다.")
