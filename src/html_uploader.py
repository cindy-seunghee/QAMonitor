"""buzz-html 대시보드 업로더"""

from __future__ import annotations

import base64
import os
from typing import Optional

import requests


UPLOAD_URL = "https://bofvqthrlxzktxidfzaz.supabase.co/functions/v1/upload"


def upload_dashboard(file_path: str, filename: str = None) -> Optional[str]:
    """HTML 파일을 buzz-html에 업로드하고 URL을 반환한다.

    Returns:
        업로드된 페이지 URL, 실패 시 None
    """
    token = os.environ.get("BUZZ_HTML_TOKEN", "")
    if not token:
        print("  ⚠ BUZZ_HTML_TOKEN 미설정 — buzz-html 업로드 건너뜀")
        return None

    if not filename:
        filename = os.path.basename(file_path)

    with open(file_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    try:
        resp = requests.post(
            UPLOAD_URL,
            headers={"Authorization": f"Bearer {token}"},
            files={
                "content": (None, content_b64),
                "filename": (None, filename),
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            url = data["url"]
            print(f"  ✓ buzz-html 업로드 완료: {url}")
            return url
        else:
            print(f"  ✗ buzz-html 업로드 실패: {data}")
            return None
    except Exception as e:
        print(f"  ✗ buzz-html 업로드 오류: {e}")
        return None
