"""PRD/Figma 변경 감지 — TC 작성 기간 중 description/디자인 변경 모니터링"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path

import requests


SNAPSHOT_DIR = Path("snapshots")
MAX_DEPTH = 10

# Figma URL에서 fileKey, nodeId 추출
_FIGMA_URL_RE = re.compile(
    r"figma\.com/(?:design|file)/([a-zA-Z0-9]+)(?:/[^?]*)?\?.*?node-id=([0-9]+-[0-9]+)"
)


# ── PRD 소스 탐색 (Linear / Confluence) ──────────────────────────────────

_LINEAR_ISSUE_RE = re.compile(r"linear\.app/[^/]+/issue/([A-Z]+-\d+)")
_CONFLUENCE_PAGE_RE = re.compile(r"atlassian\.net/wiki/.*?/(?:pages|history)/(\d+)")
# description 본문에서 전체 URL을 추출하기 위한 패턴
_CONFLUENCE_URL_RE = re.compile(r"https?://[^\s()<>\]]+atlassian\.net/wiki/[^\s()<>\]]+")


def _find_prd_source(qa_card: dict) -> dict | None:
    """QA카드 Attachments에서 PRD 소스를 찾는다.

    Returns:
      {"type": "linear", "identifier": "SUP-1982", "url": "..."} |
      {"type": "confluence", "page_id": "4959633485", "url": "..."} |
      None
    """
    attachments = qa_card.get("attachments", {}).get("nodes", [])
    for att in attachments:
        title = (att.get("title") or "").strip().lower()
        url = att.get("url") or ""
        if "prd" not in title:
            continue
        # Linear PRD
        m = _LINEAR_ISSUE_RE.search(url)
        if m:
            return {"type": "linear", "identifier": m.group(1), "url": url}
        # Confluence PRD
        m = _CONFLUENCE_PAGE_RE.search(url)
        if m:
            return {"type": "confluence", "page_id": m.group(1), "url": url}
    return None


# ── Confluence 페이지 본문 조회 ───────────────────────────────────────────


def _fetch_confluence_page(page_id: str) -> dict | None:
    """Confluence REST API로 페이지 제목 + 본문을 조회.

    Returns: {"title": str, "body": str, "version": int} | None
    """
    email = os.environ.get("CONFLUENCE_EMAIL", "")
    token = os.environ.get("CONFLUENCE_TOKEN", "")
    if not email or not token:
        print("      CONFLUENCE_EMAIL 또는 CONFLUENCE_TOKEN 미설정 — Confluence PRD 감지 건너뜀")
        return None

    domain = os.environ.get("CONFLUENCE_DOMAIN", "buzzvil.atlassian.net")
    url = f"https://{domain}/wiki/api/v2/pages/{page_id}?body-format=storage"

    resp = requests.get(url, auth=(email, token), timeout=30)
    if resp.status_code != 200:
        print(f"      Confluence API 오류 ({resp.status_code}): {resp.text[:200]}")
        return None

    data = resp.json()
    title = data.get("title", "")
    body_html = data.get("body", {}).get("storage", {}).get("value", "")
    version = data.get("version", {}).get("number", 0)

    # HTML → 텍스트 변환 (간이 파싱)
    body_text = _html_to_text(body_html)
    return {"title": title, "body": body_text, "html": body_html, "version": version}


def _html_to_text(html: str) -> str:
    """Confluence storage format HTML을 텍스트로 변환."""
    import html as html_module
    text = html

    # 블록 요소 앞에 줄바꿈 보장
    for tag in ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "table", "ul", "ol"]:
        text = re.sub(rf"<{tag}(\s|>)", rf"\n<{tag}\1", text)

    # 헤딩 → Markdown 스타일 (줄바꿈 포함)
    for i in range(1, 7):
        text = re.sub(
            rf"<h{i}[^>]*>(.*?)</h{i}>",
            lambda m, level=i: f"\n{'#' * level} {m.group(1).strip()}\n",
            text, flags=re.DOTALL,
        )

    # 리스트 아이템
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n* \1", text, flags=re.DOTALL)

    # 테이블 행 → 각 셀을 " | "로 연결하여 한 줄로
    def _table_row(m):
        row_html = m.group(1)
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.DOTALL)
        # 셀 내부 HTML 태그 제거 + 줄바꿈 제거 + 공백 정리
        cleaned = []
        for c in cells:
            c = re.sub(r"<[^>]+>", "", c)
            c = html_module.unescape(c)
            c = c.replace("\n", " ").strip()
            if c:
                cleaned.append(c)
        if not cleaned:
            return ""
        if all(c.startswith("--") for c in cleaned):
            return ""
        return "\n[표] " + " | ".join(cleaned)

    text = re.sub(r"<tr[^>]*>(.*?)</tr>", _table_row, text, flags=re.DOTALL)

    # 줄바꿈 태그
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<p[^>]*>", "\n", text)
    text = re.sub(r"</p>", "", text)

    # 이미지 태그 → 플레이스홀더 (추가/삭제/교체 감지용)
    def _img_placeholder(m):
        src = re.search(r'src="([^"]*)"', m.group(0))
        # src URL의 해시로 이미지 식별 (URL 자체는 노출 안 함)
        if src:
            import hashlib
            img_id = hashlib.md5(src.group(1).encode()).hexdigest()[:8]
            return f"[이미지:{img_id}]"
        return "[이미지]"
    text = re.sub(r"<img[^>]*>", _img_placeholder, text)

    # 나머지 HTML 태그 제거
    text = re.sub(r"<[^>]+>", "", text)

    # HTML 엔티티 디코드
    text = html_module.unescape(text)

    # 연속 빈 줄 정리
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── HTML 트리 기반 비교 ────────────────────────────────────────────────


def _html_to_nodes(html: str) -> list[dict]:
    """HTML을 플랫한 노드 리스트로 변환. 각 노드에 경로(path)와 텍스트(text)를 부여.
    Returns: [{"path": "섹션 > 표 이름 > 행 제목 > 열 제목", "text": "셀 내용"}, ...]
    """
    from bs4 import BeautifulSoup
    import hashlib

    soup = BeautifulSoup(html, "html.parser")
    nodes = []
    current_heading = ""

    for el in soup.children:
        _walk_element(el, current_heading, nodes)

    # 헤딩 트래킹을 위해 순차 처리
    result = []
    heading = ""
    for n in nodes:
        if n.get("_heading"):
            heading = n["_heading"]
            continue
        n["path"] = f"{heading} > {n['path']}" if heading and n["path"] else heading or n["path"]
        result.append(n)
    return result


def _walk_element(el, heading: str, nodes: list):
    """HTML 요소를 재귀적으로 순회하며 노드 리스트에 추가."""
    from bs4 import NavigableString, Tag
    import hashlib

    if isinstance(el, NavigableString):
        return
    if not isinstance(el, Tag):
        return

    # 헤딩
    if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        nodes.append({"_heading": el.get_text(strip=True)})
        return

    # 테이블
    if el.name == "table":
        rows = el.find_all("tr")
        if not rows:
            return
        # 헤더 행에서 열 이름 추출
        header_cells = rows[0].find_all(["th", "td"])
        col_names = [c.get_text(strip=True) for c in header_cells]

        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            row_name = cells[0].get_text(strip=True)[:30] if cells else ""
            for j, cell in enumerate(cells):
                col_name = col_names[j] if j < len(col_names) else f"열{j}"
                base_path = f"[표] {row_name} > {col_name}"
                # 셀 내부를 리스트 항목 단위로 분해
                cell_nodes = _extract_cell_nodes(cell, base_path)
                nodes.extend(cell_nodes)
        return

    # 리스트 아이템
    if el.name == "li":
        text = _cell_text_with_images(el)
        if text.strip():
            nodes.append({"path": "", "text": text.strip()})
        return

    # 일반 단락
    if el.name == "p":
        text = _cell_text_with_images(el)
        if text.strip():
            nodes.append({"path": "", "text": text.strip()})
        return

    # 나머지 → 자식 순회
    for child in el.children:
        _walk_element(child, heading, nodes)


def _cell_text_with_images(el) -> str:
    """셀/요소 내부 텍스트를 추출하되, <img>는 [이미지:해시]로 변환."""
    import hashlib
    parts = []
    for child in el.descendants:
        from bs4 import NavigableString, Tag
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag) and child.name == "img":
            src = child.get("src", "")
            img_id = hashlib.md5(src.encode()).hexdigest()[:8] if src else "unknown"
            parts.append(f"[이미지:{img_id}]")
    return " ".join("".join(parts).split())


def _extract_cell_nodes(cell, base_path: str) -> list[dict]:
    """테이블 셀 내부를 리스트 항목 단위로 분해. 리스트가 없으면 셀 전체를 하나의 노드로."""
    from bs4 import Tag

    # 셀에 리스트가 있는지 확인
    has_list = cell.find(["ul", "ol"])
    if not has_list:
        text = _cell_text_with_images(cell)
        if text.strip():
            return [{"path": base_path, "text": text.strip()}]
        return []

    nodes = []
    current_section = ""

    for child in cell.children:
        if not isinstance(child, Tag):
            continue
        if child.name in ("p", "h3", "h4", "h5"):
            # 섹션 헤더 (셀 내 <p><strong>유형 1. OX 퀴즈</strong></p> 등)
            section_text = child.get_text(strip=True)
            if section_text:
                current_section = section_text
        elif child.name in ("ul", "ol"):
            section_path = f"{base_path} > {current_section}" if current_section else base_path
            _walk_list_items(child, section_path, nodes)

    # 노드가 없으면 셀 전체를 하나로 (폴백)
    if not nodes:
        text = _cell_text_with_images(cell)
        if text.strip():
            return [{"path": base_path, "text": text.strip()}]
    return nodes


def _walk_list_items(list_el, parent_path: str, nodes: list):
    """리스트 요소를 재귀적으로 순회하여 개별 노드 추출."""
    from bs4 import Tag

    for li in list_el.find_all("li", recursive=False):
        direct_text = _get_li_direct_text(li)
        sub_lists = li.find_all(["ul", "ol"], recursive=False)

        if sub_lists:
            # 하위 리스트가 있으면 현재 항목은 경로 segment
            item_name = _path_name(direct_text)
            item_path = f"{parent_path} > {item_name}" if parent_path and item_name else parent_path or item_name

            # 현재 항목의 직접 텍스트도 노드로 추가
            if direct_text.strip():
                nodes.append({"path": item_path, "text": direct_text.strip()})

            # 하위 리스트 재귀
            for sub in sub_lists:
                _walk_list_items(sub, item_path, nodes)
        else:
            # 말단 항목 → 전체 텍스트를 노드로
            full_text = _cell_text_with_images(li)
            if full_text.strip():
                nodes.append({"path": parent_path, "text": full_text.strip()})


def _get_li_direct_text(li) -> str:
    """<li> 요소의 직접 텍스트만 추출 (중첩 리스트/figure 제외)."""
    from bs4 import NavigableString, Tag
    parts = []
    for child in li.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag) and child.name not in ("ul", "ol", "figure"):
            parts.append(child.get_text())
    return " ".join("".join(parts).split())


def _path_name(text: str, max_len: int = 25) -> str:
    """텍스트에서 경로용 이름을 추출 (콜론 앞 또는 첫 max_len자)."""
    if not text:
        return ""
    for sep in (":", "—", "："):
        if sep in text:
            name = text.split(sep)[0].strip()
            if name:
                return name[:max_len]
    return text[:max_len].strip()


def _compare_node_lists(old_nodes: list[dict], new_nodes: list[dict]) -> list[dict]:
    """두 노드 리스트를 경로+텍스트 기준으로 비교.
    Returns: [{"type": "modified"|"added"|"removed", "path": str, "old": str, "new": str}, ...]
    """
    # 경로별로 그룹핑 (같은 경로가 여러 개일 수 있으므로 순서 유지)
    old_by_path: dict[str, list[str]] = {}
    new_by_path: dict[str, list[str]] = {}

    for n in old_nodes:
        old_by_path.setdefault(n["path"], []).append(n["text"])
    for n in new_nodes:
        new_by_path.setdefault(n["path"], []).append(n["text"])

    changes = []
    all_paths = list(dict.fromkeys(list(old_by_path.keys()) + list(new_by_path.keys())))

    for path in all_paths:
        old_texts = old_by_path.get(path, [])
        new_texts = new_by_path.get(path, [])

        # SequenceMatcher로 최적 매칭 (삽입/삭제 시 오정렬 방지)
        from difflib import SequenceMatcher
        sm = SequenceMatcher(None, old_texts, new_texts)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            elif op == "replace":
                # 1:1 매칭되는 부분은 수정, 나머지는 추가/삭제
                pairs = min(i2 - i1, j2 - j1)
                for k in range(pairs):
                    changes.append({"type": "modified", "path": path, "old": old_texts[i1 + k], "new": new_texts[j1 + k]})
                for i in range(i1 + pairs, i2):
                    changes.append({"type": "removed", "path": path, "old": old_texts[i], "new": ""})
                for j in range(j1 + pairs, j2):
                    changes.append({"type": "added", "path": path, "old": "", "new": new_texts[j]})
            elif op == "delete":
                for i in range(i1, i2):
                    changes.append({"type": "removed", "path": path, "old": old_texts[i], "new": ""})
            elif op == "insert":
                for j in range(j1, j2):
                    changes.append({"type": "added", "path": path, "old": "", "new": new_texts[j]})

    # 후처리: path 이동으로 인한 거짓 삭제+추가 제거
    changes = _reconcile_path_moves(changes)

    return changes


def _get_section_root(path: str) -> str:
    """path에서 상위 2 depth를 추출. 예: 'A > B > C > D' → 'A > B'"""
    parts = [p.strip() for p in path.split(">")]
    return " > ".join(parts[:2]) if len(parts) >= 2 else path


def _reconcile_path_moves(changes: list[dict]) -> list[dict]:
    """같은 섹션 내에서 텍스트가 동일한 removed+added 쌍을 제거한다.
    리스트 항목에 하위 항목이 추가되면 path depth가 바뀌어
    동일 텍스트가 삭제+추가로 잡히는 문제를 해결한다.
    """
    from difflib import SequenceMatcher

    removed = [(i, c) for i, c in enumerate(changes) if c["type"] == "removed"]
    added = [(i, c) for i, c in enumerate(changes) if c["type"] == "added"]

    remove_indices = set()

    for ri, rc in removed:
        if ri in remove_indices:
            continue
        for ai, ac in added:
            if ai in remove_indices:
                continue
            # 텍스트 동일 + 같은 상위 섹션 → 양쪽 제거 (path 이동일 뿐)
            if rc["old"] == ac["new"] and _get_section_root(rc["path"]) == _get_section_root(ac["path"]):
                remove_indices.add(ri)
                remove_indices.add(ai)
                break
            # 텍스트 유사도 0.8 이상 + 같은 상위 섹션 → modified로 전환
            ratio = SequenceMatcher(None, rc["old"], ac["new"]).ratio()
            if ratio >= 0.8 and _get_section_root(rc["path"]) == _get_section_root(ac["path"]):
                changes[ri] = {"type": "modified", "path": ac["path"], "old": rc["old"], "new": ac["new"]}
                remove_indices.add(ai)
                break

    return [c for i, c in enumerate(changes) if i not in remove_indices]


MAX_SUMMARY_ITEMS = 5


def _format_tree_changes(changes: list[dict]) -> str:
    """트리 비교 결과를 전체 포맷팅 (스레드 상세용)."""
    return _format_changes_list(changes, max_items=None)


def _format_tree_changes_summary(changes: list[dict]) -> str:
    """트리 비교 결과를 요약 포맷팅 (메인 메시지용, 각 유형별 MAX_SUMMARY_ITEMS건)."""
    return _format_changes_list(changes, max_items=MAX_SUMMARY_ITEMS)


def _format_changes_list(changes: list[dict], max_items: int | None = None) -> str:
    """트리 비교 결과를 mrkdwn 텍스트로 포맷팅 (메인 메시지용)."""
    modified = [c for c in changes if c["type"] == "modified"]
    added = [c for c in changes if c["type"] == "added"]
    removed = [c for c in changes if c["type"] == "removed"]

    lines = []
    if modified:
        lines.append(f"\u2022 *수정* ({len(modified)}건)")
        show = modified[:max_items] if max_items else modified
        prev_path = None
        for c in show:
            path = c["path"] or "(본문)"
            old, new = _truncate_diff_pair(c["old"], c["new"])
            if path != prev_path:
                lines.append(f"  \u2022 {path}")
                prev_path = path
            lines.append(f">       변경 전: _{old}_")
            lines.append(f">       변경 후: *{new}*")
        if max_items and len(modified) > max_items:
            lines.append(f"  _... 외 {len(modified) - max_items}건_")

    if added:
        lines.append(f"\u2022 *추가* ({len(added)}건)")
        show = added[:max_items] if max_items else added
        lines.extend(_format_grouped_items(show, "added"))
        if max_items and len(added) > max_items:
            lines.append(f"  _... 외 {len(added) - max_items}건_")

    if removed:
        lines.append(f"\u2022 *삭제* ({len(removed)}건)")
        show = removed[:max_items] if max_items else removed
        lines.extend(_format_grouped_items(show, "removed"))
        if max_items and len(removed) > max_items:
            lines.append(f"  _... 외 {len(removed) - max_items}건_")

    if max_items:
        total = len(modified) + len(added) + len(removed)
        shown = min(len(modified), max_items) + min(len(added), max_items) + min(len(removed), max_items)
        if shown < total:
            lines.append(f"_상세 내용은 스레드를 확인해주세요._")

    return "\n".join(lines)


def _format_grouped_items(items: list[dict], change_type: str) -> list[str]:
    """같은 경로의 항목들을 그룹핑하여 경로 1회만 표시, 하위에 내용 나열."""
    lines = []
    prev_path = None
    for c in items:
        path = c["path"] or "(본문)"
        text = c.get("new", "") if change_type == "added" else c.get("old", "")
        if path != prev_path:
            lines.append(f"  \u2022 {path}")
            prev_path = path
        if change_type == "removed":
            lines.append(f"     \u25E6 ~{_truncate(text)}~")
        else:
            lines.append(f"     \u25E6 {_truncate(text)}")
    return lines


def format_changes_rich_text(changes: list[dict]) -> list[dict]:
    """트리 비교 결과를 Slack rich_text 블록 리스트로 변환 (스레드 상세용)."""
    modified = [c for c in changes if c["type"] == "modified"]
    added = [c for c in changes if c["type"] == "added"]
    removed = [c for c in changes if c["type"] == "removed"]

    elements = []

    for label, items, style_fn in [
        ("수정", modified, None),
        ("추가", added, None),
        ("삭제", removed, {"strike": True}),
    ]:
        if not items:
            continue
        # 섹션 헤더
        elements.append({
            "type": "rich_text_section",
            "elements": [{"type": "text", "text": f"{label} ({len(items)}건)", "style": {"bold": True}}],
        })
        # 경로별 그룹핑
        grouped = _group_by_path(items)
        for path, group_items in grouped:
            # 경로 (indent 0)
            elements.append({
                "type": "rich_text_list",
                "style": "bullet",
                "indent": 0,
                "elements": [{
                    "type": "rich_text_section",
                    "elements": [{"type": "text", "text": path, "style": {"bold": True}}],
                }],
            })
            # 하위 항목 (indent 1)
            sub_elements = []
            for c in group_items:
                if label == "수정":
                    old, new = _truncate_diff_pair(c["old"], c["new"])
                    sub_elements.append({
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "변경 전: "},
                            {"type": "text", "text": old, "style": {"italic": True}},
                            {"type": "text", "text": "\n변경 후: "},
                            {"type": "text", "text": new, "style": {"bold": True}},
                        ],
                    })
                elif label == "삭제":
                    sub_elements.append({
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": _truncate(c["old"]), "style": {"strike": True}}],
                    })
                else:
                    sub_elements.append({
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": _truncate(c["new"])}],
                    })
            if sub_elements:
                elements.append({
                    "type": "rich_text_list",
                    "style": "bullet",
                    "indent": 1,
                    "elements": sub_elements,
                })

    if not elements:
        return []
    return [{"type": "rich_text", "elements": elements}]


def _group_by_path(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """아이템을 경로별로 그룹핑 (순서 유지)."""
    groups = []
    prev_path = None
    current_group = []
    for c in items:
        path = c.get("path") or "(본문)"
        if path != prev_path:
            if current_group:
                groups.append((prev_path, current_group))
            current_group = [c]
            prev_path = path
        else:
            current_group.append(c)
    if current_group:
        groups.append((prev_path, current_group))
    return groups


# ── PRD Description 변경 감지 ─────────────────────────────────────────


def _snapshot_path_prd(qa_card_id: str) -> Path:
    return SNAPSHOT_DIR / f"prd_{qa_card_id}.txt"


def check_description_change(qa_card: dict) -> dict | None:
    """QA카드에 연결된 PRD의 description을 이전 스냅샷과 비교.

    PRD 소스:
      - Linear 이슈 → description 필드
      - Confluence 페이지 → 본문 (HTML → 텍스트 변환)

    Returns: {"card_id": str, "prd_id": str, "title": str, "diff_text": str, "card_url": str} | None
    """
    card_id = qa_card["identifier"]
    prd_source = _find_prd_source(qa_card)
    if not prd_source:
        return None

    # PRD 본문 조회
    if prd_source["type"] == "linear":
        from src.linear_client import LinearClient
        client = LinearClient()
        prd_issue = client.get_issue_by_identifier(prd_source["identifier"])
        if not prd_issue:
            print(f"      PRD 이슈 조회 실패: {prd_source['identifier']}")
            return None
        description = prd_issue.get("description") or ""
        prd_title = prd_issue.get("title") or prd_source["identifier"]
        prd_id = prd_source["identifier"]
        prd_url = f"https://linear.app/buzzvil/issue/{prd_id}"

    elif prd_source["type"] == "confluence":
        page = _fetch_confluence_page(prd_source["page_id"])
        if not page:
            return None
        description = page["body"]
        prd_html = page.get("html", "")
        prd_title = page["title"]
        current_version = page.get("version", 0)
        prd_id = f"Confluence #{prd_source['page_id']}"
        prd_url = prd_source["url"]

        # Confluence → 트리 기반 비교 (HTML 스냅샷 사용)
        snap_html_path = SNAPSHOT_DIR / f"prd_{card_id}.html"
        snap_ver_path = SNAPSHOT_DIR / f"prd_{card_id}.version"
        SNAPSHOT_DIR.mkdir(exist_ok=True)

        # 이전 버전 번호 읽기
        old_version = 0
        if snap_ver_path.exists():
            try:
                old_version = int(snap_ver_path.read_text(encoding="utf-8").strip())
            except ValueError:
                old_version = 0

        if not snap_html_path.exists():
            snap_html_path.write_text(prd_html, encoding="utf-8")
            snap_ver_path.write_text(str(current_version), encoding="utf-8")
            # 텍스트 스냅샷도 저장 (하위 호환)
            snap_path = _snapshot_path_prd(card_id)
            snap_path.write_text(description, encoding="utf-8")
            print(f"      PRD 초기 스냅샷 저장 (트리, v{current_version}): {prd_id}")
            return None

        old_html = snap_html_path.read_text(encoding="utf-8")
        if old_html == prd_html:
            return None

        # 트리 기반 비교
        old_nodes = _html_to_nodes(old_html)
        new_nodes = _html_to_nodes(prd_html)
        changes = _compare_node_lists(old_nodes, new_nodes)

        if not changes:
            snap_html_path.write_text(prd_html, encoding="utf-8")
            snap_ver_path.write_text(str(current_version), encoding="utf-8")
            snap_path = _snapshot_path_prd(card_id)
            snap_path.write_text(description, encoding="utf-8")
            return None

        snap_html_path.write_text(prd_html, encoding="utf-8")
        snap_ver_path.write_text(str(current_version), encoding="utf-8")
        snap_path = _snapshot_path_prd(card_id)
        snap_path.write_text(description, encoding="utf-8")
        diff_text = _format_tree_changes(changes)
        diff_summary = _format_tree_changes_summary(changes)

        # 버전 비교 정보 구성
        version_info = None
        if old_version and current_version:
            version_info = f"v{old_version} → v{current_version}"

        return {
            "card_id": card_id,
            "prd_id": prd_id,
            "title": prd_title,
            "diff_text": diff_text,
            "diff_summary": diff_summary,
            "changes": changes,
            "card_url": prd_url,
            "version_info": version_info,
        }

    else:
        return None

    # Linear PRD — 기존 텍스트 기반 비교
    snap_path = _snapshot_path_prd(card_id)
    SNAPSHOT_DIR.mkdir(exist_ok=True)

    if not snap_path.exists():
        snap_path.write_text(description, encoding="utf-8")
        print(f"      PRD 초기 스냅샷 저장: {prd_id}")
        return None

    old_desc = snap_path.read_text(encoding="utf-8")
    if old_desc == description:
        return None

    # diff 생성
    old_lines = old_desc.splitlines()
    new_lines = description.splitlines()

    changes = _parse_readable_diff(old_lines, new_lines)
    if not changes:
        snap_path.write_text(description, encoding="utf-8")
        return None

    snap_path.write_text(description, encoding="utf-8")
    diff_text = _format_changes(changes)

    return {
        "card_id": card_id,
        "prd_id": prd_id,
        "title": prd_title,
        "diff_text": diff_text,
        "card_url": prd_url,
    }


def _strip_markdown(text: str) -> str:
    """Markdown 특수문자 제거 (\\, *, _, #, | 등)"""
    text = text.replace("\\", "")
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)  # **bold**
    text = text.replace("*", "").replace("_", "")
    # 헤딩 마크 제거 (### 공통 사항 → 공통 사항)
    text = re.sub(r"#{1,6}\s*", "", text)
    # 테이블 파이프 제거
    text = text.replace("||", "").replace("|", "")
    text = text.strip()
    return text


