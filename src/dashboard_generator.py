"""HTML 대시보드 생성기"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _load_checklist(path: str) -> list[str]:
    """deployment_checklist.md에서 항목을 읽어온다. `- ` 로 시작하는 줄만 파싱."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        items = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- "):
                item = stripped[2:].strip()
                # [ ] 체크박스 제거
                if item.startswith("[ ] "):
                    item = item[4:]
                # ** 마크다운 볼드 → HTML <strong>
                import re
                item = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', item)
                items.append(item)
        return items
    except FileNotFoundError:
        return []


def generate_dashboard(data: dict, output_dir: str = "output", checklist_path: str = "deployment_checklist.md") -> str:
    """분석 데이터로 HTML 대시보드를 생성하고 파일 경로를 반환"""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    kst = timezone(timedelta(hours=9))
    timestamp = datetime.now(kst).strftime("%Y%m%d_%H%M%S")
    filename = f"qa_dashboard_{timestamp}.html"
    filepath = os.path.join(output_dir, filename)

    # 최신 파일 링크도 갱신
    latest_path = os.path.join(output_dir, "qa_dashboard_latest.html")

    checklist_items = _load_checklist(checklist_path)
    html = _render_html(data, checklist_items)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(html)

    return filepath


def _render_html(data: dict, checklist_items: list[str] = None) -> str:
    project_name = data.get("project_name", "QA Project")
    generated_at = data.get("generated_at", "")
    dday = data.get("dday", "")
    release_date = data.get("release_date", "")
    progress = data.get("progress", {})
    priority_breakdown = data.get("priority_breakdown", [])
    status_breakdown = data.get("status_breakdown", [])
    by_assignee = data.get("by_assignee", {})
    trend = data.get("trend", {})
    exit_status = data.get("exit_status", [])
    open_bug_count = data.get("open_bug_count", 0)
    today_new = data.get("today_new_count", 0)
    open_bugs = data.get("open_bugs", [])
    max_bugs_display = data.get("max_bugs_display", 50)
    trend_days = data.get("trend_days", 14)
    platform_breakdown = data.get("platform_breakdown", {})
    critical_issues = data.get("critical_issues", [])

    # 진행률이 "?"인 경우 표시용 값 분리
    pct_raw = progress.get("pct", 0)
    pct_is_unknown = (pct_raw == "?")
    pct_display = "?" if pct_is_unknown else pct_raw
    pct_num = 0 if pct_is_unknown else pct_raw
    progress_source = progress.get("source", "")
    progress_note = ""
    if pct_is_unknown:
        progress_note = f"<div style='font-size:12px;color:#f97316;margin-top:8px'>⚠ 테스트케이스 시트 접근 불가 — 진행률을 확인할 수 없습니다</div>"

    # 프로그레스바 색상 (계획 대비 진행률 기준)
    progress_status = data.get("progress_status", {})
    progress_ratio = progress_status.get("ratio", 1)
    if pct_is_unknown or progress_ratio <= 0.5:
        progress_bar_color = "#ef4444"        # 빨강
    elif progress_ratio <= 0.7:
        progress_bar_color = "#eab308"        # 노랑
    else:
        progress_bar_color = "#3b82f6"        # 파랑

    # 배포 체크리스트 HTML (md 파일에서 로드)
    if checklist_items:
        checklist_html = "\n          ".join(f"<p>☑ {item}</p>" for item in checklist_items)
    else:
        checklist_html = "<p style='color:#9ca3af'>deployment_checklist.md 파일을 확인하세요.</p>"

    # Chart.js 데이터
    priority_labels = json.dumps([p["priority"] for p in priority_breakdown])
    priority_values = json.dumps([p["count"] for p in priority_breakdown])
    priority_colors = json.dumps(["#ef4444", "#f97316", "#eab308", "#3b82f6", "#9ca3af"])

    status_labels = json.dumps([s["status"] for s in status_breakdown[:8]])
    status_values = json.dumps([s["count"] for s in status_breakdown[:8]])
    _status_color_map = {
        "Todo": "#ef4444",                # 빨강 — 미시작
        "Backlog": "#ef4444",             # 빨강
        "In Progress": "#f97316",         # 주황 — 작업 중
        "In Review": "#eab308",           # 노랑 — 리뷰 대기
        "개발자 QA DONE": "#a78bfa",       # 보라 — QA 검증 전 (불확실)
        "Staging QA DONE": "#22c55e",     # 초록 — 해결
        "Prodmini QA DONE": "#22c55e",    # 초록
        "Prod QA DONE": "#22c55e",        # 초록
        "Done": "#16a34a",                # 진한 초록
        "Not a Bug": "#9ca3af",           # 회색 — 제외
        "Can't Reproduce": "#d1d5db",     # 연회색
        "Won't Fix": "#d1d5db",           # 연회색
        "Canceled": "#d1d5db",            # 연회색
        "Duplicate": "#d1d5db",           # 연회색
    }
    status_colors = json.dumps([
        _status_color_map.get(s["status"], s.get("color", "#9ca3af"))
        for s in status_breakdown[:8]
    ])

    trend_labels = json.dumps(trend.get("labels", []))
    trend_created = json.dumps(trend.get("created", []))
    trend_resolved = json.dumps(trend.get("resolved", []))

    # 담당자 테이블 행
    assignee_rows = ""
    for name, info in by_assignee.items():
        pct = info["qa_pct"]
        total = info["qa_total"]
        done = info["qa_done"]
        bug_cnt = len(info["open_bugs"])
        bar_color = "#22c55e" if pct >= 80 else "#f97316" if pct >= 50 else "#ef4444"
        if total == 0 and bug_cnt > 0:
            # QA 카드 없이 버그만 있는 담당자
            assignee_rows += f"""
            <tr>
                <td><span class="assignee-badge">{name}</span></td>
                <td class="text-center">—</td>
                <td class="text-center">—</td>
                <td class="text-center">—</td>
                <td><div class="progress-bar-wrap"><div class="progress-bar" style="width:0%;background:#9ca3af"></div><span>—</span></div></td>
                <td class="text-center"><span class="badge {'badge-red' if bug_cnt > 0 else 'badge-green'}">{bug_cnt}</span></td>
            </tr>"""
        else:
            assignee_rows += f"""
            <tr>
                <td><span class="assignee-badge">{name}</span></td>
                <td class="text-center">{total}</td>
                <td class="text-center">{done}</td>
                <td class="text-center">{total - done}</td>
                <td><div class="progress-bar-wrap"><div class="progress-bar" style="width:{pct}%;background:{bar_color}"></div><span>{pct}%</span></div></td>
                <td class="text-center"><span class="badge {'badge-red' if bug_cnt > 0 else 'badge-green'}">{bug_cnt}</span></td>
            </tr>"""

    # 오픈 버그 테이블
    priority_badge = {
        "Urgent": "badge-red",
        "High": "badge-orange",
        "Medium": "badge-yellow",
        "Low": "badge-blue",
        "No priority": "badge-gray",
    }
    bug_rows = ""
    for bug in open_bugs[:max_bugs_display]:
        identifier = bug.get("identifier", "")
        title = bug.get("title", "")
        priority = bug.get("priorityLabel", "No priority")
        state = bug.get("state", {}).get("name", "")
        assignee = (bug.get("assignee") or {}).get("displayName") or "미지정"
        url = bug.get("url", "#")
        created = bug.get("createdAt", "")[:10] if bug.get("createdAt") else ""
        badge_cls = priority_badge.get(priority, "badge-gray")
        bug_rows += f"""
        <tr>
            <td><a href="{url}" target="_blank" class="issue-link">{identifier}</a></td>
            <td><a href="{url}" target="_blank" class="issue-link">{title[:60]}{'...' if len(title) > 60 else ''}</a></td>
            <td><span class="badge {badge_cls}">{priority}</span></td>
            <td>{state}</td>
            <td>{assignee}</td>
            <td class="text-center text-muted">{created}</td>
        </tr>"""

    # 플랫폼별 비교 카드
    platform_cards_html = ""
    platform_bar_html = ""
    total_all_platform = sum(p["total"] for p in platform_breakdown.values()) or 1
    platform_icons = {"iOS": "\U0001F34E", "Android": "\U0001F916", "Web": "\U0001F310", "공통": "\u2699\uFE0F"}
    platform_colors = {"iOS": "#555", "Android": "#3ddc84", "Web": "#0065ff", "공통": "#8777d9"}
    for pname, pdata in platform_breakdown.items():
        icon = platform_icons.get(pname, "")
        high_cls = "metric-red" if pdata["high"] > 0 else "metric-green"
        platform_cards_html += f"""
        <div class="card">
          <div style="font-size:13px;font-weight:700;margin-bottom:12px">{icon} {pname}</div>
          <div style="display:flex;justify-content:space-between;margin-bottom:6px">
            <span style="font-size:12px;color:#6b7280">전체 이슈</span>
            <span style="font-weight:700">{pdata['total']}건</span>
          </div>
          <div style="display:flex;justify-content:space-between;margin-bottom:6px">
            <span style="font-size:12px;color:#6b7280">미해결</span>
            <span style="font-weight:700;color:#f97316">{pdata['open']}건</span>
          </div>
          <div style="display:flex;justify-content:space-between">
            <span style="font-size:12px;color:#6b7280">High/Urgent</span>
            <span class="{high_cls}" style="font-weight:700">{pdata['high']}건</span>
          </div>
        </div>"""
        pct = round(pdata["total"] / total_all_platform * 100, 1)
        color = platform_colors.get(pname, "#888")
        platform_bar_html += f"""
        <div style="display:flex;align-items:center;margin-bottom:8px">
          <span style="width:80px;font-size:12px;color:#6b7280">{icon} {pname}</span>
          <div style="flex:1;background:#e5e7eb;border-radius:4px;height:18px;overflow:hidden">
            <div style="width:{pct}%;height:100%;background:{color};border-radius:4px;display:flex;align-items:center;justify-content:flex-end;padding-right:6px;font-size:11px;font-weight:700;color:#fff;min-width:30px">{pdata['total']}건</div>
          </div>
        </div>"""

    # 크리티컬 이슈 테이블
    critical_rows_html = ""
    for bug in critical_issues[:15]:
        identifier = bug.get("identifier", "")
        title = bug.get("title", "")[:50]
        priority = bug.get("priorityLabel", "")
        state = bug.get("state", {}).get("name", "")
        assignee = (bug.get("assignee") or {}).get("displayName") or "미지정"
        url = bug.get("url", "#")
        badge_cls = "badge-red" if priority == "Urgent" else "badge-orange"
        state_cls = "badge-red" if state == "Todo" else "badge-yellow" if state == "In Progress" else "badge-blue"
        critical_rows_html += f"""
        <tr>
          <td><a href="{url}" target="_blank" class="issue-link">{identifier}</a></td>
          <td><a href="{url}" target="_blank" class="issue-link">{title}</a></td>
          <td><span class="badge {badge_cls}">{priority}</span></td>
          <td><span class="badge {state_cls}">{state}</span></td>
          <td>{assignee}</td>
        </tr>"""

    # 배포 판정 (상세)
    urgent_count = sum(1 for b in open_bugs if b.get("priorityLabel") == "Urgent")
    high_count = sum(1 for b in open_bugs if b.get("priorityLabel") == "High")
    if urgent_count == 0 and high_count == 0 and all(c["pass"] for c in exit_status):
        verdict_text = "배포 가능"
        verdict_cls = "status-ok"
        verdict_icon = "\u2705"
        verdict_detail = "모든 배포 기준이 충족되었습니다."
    elif urgent_count > 0:
        verdict_text = "배포 불가"
        verdict_cls = "status-ng"
        verdict_icon = "\u26D4"
        verdict_detail = f"Urgent {urgent_count}건, High {high_count}건 미해결. 크리티컬 이슈 해결 후 재판정이 필요합니다."
    elif high_count > 0:
        verdict_text = "조건부 배포 가능"
        verdict_cls = "status-warn"
        verdict_icon = "\u26A0\uFE0F"
        verdict_detail = f"High {high_count}건 미해결. 해당 이슈의 영향도 검토 후 배포 여부를 결정해야 합니다."
    else:
        verdict_text = "배포 불가"
        verdict_cls = "status-ng"
        verdict_icon = "\u274C"
        verdict_detail = "일부 배포 기준이 미충족입니다."

    # 종료 기준 체크리스트
    criteria_rows = ""
    all_pass = all(c["pass"] for c in exit_status)
    for c in exit_status:
        icon = "✅" if c["pass"] else "❌"
        row_cls = "criteria-pass" if c["pass"] else "criteria-fail"
        criteria_rows += f"""
        <div class="criteria-item {row_cls}">
            <span class="criteria-icon">{icon}</span>
            <span class="criteria-label">{c['label']}</span>
            <span class="criteria-value">{c['current']}</span>
        </div>"""

    deploy_status_text = "배포 가능" if all_pass else "배포 불가"
    deploy_status_cls = "status-ok" if all_pass else "status-ng"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{project_name} - QA 대시보드</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; color: #1a1a2e; }}
  a {{ color: inherit; text-decoration: none; }}

  /* ── Header ── */
  .header {{
    background: linear-gradient(135deg, #1e1b4b 0%, #312e81 50%, #4338ca 100%);
    color: white; padding: 24px 32px;
    display: flex; justify-content: space-between; align-items: center;
  }}
  .header-left h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .header-left p {{ font-size: 13px; opacity: 0.75; }}
  .header-right {{ text-align: right; }}
  .dday-badge {{
    display: inline-block; background: rgba(255,255,255,0.2);
    border: 1px solid rgba(255,255,255,0.4);
    border-radius: 20px; padding: 6px 16px;
    font-size: 18px; font-weight: 700; margin-bottom: 4px;
  }}
  .release-info {{ font-size: 12px; opacity: 0.7; }}

  /* ── Layout ── */
  .container {{ max-width: 1400px; margin: 0 auto; padding: 24px 24px; }}
  .section {{ margin-bottom: 28px; }}
  .section-title {{
    font-size: 15px; font-weight: 600; color: #374151;
    margin-bottom: 14px; padding-left: 10px;
    border-left: 4px solid #4338ca;
  }}
  .grid-4 {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }}
  .grid-3 {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }}
  .grid-2 {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }}
  @media (max-width: 1024px) {{ .grid-4 {{ grid-template-columns: repeat(2, 1fr); }} }}
  @media (max-width: 768px) {{ .grid-4, .grid-3, .grid-2 {{ grid-template-columns: 1fr; }} }}

  /* ── Cards ── */
  .card {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .metric-card {{ text-align: center; }}
  .metric-value {{ font-size: 36px; font-weight: 700; margin: 8px 0 4px; }}
  .metric-label {{ font-size: 13px; color: #6b7280; }}
  .metric-sub {{ font-size: 12px; color: #9ca3af; margin-top: 4px; }}
  .metric-red {{ color: #ef4444; }}
  .metric-orange {{ color: #f97316; }}
  .metric-green {{ color: #22c55e; }}
  .metric-blue {{ color: #3b82f6; }}
  .metric-purple {{ color: #8b5cf6; }}

  /* ── Progress ── */
  .big-progress-wrap {{
    background: #e5e7eb; border-radius: 999px; height: 24px;
    overflow: hidden; margin: 12px 0 8px;
  }}
  .big-progress-bar {{
    height: 100%; border-radius: 999px;
    background: linear-gradient(90deg, #4338ca, #7c3aed);
    transition: width 0.8s ease;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 600; color: white;
    min-width: 40px;
  }}
  .progress-stats {{ display: flex; gap: 20px; margin-top: 12px; }}
  .progress-stat {{ display: flex; align-items: center; gap: 6px; font-size: 13px; }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; }}

  /* ── Progress bar in table ── */
  .progress-bar-wrap {{
    display: flex; align-items: center; gap: 8px;
  }}
  .progress-bar-wrap .progress-bar {{
    height: 8px; border-radius: 4px; min-width: 2px; max-width: 100px;
  }}
  .progress-bar-wrap span {{ font-size: 12px; color: #6b7280; white-space: nowrap; }}

  /* ── Charts ── */
  .chart-container {{ position: relative; height: 220px; }}

  /* ── Tables ── */
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f9fafb; padding: 10px 12px; text-align: left; font-weight: 600; color: #6b7280; font-size: 12px; border-bottom: 1px solid #e5e7eb; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #f3f4f6; vertical-align: middle; }}
  tr:hover td {{ background: #fafafa; }}
  tr:last-child td {{ border-bottom: none; }}
  .text-center {{ text-align: center; }}
  .text-muted {{ color: #9ca3af; }}

  /* ── Badges ── */
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 600;
  }}
  .badge-red {{ background: #fee2e2; color: #dc2626; }}
  .badge-orange {{ background: #ffedd5; color: #ea580c; }}
  .badge-yellow {{ background: #fef9c3; color: #ca8a04; }}
  .badge-blue {{ background: #dbeafe; color: #2563eb; }}
  .badge-gray {{ background: #f3f4f6; color: #6b7280; }}
  .badge-green {{ background: #dcfce7; color: #16a34a; }}
  .assignee-badge {{
    display: inline-block; background: #ede9fe; color: #5b21b6;
    padding: 3px 10px; border-radius: 6px; font-size: 12px; font-weight: 600;
  }}

  /* ── Exit Criteria ── */
  .criteria-item {{
    display: flex; align-items: center; gap: 12px;
    padding: 12px 16px; border-radius: 8px; margin-bottom: 8px;
  }}
  .criteria-pass {{ background: #f0fdf4; border: 1px solid #bbf7d0; }}
  .criteria-fail {{ background: #fef2f2; border: 1px solid #fecaca; }}
  .criteria-icon {{ font-size: 18px; }}
  .criteria-label {{ flex: 1; font-size: 13px; font-weight: 500; }}
  .criteria-value {{ font-size: 13px; font-weight: 700; color: #374151; }}

  .deploy-status {{
    text-align: center; padding: 16px; border-radius: 10px; margin-top: 16px;
    font-size: 18px; font-weight: 700;
  }}
  .status-ok {{ background: #dcfce7; color: #15803d; border: 2px solid #86efac; }}
  .status-ng {{ background: #fee2e2; color: #dc2626; border: 2px solid #fca5a5; }}
  .status-warn {{ background: #fef9c3; color: #a16207; border: 2px solid #fde047; }}

  /* ── Issue link ── */
  .issue-link {{ color: #4338ca; }}
  .issue-link:hover {{ text-decoration: underline; }}

  .updated-at {{ text-align: right; font-size: 11px; color: #9ca3af; margin-top: 8px; }}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h1>{project_name} — QA 모니터링 대시보드</h1>
    <p>{'[' + data.get('test_phase', '') + '] ' if data.get('test_phase') else ''}마지막 업데이트: {generated_at[:19].replace('T', ' ')}</p>
  </div>
  <div class="header-right">
    {'<div class="dday-badge">' + dday + '</div>' if dday else ''}
    {'<div class="release-info">릴리즈 목표: ' + release_date + '</div>' if release_date else ''}
  </div>
</div>

<div class="container">

  <!-- ── 핵심 지표 ── -->
  <div class="section">
    <div class="section-title">핵심 지표</div>
    <div class="grid-4">
      <div class="card metric-card">
        <div class="metric-label">테스트 진행률</div>
        <div class="metric-value metric-purple">{pct_display}{'%' if not pct_is_unknown else ''}</div>
        <div class="metric-sub">{progress.get('done', 0)} / {progress.get('total', 0)} 완료</div>
      </div>
      <div class="card metric-card">
        <div class="metric-label">미해결 버그</div>
        <div class="metric-value {'metric-red' if open_bug_count > 0 else 'metric-green'}">{open_bug_count}</div>
        <div class="metric-sub">오픈 이슈</div>
      </div>
      <div class="card metric-card">
        <div class="metric-label">오늘 신규 이슈</div>
        <div class="metric-value {'metric-orange' if today_new > 0 else 'metric-blue'}">{today_new}</div>
        <div class="metric-sub">금일 등록</div>
      </div>
      <div class="card metric-card">
        <div class="metric-label">테스트 미완료</div>
        <div class="metric-value metric-orange">{progress.get('not_started', 0) + progress.get('in_progress', 0)}</div>
        <div class="metric-sub">잔여 케이스</div>
      </div>
    </div>
  </div>

  <!-- ── 테스트 진행 현황 ── -->
  <div class="section">
    <div class="section-title">테스트 진행 현황</div>
    <div class="card">
      <div class="big-progress-wrap">
        <div class="big-progress-bar" style="width:{pct_num}%;background:{progress_bar_color}">
          {pct_display}{'%' if not pct_is_unknown else ''}
        </div>
      </div>
      {progress_note}
    </div>
  </div>

  <!-- ── 이슈 현황 차트 ── -->
  <div class="section">
    <div class="section-title">이슈 현황</div>
    <div class="grid-2">
      <div class="card">
        <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:12px">우선순위별 미해결 버그</div>
        <div class="chart-container">
          <canvas id="priorityChart"></canvas>
        </div>
      </div>
      <div class="card">
        <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:12px">상태별 이슈 분포</div>
        <div class="chart-container">
          <canvas id="statusChart"></canvas>
        </div>
      </div>
    </div>
  </div>

  <!-- ── 배포 판정 ── -->
  <div class="section">
    <div class="section-title">배포 가능 여부 판정</div>
    <div class="card">
      <div class="deploy-status {verdict_cls}" style="font-size:20px">
        {verdict_icon} {verdict_text}
      </div>
      <p style="text-align:center;font-size:13px;color:#6b7280;margin-top:12px">{verdict_detail}</p>
    </div>
  </div>

  <!-- ── 플랫폼별 비교 ── -->
  <div class="section">
    <div class="section-title">플랫폼별 이슈 비교</div>
    <div class="grid-4">
      {platform_cards_html}
    </div>
    <div class="card" style="margin-top:16px">
      <div style="font-size:13px;font-weight:600;color:#374151;margin-bottom:12px">플랫폼별 이슈 비율</div>
      {platform_bar_html}
    </div>
  </div>

  <!-- ── 크리티컬 이슈 상세 ── -->
  <div class="section">
    <div class="section-title">크리티컬 이슈 (Urgent / High 미해결 — {len(critical_issues)}건)</div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width:90px">ID</th>
              <th>제목</th>
              <th style="width:90px">우선순위</th>
              <th style="width:100px">상태</th>
              <th style="width:100px">담당자</th>
            </tr>
          </thead>
          <tbody>
            {critical_rows_html if critical_rows_html else '<tr><td colspan="5" class="text-center text-muted" style="padding:20px">크리티컬 이슈 없음</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ── 일별 추이 ── -->
  <div class="section">
    <div class="section-title">이슈 추이 (최근 14일)</div>
    <div class="card">
      <div class="chart-container" style="height:200px">
        <canvas id="trendChart"></canvas>
      </div>
    </div>
  </div>

  <!-- ── 담당자별 현황 ── -->
  <div class="section">
    <div class="section-title">담당자별 현황</div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>담당자</th>
              <th class="text-center">QA 케이스</th>
              <th class="text-center">완료</th>
              <th class="text-center">잔여</th>
              <th>진행률</th>
              <th class="text-center">미해결 버그</th>
            </tr>
          </thead>
          <tbody>
            {assignee_rows if assignee_rows else '<tr><td colspan="6" class="text-center text-muted" style="padding:20px">데이터 없음</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ── 미해결 버그 목록 ── -->
  <div class="section">
    <div class="section-title">미해결 버그 목록 (우선순위 순, 최대 {max_bugs_display}개)</div>
    <div class="card">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width:90px">ID</th>
              <th>제목</th>
              <th style="width:100px">우선순위</th>
              <th style="width:110px">상태</th>
              <th style="width:100px">담당자</th>
              <th class="text-center" style="width:90px">등록일</th>
            </tr>
          </thead>
          <tbody>
            {bug_rows if bug_rows else '<tr><td colspan="6" class="text-center text-muted" style="padding:20px">미해결 버그 없음 🎉</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- ── 배포 기준 체크 ── -->
  <div class="section">
    <div class="section-title">배포 기준 체크리스트</div>
    <div class="card">
      {criteria_rows if criteria_rows else '<p class="text-muted" style="text-align:center;padding:20px">기준 없음</p>'}
      <div class="deploy-status {verdict_cls}">
        {verdict_icon} {verdict_text}
      </div>
    </div>
  </div>

</div>

<script>
const priorityChart = new Chart(document.getElementById('priorityChart'), {{
  type: 'doughnut',
  data: {{
    labels: {priority_labels},
    datasets: [{{ data: {priority_values}, backgroundColor: {priority_colors}, borderWidth: 2, borderColor: '#fff' }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: 'right', labels: {{ font: {{ size: 12 }}, boxWidth: 14 }} }},
      datalabels: {{
        color: '#fff',
        font: {{ weight: 'bold', size: 13 }},
        formatter: (value) => value > 0 ? value : ''
      }}
    }}
  }},
  plugins: [ChartDataLabels]
}});

const statusChart = new Chart(document.getElementById('statusChart'), {{
  type: 'bar',
  data: {{
    labels: {status_labels},
    datasets: [{{ data: {status_values}, backgroundColor: {status_colors}, borderRadius: 6 }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }}, datalabels: {{ display: false }} }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 11 }} }} }},
      y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }}
    }}
  }}
}});

const trendChart = new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{
    labels: {trend_labels},
    datasets: [
      {{
        label: '신규 이슈',
        data: {trend_created},
        borderColor: '#ef4444', backgroundColor: 'rgba(239,68,68,0.1)',
        fill: true, tension: 0.3, pointRadius: 4
      }},
      {{
        label: '해결된 이슈',
        data: {trend_resolved},
        borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.1)',
        fill: true, tension: 0.3, pointRadius: 4
      }}
    ]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'top', labels: {{ font: {{ size: 12 }} }} }}, datalabels: {{ display: false }} }},
    scales: {{
      x: {{ grid: {{ display: false }} }},
      y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }}
    }}
  }}
}});
</script>
</body>
</html>"""
