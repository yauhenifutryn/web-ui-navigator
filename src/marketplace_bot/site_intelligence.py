from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from marketplace_bot.navigator_models import SiteMemory


def normalize_site_origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url


def build_site_memory_key(domain_pack: str, site_origin: str) -> str:
    raw = f"{domain_pack}|{site_origin}".encode("utf-8")
    return f"mem_{hashlib.sha256(raw).hexdigest()[:12]}"


def _node_key(item: dict[str, Any]) -> str:
    explicit_key = str(item.get("key", "")).strip()
    if explicit_key:
        return explicit_key
    url = str(item.get("url", "")).strip()
    if url:
        return url
    title = str(item.get("title", "")).strip().lower()
    return title


def _humanize_token(raw: str) -> str:
    cleaned = re.sub(r"[_\-]+", " ", str(raw or "").strip()).strip()
    if not cleaned:
        return ""
    return " ".join(part[:1].upper() + part[1:] for part in cleaned.split())


def _normalize_marketplace_token(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(raw or "").strip().lower()).strip("-")
    if cleaned.endswith("-"):
        cleaned = cleaned[:-1]
    return cleaned


def _derive_hierarchy_nodes(url: str) -> tuple[list[dict[str, Any]], str]:
    parsed = urlparse(str(url or "").strip())
    if not (parsed.scheme and parsed.netloc):
        return [], ""

    if "marketplace-simulation.com" in parsed.netloc and parsed.path.endswith("/engine.php"):
        query = parse_qs(parsed.query)
        quarter = str((query.get("quarter") or [""])[0]).strip()
        tab = str((query.get("tab") or [""])[0]).strip().lower()
        parent_resource = _normalize_marketplace_token((query.get("parentResource") or [""])[0])
        resource = _normalize_marketplace_token((query.get("resource") or [""])[0])
        parent_value = parent_resource or resource
        nodes: list[dict[str, Any]] = []
        parent_key = ""
        if quarter:
            quarter_key = f"marketplace::quarter::{quarter}"
            nodes.append(
                {
                    "key": quarter_key,
                    "title": f"Quarter {quarter}",
                    "url": "",
                    "section_count": 0,
                    "parent_key": "",
                    "quarter_number": int(quarter) if quarter.isdigit() else quarter,
                    "editable": False,
                    "synthetic": True,
                }
            )
            parent_key = quarter_key
        if tab:
            tab_key = f"{parent_key or 'marketplace'}::tab::{tab}"
            nodes.append(
                {
                    "key": tab_key,
                    "title": _humanize_token(tab),
                    "url": "",
                    "section_count": 0,
                    "parent_key": parent_key,
                    "quarter_number": int(quarter) if quarter.isdigit() else quarter if quarter else None,
                    "editable": False,
                    "synthetic": True,
                }
            )
            parent_key = tab_key
        if parent_value:
            parent_node_key = f"{parent_key or 'marketplace'}::parent::{parent_value}"
            nodes.append(
                {
                    "key": parent_node_key,
                    "title": _humanize_token(parent_value),
                    "url": "",
                    "section_count": 0,
                    "parent_key": parent_key,
                    "quarter_number": int(quarter) if quarter.isdigit() else quarter if quarter else None,
                    "editable": False,
                    "synthetic": True,
                }
            )
            parent_key = parent_node_key
        return nodes, parent_key

    return [], ""