def _find_heading_path(lines: list[str], line_idx: int) -> str:
    """line_idx 위의 Markdown 헤딩 경로를 찾는다. (계층 구조)"""
    headings = {}  # level → text
    for i in range(line_idx, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            text = _strip_markdown(stripped.lstrip("#").strip())
            if level not in headings:
                headings[level] = text
            if level <= min(headings.keys(), default=99):
                break
    if not headings:
        return ""
    return " > ".join(headings[k] for k in sorted(headings.keys()))


def _parse_table_row(line: str) -> tuple[str, str] | None:
    """테이블 행에서 (라벨, 값) 추출.
    Linear: '| 제목 | 두두두둥 |' → ('제목', '두두두둥')
    Confluence: '[표] 제목 | 두두두둥' → ('제목', '두두두둥')
    """
    stripped = line.strip()
    # Confluence [표] 형식
    if stripped.startswith("[표]"):
        parts = stripped[3:].split("|")
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) >= 2:
            return (_strip_markdown(parts[0]), _strip_markdown(parts[1]))
        return None
    # Linear | ... | 형식
    if not stripped.startswith("|"):
        return None
    cells = [_strip_markdown(c) for c in line.split("|")]
    cells = [c for c in cells if c]
    if len(cells) < 2:
        return None
    if cells[0].startswith("--"):
        return None
    return (cells[0], cells[1])


