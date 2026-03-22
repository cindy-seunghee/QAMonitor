# 메인 메시지
# {} 안의 값은 자동으로 치환됩니다.
# 아이콘은 계획 대비 진행률에 따라 자동 결정:
#   ≤50% → :mulgae_redcard:  /  ≤70% → :mulgae_yellowcard:  /  >70% → :mulgae_love:
format: *{today} {project_name}* *`{test_phase}` 진행 상황*
format: {progress_icon} *테스트 진행률* : *`{progress_pct}`* *%*
format: • *잔여 이슈* : *`{open_bug_count}`*건({remaining_issues_link})
format:     ◦ *Urgent* : *`{urgent_count}`*건 {urgent_if_exists}
format:     ◦ *High* : *`{high_count}`*건 {high_if_exists}
format:     ◦ Medium : {medium_count}건 {medium_if_exists}
format:     ◦ Low : {low_count}건 {low_if_exists}
format: • *대시보드* ({dashboard_link})
format: {developer_mentions}
format: 미해결 결함을 확인해주세요 :mulgae_sad:

# 스레드: 담당자별 현황
sections: assignee_detail

# 스레드: 배포 기준 체크리스트 [통합테스트]
sections: exit_checklist

# 스레드: 배포 기준 체크리스트 [리그레션테스트]
sections: exit_checklist
