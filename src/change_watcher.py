"""PRD/Figma 변경 감지 — TC 작성 기간 중 description/디자인 변경 모니터링"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path

import requests


SNAPSHOT_DIR = Path("snapshots")
MAX_DEPTH = 3

# Figma URL에서 fileKey, nodeId 추출
_FIGMA_URL_RE = re.compile(
    r"figma\.com/(?:design|file)/([a-zA-Z0-9]+)(?:/[^?]*)?\?.*?node-id=([0-9]+-[0-9]+)"
)


# ── PRD 소스 탐색 (Linear / Confluence) ──────────────────────────────────

_LINEAR_ISSUE_RE = re.compile(r"linear\.app/[^/]+/issue/([A-Z]+-\d+)")
_CONFLUENCE_PAGE_RE = re.compile(r"atlassian\.net/wiki/.*?/(?:pages|history)/(\d+)")


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

    Returns: {"title": str, "body": str} | None
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

    # HTML → 텍스트 변환 (간이 파싱)
    body_text = _html_to_text(body_html)
    return {"title": title, "body": body_text}


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

    # 나머지 HTML 태그 제거
    text = re.sub(r"<[^>]+>", "", text)

    # HTML 엔티티 디코드
    text = html_module.unescape(text)

    # 연속 빈 줄 정리
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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
        prd_title = page["title"]
        prd_id = f"Confluence #{prd_source['page_id']}"
        prd_url = prd_source["url"]

    else:
        return None

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
                        if detail["label"]:
                            desc = f"{detail['label']}: '{detail['old']}' → '{detail['new']}'"
                        else:
                            desc = f"'{detail['old']}' → '{detail['new']}'"
                        changes.append({"type": "modified", "section": section, "detail": desc, "is_table": detail.get("is_table", False)})
                    else:
                        section = _find_heading_path(old_lines, rm_idx)
                        is_table = bool(_parse_table_row(rm_raw))
                        changes.append({"type": "modified", "section": section, "detail": f"'{_clean_line(rm_raw)}' → '{clean}'", "is_table": is_table})
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


def _format_grouped(items: list[dict]) -> list[str]:
    """같은 섹션의 항목들을 그룹핑하여 포맷팅.
    같은 섹션이 연속되면 섹션명 한 번만 표시, 하위에 detail 나열.
    """
    lines = []
    prev_section = None

    for c in items:
        section = c.get("section", "")
        detail = c.get("detail", "")
        is_table = c.get("is_table", False)
        table_tag = "[표] " if is_table else ""

        if section and section == prev_section:
            # 같은 섹션 — 하위 항목만
            lines.append(f"        \u25E6 {table_tag}{detail}")
        elif section:
            # 새 섹션
            lines.append(f"    \u2022 {section}")
            lines.append(f"        \u25E6 {table_tag}{detail}")
            prev_section = section
        else:
            # 섹션 없음
            lines.append(f"    \u25E6 {table_tag}{detail}")
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
    """Figma REST API로 노드 트리 조회"""
    token = os.environ.get("FIGMA_TOKEN", "")
    if not token:
        raise RuntimeError("FIGMA_TOKEN 환경변수가 설정되지 않았습니다")

    ids = ",".join(nid.replace(":", "-") for nid in node_ids)
    url = f"https://api.figma.com/v1/files/{file_key}/nodes?ids={ids}"
    resp = requests.get(url, headers={"X-Figma-Token": token}, timeout=30)
    resp.raise_for_status()
    return resp.json()


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

    for file_key, targets in by_file.items():
        node_ids = [t["node_id"] for t in targets]
        try:
            api_data = _fetch_figma_nodes(file_key, node_ids)
        except Exception as e:
            print(f"      Figma API 오류 ({file_key}): {e}")
            continue

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
    """TC 작성 기간인지 판단.
    - 테스트 전 → 항상 감시
    - 통합테스트 시작일 당일 → 오전(12시 전)에만 마지막 1회 체크
    """
    from datetime import datetime, timezone, timedelta
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today = now.strftime("%Y-%m-%d")

    current_phase = test_phases.get("current_phase", "")

    # 테스트 전 → 항상 감시
    if current_phase == "테스트 전":
        return True

    # 통합테스트 시작일 당일 → 오전(12시 전)에만 마지막 1회
    integration = test_phases.get("integration")
    if integration and integration["start"] == today and now.hour < 12:
        return True

    return False


_PRD_KEYWORDS = ["prd"]


def _has_no_prd_note(qa_card: dict) -> bool:
    """Description 특이사항에 'PRD 없음' 문구가 있는지 확인."""
    description = qa_card.get("description") or ""
    for line in description.splitlines():
        stripped = line.strip().lstrip("*").strip()
        if "prd 없음" in stripped.lower():
            return True
    return False


def check_missing_links(qa_card: dict) -> dict:
    """QA카드에 PRD 링크와 Figma 링크가 있는지 확인.

    PRD 판단 기준:
      1) Attachments에 title이 "PRD"(대소문자 무관)인 항목이 있으면 → PRD 있음
      2) Description 특이사항에 'PRD 없음' 문구가 있으면 → 의도적 미첨부 (알림 안 함)
      3) 둘 다 없으면 → PRD 누락 (알림)

    Returns: {"missing_prd": bool, "missing_figma": bool}
    """
    attachments = qa_card.get("attachments", {}).get("nodes", [])

    # PRD 체크: Attachments에 title "PRD" 포함
    has_prd = any(
        any(kw in (att.get("title") or "").lower() for kw in _PRD_KEYWORDS)
        for att in attachments
    )

    # Description에 'PRD 없음' 명시된 경우 → 알림 불필요
    no_prd_noted = _has_no_prd_note(qa_card)

    # Figma 체크
    has_figma = bool(parse_figma_urls(qa_card))

    return {
        "missing_prd": not has_prd and not no_prd_noted,
        "missing_figma": not has_figma,
    }


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
    if figma_targets and os.environ.get("FIGMA_TOKEN"):
        figma_changes = check_figma_changes(figma_targets, card_id)
    elif figma_targets:
        print(f"      FIGMA_TOKEN 미설정 — Figma 변경 감지 건너뜀")

    return {
        "card_id": card_id,
        "title": qa_card.get("title", ""),
        "card_url": card_url,
        "prd_change": prd_change,
        "figma_changes": figma_changes,
    }