def _clean_line(line: str) -> str:
    """Markdown 문법을 정리하여 핵심 텍스트만 추출."""
    text = line.strip().lstrip("*-").strip()
    if not text or text.startswith("| --") or text == "|":
        return ""
    # 헤딩 라인 자체는 섹션 경로로 쓰므로 내용에서 제외
    if text.startswith("#"):
        return ""
    # [표] 행은 테이블 파싱에서 처리하므로 strip_markdown은 적용하되 | 보존
    if text.startswith("[표]"):
        return text  # _extract_change_detail에서 _parse_table_row로 처리
    return _strip_markdown(text)


def _extract_change_detail(
    old_line: str, new_line: str, lines_for_context: list[str], line_idx: int,
) -> dict | None:
    """변경된 한 쌍의 라인에서 읽기 좋은 변경 설명을 추출."""
    section = _find_heading_path(lines_for_context, line_idx)

    # 테이블 행인 경우: 라벨 + 값 비교
    old_table = _parse_table_row(old_line)
    new_table = _parse_table_row(new_line)
    if old_table and new_table and old_table[0] == new_table[0]:
        return {
            "section": section,
            "is_table": True,
            "label": old_table[0],
            "old": old_table[1],
            "new": new_table[1],
        }

    # 일반 텍스트
    old_clean = _clean_line(old_line)
    new_clean = _clean_line(new_line)
    if old_clean and new_clean:
        return {
            "section": section,
            "is_table": bool(_parse_table_row(old_line)),
            "label": "",
            "old": old_clean,
            "new": new_clean,
        }

    return None


