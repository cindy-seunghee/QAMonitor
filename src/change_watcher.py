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


# ── Linear Description 변경 감지 ─────────────────────────────────────────


def _snapshot_path_linear(card_id: str) -> Path:
    return SNAPSHOT_DIR / f"linear_{card_id}.txt"


def check_description_change(qa_card: dict) -> dict | None:
    """QA카드 description을 이전 스냅샷과 비교. 변경 시 diff 반환.

    Returns: {"card_id": str, "title": str, "diff_text": str, "card_url": str} | None
    """
    card_id = qa_card["identifier"]
    description = qa_card.get("description") or ""
    snap_path = _snapshot_path_linear(card_id)

    SNAPSHOT_DIR.mkdir(exist_ok=True)

    if not snap_path.exists():
        # 최초 스냅샷 저장 (알림 없음)
        snap_path.write_text(description, encoding="utf-8")
        return None

    old_desc = snap_path.read_text(encoding="utf-8")
    if old_desc == description:
        return None

    # diff 생성
    old_lines = old_desc.splitlines()
    new_lines = description.splitlines()
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="변경 전", tofile="변경 후",
        lineterm="",
    ))

    if not diff_lines:
        # 줄바꿈 차이만 있는 경우
        snap_path.write_text(description, encoding="utf-8")
        return None

    # 스냅샷 업데이트
    snap_path.write_text(description, encoding="utf-8")

    # diff 포맷팅 (최대 30줄)
    diff_text = "\n".join(diff_lines[:30])
    if len(diff_lines) > 30:
        diff_text += f"\n... 외 {len(diff_lines) - 30}줄"

    card_url = f"https://linear.app/buzzvil/issue/{card_id}"
    return {
        "card_id": card_id,
        "title": qa_card.get("title", ""),
        "diff_text": diff_text,
        "card_url": card_url,
    }


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
