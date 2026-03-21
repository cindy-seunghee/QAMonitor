# 메인 메시지
# 채널에 보이는 첫 번째 메시지입니다.
# {test_phase}는 현재 테스트 단계로 자동 치환됩니다 (통합테스트 / 리그레션테스트)
intro: [{test_phase}] QA 현황 보고입니다.
sections: progress, bug_summary, priority_breakdown, exit_summary
footer: 담당자별 상세 현황과 배포 기준은 스레드를 확인해주세요.

# 스레드: 담당자별 현황
# QAM 멘션 + 담당 테스트 진행률 + 미해결 버그 목록
sections: assignee_detail

# 스레드: 배포 기준 체크리스트 [통합테스트]
# 통합테스트 단계일 때만 표시
sections: exit_checklist

# 스레드: 배포 기준 체크리스트 [리그레션테스트]
# 리그레션테스트 단계일 때만 표시
sections: exit_checklist