def _parse_readable_diff(old_lines: list[str], new_lines: list[str]) -> list[dict]:
    """unified diff를 파싱하여 읽기 좋은 변경 목록을 생성.

    Returns: [{"type": "modified"|"added"|"removed", "section": str, "detail": str}, ...]
    """
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines, lineterm="",
    ))
    if not diff_lines:
        return []

    changes = []
    old_idx = 0
    new_idx = 0
    pending_removed = []  # (old_idx, raw_line)

    for line in diff_lines:
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            # 남은 pending 처리
            for rm_idx, rm_raw in pending_removed:
                clean = _clean_line(rm_raw)
                if clean:
                    section = _find_heading_path(old_lines, rm_idx)
                    changes.append({"type": "removed", "section": section, "detail": clean})
            pending_removed = []
            m = re.match(r"@@ -(\d+)", line)
            if m:
                old_idx = int(m.group(1)) - 1
            m2 = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
            if m2:
                new_idx = int(m2.group(1)) - 1
            continue

        if line.startswith("-"):
            raw = line[1:]
            clean = _clean_line(raw)
            if clean:
                pending_removed.append((old_idx, raw))
            old_idx += 1
        elif line.startswith("+"):
            raw = line[1:]
            clean = _clean_line(raw)
            if clean:
                if pending_removed:
                    rm_idx, rm_raw = pending_removed.pop(0)
                    detail = _extract_change_detail(rm_raw, raw, old_lines, rm_idx)
                    if detail:
                        section = detail["section"]
                        label = detail["label"]
                        changes.append({"type": "modified", "section": section, "label": label, "old": detail["old"], "new": detail["new"], "is_table": detail.get("is_table", False)})
                    else:
                        section = _find_heading_path(old_lines, rm_idx)
                        is_table = bool(_parse_table_row(rm_raw))
                        changes.append({"type": "modified", "section": section, "label": "", "old": _clean_line(rm_raw), "new": clean, "is_table": is_table})
                else:
                    section = _find_heading_path(new_lines, new_idx)
                    is_table = bool(_parse_table_row(raw))
                    changes.append({"type": "added", "section": section, "detail": clean, "is_table": is_table})
            new_idx += 1
        else:
            for rm_idx, rm_raw in pending_removed:
                clean = _clean_line(rm_raw)
                if clean:
                    section = _find_heading_path(old_lines, rm_idx)
                    is_table = bool(_parse_table_row(rm_raw))
                    changes.append({"type": "removed", "section": section, "detail": clean, "is_table": is_table})
            pending_removed = []
            old_idx += 1
            new_idx += 1

    # 남은 pending 처리
    for rm_idx, rm_raw in pending_removed:
        clean = _clean_line(rm_raw)
        if clean:
            section = _find_heading_path(old_lines, rm_idx)
            is_table = bool(_parse_table_row(rm_raw))
            changes.append({"type": "removed", "section": section, "detail": clean, "is_table": is_table})

    return changes