def _normalized_site_map(site_index: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append_raw_node(
        item: dict[str, Any],
        *,
        key_override: str = "",
        parent_key_override: str = "",
        title_override: str = "",
        section_count_override: int | None = None,
        quarter_override: Any = None,
        editable_override: bool | None = None,
    ) -> None:
        key = key_override.strip() or _node_key(item)
        if not key or key in seen:
            return
        seen.add(key)
        normalized.append(
            {
                "key": key,
                "url": str(item.get("url", "")).strip(),
                "title": title_override.strip() or str(item.get("title", "")).strip(),
                "section_count": int(section_count_override if section_count_override is not None else item.get("section_count", 0) or 0),
                "parent_key": parent_key_override.strip() or str(item.get("parent_key", "")).strip(),
                "quarter_number": quarter_override if quarter_override is not None else item.get("quarter_number"),
                "editable": bool(editable_override if editable_override is not None else item.get("editable", False)),
                "synthetic": bool(item.get("synthetic", False)),
            }
        )

    def _append_node(
        item: dict[str, Any],
        *,
        key_override: str = "",
        parent_key_override: str = "",
        title_override: str = "",
        section_count_override: int | None = None,
        quarter_override: Any = None,
        editable_override: bool | None = None,
    ) -> None:
        explicit_parent_key = parent_key_override.strip() or str(item.get("parent_key", "")).strip()
        if not explicit_parent_key:
            parent_url = str(item.get("parent_url", "")).strip()
            parent_title = str(item.get("parent_title", "")).strip()
            explicit_parent_key = _node_key({"url": parent_url, "title": parent_title}) if parent_url or parent_title else ""

        derived_nodes, derived_parent_key = _derive_hierarchy_nodes(str(item.get("url", "")).strip())
        for derived in derived_nodes:
            _append_raw_node(derived)

        _append_raw_node(
            item,
            key_override=key_override,
            parent_key_override=explicit_parent_key or derived_parent_key,
            title_override=title_override,
            section_count_override=section_count_override,
            quarter_override=quarter_override,
            editable_override=editable_override,
        )

    for item in site_index.get("site_map", []) or []:
        if isinstance(item, dict):
            _append_node(item)

    for item in site_index.get("completed_quarters", []) or []:
        if not isinstance(item, dict):
            continue
        section_count = len(item.get("sections", []) or [])
        _append_node(item, section_count_override=section_count)

    parent_title = str(site_index.get("title", "")).strip().lower()
    parent_url = str(site_index.get("url", "")).strip().lower()
    default_quarter = site_index.get("editable_quarter")
    for position, item in enumerate(site_index.get("sections", []) or []):
        if not isinstance(item, dict):
            continue
        menu_item = str(item.get("menu_item") or item.get("title") or "").strip()
        if not menu_item:
            continue
        section_quarter = item.get("quarter_number", default_quarter)
        synthetic_key = f"section::{section_quarter or ''}::{parent_url or parent_title}::{position}::{menu_item.lower()}"
        navigation_items = item.get("navigation_items", []) or []
        parent_key = str(item.get("parent_key", "")).strip()
        if not parent_key:
            parent_key = _node_key(
                {
                    "url": str(item.get("parent_url", "") or site_index.get("url", "")).strip(),
                    "title": str(item.get("parent_title", "") or site_index.get("title", "")).strip(),
                }
            )
        _append_node(
            item,
            key_override=synthetic_key,
            parent_key_override=parent_key,
            title_override=menu_item,
            section_count_override=len(navigation_items),
            quarter_override=section_quarter,
            editable_override=bool(default_quarter and section_quarter == default_quarter),
        )
    return normalized


def _normalized_navigation_items(site_index: dict[str, Any]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for item in site_index.get("navigation_items", []) or []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def build_structure_manifest(site_index: dict[str, Any]) -> dict[str, Any]:
    normalized_site_map = _normalized_site_map(site_index)
    normalized_nodes: list[dict[str, Any]] = []
    for position, item in enumerate(normalized_site_map):
        normalized_nodes.append(
            {
                "key": _node_key(item),
                "position": position,
                "url": str(item.get("url", "")).strip(),
                "title": str(item.get("title", "")).strip(),
                "section_count": int(item.get("section_count", 0) or 0),
                "parent_key": str(item.get("parent_key", "")).strip(),
                "quarter_number": item.get("quarter_number"),
                "editable": bool(item.get("editable", False)),
                "synthetic": bool(item.get("synthetic", False)),
            }
        )
    return {
        "navigation_items": _normalized_navigation_items(site_index),
        "node_count": len(normalized_nodes),
        "nodes": normalized_nodes,
    }


def compute_structure_digest(site_index: dict[str, Any]) -> str:
    manifest = build_structure_manifest(site_index)
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()


def compute_site_fingerprint(site_origin: str, site_index: dict[str, Any]) -> str:
    manifest = build_structure_manifest(site_index)
    payload = {
        "site_origin": normalize_site_origin(site_origin),
        "navigation_items": manifest.get("navigation_items", []),
        "site_map_keys": [str(item.get("key", "")) for item in manifest.get("nodes", [])],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def analyze_site_change(existing_memory: SiteMemory | None, probe: dict[str, Any]) -> dict[str, Any]:
    site_origin = normalize_site_origin(str(probe.get("site_origin", "")))
    current_manifest = build_structure_manifest(probe)
    current_nav = [str(item) for item in current_manifest.get("navigation_items", [])]
    current_nodes = {str(item.get("key", "")): item for item in current_manifest.get("nodes", [])}
    current_node_count = int(current_manifest.get("node_count", 0) or 0)

    if existing_memory is None:
        return {
            "site_origin": site_origin,
            "change_status": "new",
            "change_summary": f"No prior site memory exists yet. A fresh site index is required to build the first structure checklist across {current_node_count} nodes.",
            "refresh_scope": "full",
            "new_nodes": sorted(current_nodes),
            "changed_nodes": [],
            "removed_nodes": [],
            "matched_nodes": 0,
            "previous_node_count": 0,
            "current_node_count": current_node_count,
        }

    previous_site_index = existing_memory.indexed_context.get("site_index", {})
    previous_manifest = build_structure_manifest(previous_site_index)
    previous_nodes = {str(item.get("key", "")): item for item in previous_manifest.get("nodes", [])}
    previous_nav = [str(item) for item in previous_manifest.get("navigation_items", [])]
    previous_node_count = int(previous_manifest.get("node_count", 0) or 0)

    new_nodes = sorted(set(current_nodes) - set(previous_nodes))
    changed_nodes: list[str] = []
    for key in sorted(set(current_nodes) & set(previous_nodes)):
        if (
            str(current_nodes[key].get("title", "")) != str(previous_nodes[key].get("title", ""))
            or int(current_nodes[key].get("section_count", 0) or 0) != int(previous_nodes[key].get("section_count", 0) or 0)
            or str(current_nodes[key].get("parent_key", "")) != str(previous_nodes[key].get("parent_key", ""))
            or current_nodes[key].get("quarter_number") != previous_nodes[key].get("quarter_number")
            or bool(current_nodes[key].get("editable", False)) != bool(previous_nodes[key].get("editable", False))
        ):
            changed_nodes.append(key)
    nav_expanded = any(item not in previous_nav for item in current_nav if item)
    nav_reduced = any(item not in current_nav for item in previous_nav if item)
    nav_changed = nav_expanded or nav_reduced
    removed_nodes = sorted(set(previous_nodes) - set(current_nodes)) if nav_reduced else []
    matched_nodes = len(set(current_nodes) & set(previous_nodes)) - len(changed_nodes)

    if not new_nodes and not changed_nodes and not removed_nodes and not nav_changed:
        return {
            "site_origin": site_origin,
            "change_status": "unchanged",
            "change_summary": (
                f"Structure checklist matched all {matched_nodes} known nodes. "
                "The agent can reuse the saved site map and refresh only the current page."
            ),
            "refresh_scope": "partial",
            "new_nodes": [],
            "changed_nodes": [],
            "removed_nodes": [],
            "matched_nodes": matched_nodes,
            "previous_node_count": previous_node_count,
            "current_node_count": current_node_count,
        }

    total_changes = len(new_nodes) + len(changed_nodes) + len(removed_nodes) + (1 if nav_changed else 0)
    refresh_scope = "full" if total_changes > 3 else "partial"
    summary_parts: list[str] = []
    if matched_nodes:
        summary_parts.append(f"reused nodes: {matched_nodes}")
    if new_nodes:
        summary_parts.append(f"new nodes: {len(new_nodes)}")
    if changed_nodes:
        summary_parts.append(f"changed nodes: {len(changed_nodes)}")
    if removed_nodes:
        summary_parts.append(f"removed nodes: {len(removed_nodes)}")
    if nav_changed:
        summary_parts.append("navigation changed")

    return {
        "site_origin": site_origin,
        "change_status": "changed",
        "change_summary": "Structure checklist drift detected, " + ", ".join(summary_parts) + f". The agent will run a {refresh_scope} refresh.",
        "refresh_scope": refresh_scope,
        "new_nodes": new_nodes,
        "changed_nodes": changed_nodes,
        "removed_nodes": removed_nodes,
        "matched_nodes": matched_nodes,
        "previous_node_count": previous_node_count,
        "current_node_count": current_node_count,
    }


def choose_index_strategy(index_mode: str, existing_memory: dict[str, Any] | None, change_report: dict[str, Any]) -> str:
    if index_mode == "lightweight":
        return "lightweight"
    if index_mode == "advanced":
        return "full"
    if existing_memory is None:
        return "full"
    if change_report.get("refresh_scope") == "full":
        return "full"
    return "partial"


def merge_site_index(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(previous or {})
    current = dict(current or {})

    merged_nav: list[str] = []
    for item in [*(previous or {}).get("navigation_items", []), *current.get("navigation_items", [])]:
        text = str(item).strip()
        if text and text not in merged_nav:
            merged_nav.append(text)

    merged_map: dict[str, dict[str, Any]] = {}
    for item in _normalized_site_map(previous or {}):
        merged_map[_node_key(item)] = item
    for item in _normalized_site_map(current):
        merged_map[_node_key(item)] = item

    merged["navigation_items"] = merged_nav
    merged["site_map"] = list(merged_map.values())

    for key in ("title", "url", "source", "editable_quarter", "quarter_range"):
        if key in current and current.get(key) not in (None, "", {}, []):
            merged[key] = current.get(key)
        elif key not in merged:
            merged[key] = current.get(key)

    if current.get("sections"):
        merged["sections"] = current.get("sections", [])
    elif "sections" not in merged:
        merged["sections"] = []

    if current.get("semantic_text"):
        merged["semantic_text"] = current.get("semantic_text", "")
    elif "semantic_text" not in merged:
        merged["semantic_text"] = ""

    if previous.get("completed_quarters") or current.get("completed_quarters"):
        quarters: dict[str, dict[str, Any]] = {}
        for item in [*(previous or {}).get("completed_quarters", []), *current.get("completed_quarters", [])]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("url") or item.get("quarter_number") or item.get("title") or "")
            if not key:
                continue
            quarters[key] = item
        merged["completed_quarters"] = list(quarters.values())

    return merged