MAX_DETAIL_LEN = 40


def _truncate(text: str, max_len: int = MAX_DETAIL_LEN) -> str:
    """텍스트를 max_len자로 자르되, 앞뒤를 살리고 가운데를 생략."""
    if len(text) <= max_len:
        return text
    # 앞쪽 60%, 뒤쪽 40% 비율로 분배
    ellipsis = "...(생략)..."
    keep_front = int(max_len * 0.6)
    keep_back = max_len - keep_front
    return text[:keep_front] + ellipsis + text[-keep_back:]


def _truncate_diff_pair(old: str, new: str, max_len: int = MAX_DETAIL_LEN) -> tuple[str, str]:
    """변경 전후 텍스트에서 실제 바뀐 부분 주변을 표시."""
    if len(old) <= max_len and len(new) <= max_len:
        return old, new

    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, old, new)
    first_diff_pos = None
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            first_diff_pos = min(i1, j1)
            break

    if first_diff_pos is None:
        return _truncate(old, max_len), _truncate(new, max_len)

    # 변경 위치를 중심으로 앞뒤 컨텍스트 포함
    context_before = 10
    start = max(0, first_diff_pos - context_before)

    prefix = "..." if start > 0 else ""
    old_slice = old[start:start + max_len]
    new_slice = new[start:start + max_len]
    old_suffix = "..." if start + max_len < len(old) else ""
    new_suffix = "..." if start + max_len < len(new) else ""

    return f"{prefix}{old_slice}{old_suffix}", f"{prefix}{new_slice}{new_suffix}"


def _format_detail(c: dict) -> str:
    """변경 항목 하나를 포맷팅."""
    is_table = c.get("is_table", False)
    table_tag = "[표] " if is_table else ""
    change_type = c.get("type", "")

    if change_type == "modified":
        label = c.get("label", "")
        old, new = _truncate_diff_pair(c.get("old", ""), c.get("new", ""))
        header = label if label else f"{table_tag}수정"
        return f"{header}\n>             변경 전: _{old}_\n>             변경 후: *{new}*"
    else:
        # 추가/삭제
        detail = c.get("detail", "")
        return f"{table_tag}{_truncate(detail)}"


def _format_grouped(items: list[dict]) -> list[str]:
    """같은 섹션의 항목들을 그룹핑하여 포맷팅.
    같은 섹션이 연속되면 섹션명 한 번만 표시, 하위에 detail 나열.
    """
    lines = []
    prev_section = None

    for c in items:
        section = c.get("section", "")
        formatted = _format_detail(c)

        if section and section == prev_section:
            lines.append(f"     \u25E6 {formatted}")
        elif section:
            lines.append(f"  \u2022 {section}")
            lines.append(f"     \u25E6 {formatted}")
            prev_section = section
        else:
            lines.append(f"  \u25E6 {formatted}")
            prev_section = None

    return lines


def _format_changes(changes: list[dict]) -> str:
    """변경 목록을 수정/추가/삭제 그룹별로 포맷팅."""
    modified = [c for c in changes if c["type"] == "modified"]
    added = [c for c in changes if c["type"] == "added"]
    removed = [c for c in changes if c["type"] == "removed"]

    lines = []
    if modified:
        lines.append(f"\u2022 *수정* ({len(modified)}건)")
        lines.extend(_format_grouped(modified))

    if added:
        lines.append(f"\u2022 *추가* ({len(added)}건)")
        lines.extend(_format_grouped(added))

    if removed:
        lines.append(f"\u2022 *삭제* ({len(removed)}건)")
        lines.extend(_format_grouped(removed))

    return "\n".join(lines)


# ── Figma 디자인 변경 감지 ───────────────────────────────────────────────


def parse_figma_urls(qa_card: dict) -> list[dict]:
    """QA카드 Attachments에서 Figma URL을 파싱하여 모니터링 대상 목록 반환.

    Returns: [{"file_key": str, "node_id": str, "url": str}, ...]
    """
    attachments = qa_card.get("attachments", {}).get("nodes", [])
    targets = []
    seen = set()
    for att in attachments:
        url = att.get("url") or ""
        if "figma.com" not in url:
            continue
        m = _FIGMA_URL_RE.search(url)
        if m:
            file_key = m.group(1)
            node_id = m.group(2).replace("-", ":")
            key = (file_key, node_id)
            if key not in seen:
                seen.add(key)
                targets.append({
                    "file_key": file_key,
                    "node_id": node_id,
                    "url": url,
                })
    return targets


def _fetch_figma_nodes(file_key: str, node_ids: list[str]) -> dict:
    """Figma REST API로 노드 트리 조회 (429 시 최대 3회 retry)"""
    import time

    token = os.environ.get("FIGMA_TOKEN", "")
    if not token:
        raise RuntimeError("FIGMA_TOKEN 환경변수가 설정되지 않았습니다")

    ids = ",".join(nid.replace(":", "-") for nid in node_ids)
    url = f"https://api.figma.com/v1/files/{file_key}/nodes?ids={ids}"
    headers = {"X-Figma-Token": token}

    last_resp = None
    for attempt in range(3):
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 429:
            last_resp = resp
            rate_type = resp.headers.get("X-Figma-Rate-Limit-Type", "unknown")
            retry_after = int(resp.headers.get("Retry-After", 30))
            # 월 한도(Collab/View seat)는 retry해도 소용 없음
            if retry_after > 3600:
                raise RuntimeError(
                    f"Figma API 월 한도 초과 (seat={rate_type}, Retry-After={retry_after}s) "
                    f"→ Dev/Full seat 업그레이드 필요"
                )
            wait = min(retry_after, 60)
            print(f"      Figma API rate limit (seat={rate_type}) — {wait}초 대기 후 재시도 ({attempt + 1}/3)")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()

    rate_type = last_resp.headers.get("X-Figma-Rate-Limit-Type", "unknown") if last_resp else "unknown"
    raise RuntimeError(
        f"Figma API rate limit 초과 (seat={rate_type}, 3회 재시도 실패, file={file_key})"
    )


def _extract_node_props(node: dict) -> dict:
    """비교 대상 속성 추출"""
    return {
        # 기본 속성
        "name": node.get("name", ""),
        "type": node.get("type", ""),
        "bbox": node.get("absoluteBoundingBox"),
        "text": node.get("characters"),
        "child_count": len(node.get("children", [])),
        # 확장 속성
        "fills": node.get("fills"),
        "strokes": node.get("strokes"),
        "strokeWeight": node.get("strokeWeight"),
        "cornerRadius": node.get("cornerRadius"),
        "opacity": node.get("opacity"),
        "visible": node.get("visible", True),
        "rotation": node.get("rotation"),
        "effects": node.get("effects"),
        "style": node.get("style"),
        "layoutMode": node.get("layoutMode"),
        "itemSpacing": node.get("itemSpacing"),
        "paddingTop": node.get("paddingTop"),
        "paddingRight": node.get("paddingRight"),
        "paddingBottom": node.get("paddingBottom"),
        "paddingLeft": node.get("paddingLeft"),
    }


def _flatten_nodes(node: dict, parent_name: str = "", depth: int = 0) -> dict:
    """노드 트리를 depth 제한하여 flat dict로 변환"""
    result = {}
    props = _extract_node_props(node)
    props["parent"] = parent_name
    props["depth"] = depth
    result[node["id"]] = props

    if depth < MAX_DEPTH:
        for child in node.get("children", []):
            result.update(_flatten_nodes(
                child, node.get("name", ""), depth + 1,
            ))
    return result


def _compare_snapshots(old_nodes: dict, new_nodes: dict) -> list[dict]:
    """두 스냅샷 비교 후 변경 사항 리스트 반환"""
    changes = []
    old_ids = set(old_nodes.keys())
    new_ids = set(new_nodes.keys())

    # 추가된 노드
    for nid in sorted(new_ids - old_ids):
        n = new_nodes[nid]
        changes.append({
            "type": "added",
            "name": n["name"],
            "detail": f'"{n["parent"]}" 하위에 추가됨',
        })

    # 삭제된 노드
    for nid in sorted(old_ids - new_ids):
        n = old_nodes[nid]
        changes.append({
            "type": "removed",
            "name": n["name"],
            "detail": "삭제됨",
        })

    # 변경된 노드
    for nid in sorted(old_ids & new_ids):
        old, new = old_nodes[nid], new_nodes[nid]
        diffs = []

        if old["name"] != new["name"]:
            diffs.append(f'이름: "{old["name"]}" -> "{new["name"]}"')
        if old["bbox"] != new["bbox"] and old["bbox"] and new["bbox"]:
            ob, nb = old["bbox"], new["bbox"]
            diffs.append(f'크기: {ob["width"]}x{ob["height"]} -> {nb["width"]}x{nb["height"]}')
        if old.get("text") != new.get("text") and old.get("text") is not None:
            diffs.append(f'텍스트: "{old["text"]}" -> "{new["text"]}"')
        if old.get("child_count") != new.get("child_count"):
            diffs.append(f'하위 노드: {old["child_count"]}개 -> {new["child_count"]}개')
        if old.get("visible") != new.get("visible"):
            state = "숨김" if not new["visible"] else "표시"
            diffs.append(f'가시성: {state} 처리됨')

        # 확장 속성 (변경 여부만)
        for prop in ["fills", "strokes", "effects", "style"]:
            if old.get(prop) != new.get(prop) and old.get(prop) is not None:
                diffs.append(f'{prop} 변경됨')
        for prop in ["cornerRadius", "opacity", "rotation", "strokeWeight",
                      "layoutMode", "itemSpacing",
                      "paddingTop", "paddingRight", "paddingBottom", "paddingLeft"]:
            if old.get(prop) != new.get(prop) and old.get(prop) is not None:
                diffs.append(f'{prop}: {old[prop]} -> {new[prop]}')

        if diffs:
            changes.append({
                "type": "modified",
                "name": new["name"],
                "detail": ", ".join(diffs),
            })

    return changes


def _snapshot_path_figma(file_key: str, node_id: str) -> Path:
    return SNAPSHOT_DIR / f"figma_{file_key}_{node_id.replace(':', '-')}.json"


def check_figma_changes(figma_targets: list[dict], card_id: str) -> list[dict]:
    """Figma 노드 트리를 조회하고 이전 스냅샷과 비교.

    Returns: [{"file_key": str, "node_id": str, "url": str, "changes": list}, ...]
    """
    if not figma_targets:
        return []

    SNAPSHOT_DIR.mkdir(exist_ok=True)
    results = []

    # file_key별로 그룹핑하여 API 호출 최소화
    by_file: dict[str, list[dict]] = {}
    for t in figma_targets:
        by_file.setdefault(t["file_key"], []).append(t)

    import time as _time
    for idx, (file_key, targets) in enumerate(by_file.items()):
        if idx > 0:
            _time.sleep(2)  # 파일 간 2초 간격으로 rate limit 회피
        node_ids = [t["node_id"] for t in targets]
        try:
            api_data = _fetch_figma_nodes(file_key, node_ids)
        except Exception as e:
            print(f"      Figma API 오류 ({file_key}): {e}")
            raise

        nodes_data = api_data.get("nodes", {})
        for t in targets:
            node_id = t["node_id"]
            # Figma API 응답에서 node_id 키 형식 대응 (: 또는 -)
            node_doc = None
            for key_variant in [node_id, node_id.replace(":", "-")]:
                if key_variant in nodes_data:
                    node_doc = nodes_data[key_variant].get("document")
                    break

            if not node_doc:
                print(f"      Figma 노드 미발견: {node_id}")
                continue

            new_flat = _flatten_nodes(node_doc)
            snap_path = _snapshot_path_figma(file_key, node_id)

            if snap_path.exists():
                old_flat = json.loads(snap_path.read_text(encoding="utf-8"))
                changes = _compare_snapshots(old_flat, new_flat)
                if changes:
                    results.append({
                        "file_key": file_key,
                        "node_id": node_id,
                        "url": t["url"],
                        "changes": changes,
                    })
            # 스냅샷 업데이트
            snap_path.write_text(
                json.dumps(new_flat, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    return results


# ── 통합 실행 ────────────────────────────────────────────────────────────


def should_watch(test_phases: dict) -> bool:
    """변경 감시 대상인지 판단.
    - 테스트 전 → 감시 (기능테스트 일정 미정이면 스킵)
    - 통합/리그레션 기간 내 → 감시
    - 리그레션 있으면 리그레션 종료일까지, 없으면 통합 종료일까지
    """
    from datetime import datetime, timezone, timedelta
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).strftime("%Y-%m-%d")

    current_phase = test_phases.get("current_phase", "")
    integration = test_phases.get("integration")
    regression = test_phases.get("regression")

    # 테스트 전 → 감시 (기능테스트 일정 미정이면 스킵)
    if current_phase == "테스트 전":
        return bool(integration)

    # 종료일 결정: 리그레션 있으면 리그레션 종료일, 없으면 통합 종료일
    end_date = None
    if regression:
        end_date = regression["end"]
    elif integration:
        end_date = integration["end"]

    if not end_date:
        return False

    # 오늘이 종료일 이내면 감시
    return today <= end_date


_PRD_KEYWORDS = ["prd"]


def _has_no_prd_note(qa_card: dict) -> bool:
    """Description 특이사항에 'PRD 없음' 문구가 있는지 확인."""
    description = qa_card.get("description") or ""
    for line in description.splitlines():
        stripped = line.strip().lstrip("*").strip()
        if "prd 없음" in stripped.lower():
            return True
    return False


def _extract_requirement_link(card: dict) -> dict | None:
    """카드의 Attachments/Description에서 요구사항(PRD) 스펙 링크를 찾는다.

    요구사항으로 인정하는 기준:
      - Attachments에 Confluence 페이지 링크가 있으면 (제목 무관 — 보통 자동 첨부됨)
      - Attachments에 title이 "PRD"인 Linear 이슈 링크가 있으면
      - Description 본문에 Confluence 페이지 링크가 있으면

    Returns: {"url": str, "title": str} | None
    """
    if not card:
        return None

    attachments = (card.get("attachments") or {}).get("nodes", [])
    for att in attachments:
        url = att.get("url") or ""
        title = (att.get("title") or "").strip()
        # Confluence 페이지 → 제목과 무관하게 요구사항으로 인정
        if _CONFLUENCE_PAGE_RE.search(url):
            return {"url": url, "title": title or "PRD"}
        # title에 "prd"가 명시된 Linear 이슈 링크
        if any(kw in title.lower() for kw in _PRD_KEYWORDS) and _LINEAR_ISSUE_RE.search(url):
            return {"url": url, "title": title or "PRD"}

    # Description 본문 내 Confluence 링크
    description = card.get("description") or ""
    m = _CONFLUENCE_URL_RE.search(description)
    if m:
        return {"url": m.group(0), "title": "PRD"}

    return None


def check_missing_links(qa_card: dict) -> dict:
    """QA카드에서 요구사항(PRD) 스펙 링크를 단계적으로 탐색한다.

    탐색 순서:
      1) QA카드 자체 Attachments/Description에 요구사항 링크가 있으면 → 정상
      2) Description 특이사항에 'PRD 없음'이 명시되어 있으면 → 의도적 미첨부 (정상)
      3) 상위(parent) 카드가 있으면 → QA카드에 자동 첨부 대상
         - 상위 카드 안에 PRD 링크가 있으면 → 그 링크
         - 없으면 → 상위 카드 자체를 요구사항으로 보고 상위 카드 URL
      4) 상위 카드도 없으면 → 누락 (status="missing")

    Returns:
      {"status": "ok"} |
      {"status": "attach_from_parent", "link": {"url", "title"},
       "parent_identifier": str} |
      {"status": "missing"}
    """
    # 1) QA카드 자체에서 요구사항 링크 확인
    if _extract_requirement_link(qa_card):
        return {"status": "ok"}

    # 2) 'PRD 없음' 명시 → 알림 불필요
    if _has_no_prd_note(qa_card):
        return {"status": "ok"}

    # 3) 상위 카드 → 자동 첨부 대상
    parent = qa_card.get("parent")
    if parent:
        parent_id = parent.get("identifier", "")
        # 3-a) 상위 카드 안의 PRD 링크 우선
        parent_link = _extract_requirement_link(parent)
        # 3-b) 없으면 상위 카드 자체를 요구사항(PRD)으로 간주 → 상위 카드 URL
        if not parent_link and parent_id:
            parent_link = {
                "url": f"https://linear.app/buzzvil/issue/{parent_id}",
                "title": "PRD",
            }
        if parent_link:
            return {
                "status": "attach_from_parent",
                "link": parent_link,
                "parent_identifier": parent_id,
            }

    # 4) 상위 카드도 없음 → 누락
    return {"status": "missing"}


def watch_card_changes(qa_card: dict, config: dict) -> dict:
    """QA카드 1개에 대해 PRD/Figma 변경 감지를 수행.

    Returns: {
        "card_id": str,
        "title": str,
        "card_url": str,
        "prd_change": dict | None,
        "figma_changes": list[dict],
    }
    """
    card_id = qa_card["identifier"]
    card_url = f"https://linear.app/buzzvil/issue/{card_id}"

    # Linear description 변경 체크
    prd_change = check_description_change(qa_card)

    # Figma 변경 체크
    figma_targets = parse_figma_urls(qa_card)
    # config에서 추가 모니터링 대상 병합
    figma_cfg = config.get("figma", {}).get("watch_targets", [])
    for fc in figma_cfg:
        if fc.get("card_id") == card_id:
            for t in fc.get("targets", []):
                figma_targets.append(t)

    figma_changes = []
    # TODO: Figma API Collab seat rate limit(월 6회) 초과 문제로 비활성화
    #       Dev/Full seat 확보 후 아래 주석 해제
    # if figma_targets and os.environ.get("FIGMA_TOKEN"):
    #     figma_changes = check_figma_changes(figma_targets, card_id)
    # elif figma_targets:
    #     print(f"      FIGMA_TOKEN 미설정 — Figma 변경 감지 건너뜀")
    if figma_targets:
        print(f"      Figma 변경 감지 비활성화 (rate limit 문제)")

    return {
        "card_id": card_id,
        "title": qa_card.get("title", ""),
        "card_url": card_url,
        "prd_change": prd_change,
        "figma_changes": figma_changes,
    }
