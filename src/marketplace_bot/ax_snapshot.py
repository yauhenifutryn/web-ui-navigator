from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


INTERACTIVE_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "link",
    "listbox",
    "menuitem",
    "option",
    "radio",
    "scrollbar",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
    "treeitem",
}


@dataclass
class AxSnapshot:
    source: str
    mode: str
    summary: str
    targets: list[dict[str, Any]]
    diagnostics: dict[str, Any]
    raw: dict[str, Any]


class AxSnapshotProvider(ABC):
    @abstractmethod
    async def capture(
        self,
        page: Any,
        mode: str,
        target_scope: dict[str, Any] | None,
        include_occlusion: bool = False,
    ) -> AxSnapshot:
        raise NotImplementedError


class McpAxSnapshotProvider(AxSnapshotProvider):
    def __init__(self, fetcher: Any | None = None, max_nodes: int = 60) -> None:
        self.fetcher = fetcher
        self.max_nodes = max_nodes

    async def capture(
        self,
        page: Any,
        mode: str,
        target_scope: dict[str, Any] | None,
        include_occlusion: bool = False,
    ) -> AxSnapshot:
        if self.fetcher is None:
            raise RuntimeError("AX MCP provider is not configured")
        raw = await self.fetcher(page=page, mode=mode, target_scope=target_scope, include_occlusion=include_occlusion)
        nodes = [_normalize_input_node(item, source="mcp", index=index) for index, item in enumerate((raw or {}).get("nodes", []))]
        return _build_snapshot(nodes, source="mcp", mode=mode, raw=raw or {}, target_scope=target_scope, max_nodes=self.max_nodes)


class CdpAxSnapshotProvider(AxSnapshotProvider):
    def __init__(self, max_nodes: int = 60) -> None:
        self.max_nodes = max_nodes

    async def capture(
        self,
        page: Any,
        mode: str,
        target_scope: dict[str, Any] | None,
        include_occlusion: bool = False,
    ) -> AxSnapshot:
        session = await page.context.new_cdp_session(page)
        ax_tree = await session.send("Accessibility.getFullAXTree")
        runtime_nodes = await page.evaluate(
            """
            ({ includeOcclusion }) => {
              const selectors = [
                "button",
                "a",
                "input",
                "select",
                "textarea",
                "[role]",
                "[tabindex]",
                "summary",
              ].join(",");
              const visibleText = (el) => ((el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim()).slice(0, 120);
              const roleFor = (el) => {
                const explicit = (el.getAttribute("role") || "").trim().toLowerCase();
                if (explicit) return explicit;
                const tag = el.tagName.toLowerCase();
                if (tag === "a" && el.href) return "link";
                if (tag === "button") return "button";
                if (tag === "select") return "combobox";
                if (tag === "textarea") return "textbox";
                if (tag === "input") {
                  const type = (el.getAttribute("type") || "text").toLowerCase();
                  if (type === "checkbox") return "checkbox";
                  if (type === "radio") return "radio";
                  return "textbox";
                }
                return tag;
              };
              return Array.from(document.querySelectorAll(selectors)).map((el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const visible = style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                const viewportRatio = !visible
                  ? 0
                  : Math.max(0, Math.min(1,
                      (Math.min(window.innerWidth, rect.right) - Math.max(0, rect.left)) *
                      (Math.min(window.innerHeight, rect.bottom) - Math.max(0, rect.top)) /
                      Math.max(1, rect.width * rect.height)
                    ));
                let occluded = false;
                if (includeOcclusion && visible) {
                  const centerX = Math.min(window.innerWidth - 1, Math.max(0, rect.left + rect.width / 2));
                  const centerY = Math.min(window.innerHeight - 1, Math.max(0, rect.top + rect.height / 2));
                  const topEl = document.elementFromPoint(centerX, centerY);
                  occluded = !!topEl && topEl !== el && !el.contains(topEl);
                }
                return {
                  role: roleFor(el),
                  name: (el.getAttribute("aria-label") || visibleText(el) || el.getAttribute("name") || "").trim(),
                  focusable: typeof el.tabIndex === "number" ? el.tabIndex >= 0 : false,
                  disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
                  visible,
                  viewport_ratio: Number.isFinite(viewportRatio) ? viewportRatio : 0,
                  pointer_events: style.pointerEvents || "",
                  opacity: Number(style.opacity || "1"),
                  z_index: style.zIndex || "",
                  bounds: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                  occluded,
                };
              }).filter((item) => item.role || item.name);
            }
            """,
            {"includeOcclusion": include_occlusion},
        )
        nodes = _normalize_cdp_nodes((ax_tree or {}).get("nodes", []), runtime_nodes if isinstance(runtime_nodes, list) else [])
        return _build_snapshot(
            nodes,
            source="cdp",
            mode=mode,
            raw={"ax_tree": ax_tree or {}, "runtime_nodes": runtime_nodes or []},
            target_scope=target_scope,
            max_nodes=self.max_nodes,
        )


class FallbackAxSnapshotProvider(AxSnapshotProvider):
    def __init__(self, providers: list[AxSnapshotProvider]) -> None:
        self.providers = providers

    async def capture(
        self,
        page: Any,
        mode: str,
        target_scope: dict[str, Any] | None,
        include_occlusion: bool = False,
    ) -> AxSnapshot:
        last_exc: Exception | None = None
        for provider in self.providers:
            try:
                return await provider.capture(page, mode, target_scope, include_occlusion=include_occlusion)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No AX snapshot providers are configured")


def build_ax_snapshot_provider(
    preference: str = "mcp_then_cdp",
    *,
    max_nodes_index: int = 90,
    max_nodes_live: int = 48,
    max_nodes_verify: int = 24,
    mcp_fetcher: Any | None = None,
) -> AxSnapshotProvider:
    # v1 uses a single cap per provider instance, tuned by the largest mode it may serve.
    cdp = CdpAxSnapshotProvider(max_nodes=max(max_nodes_index, max_nodes_live, max_nodes_verify))
    mcp = McpAxSnapshotProvider(fetcher=mcp_fetcher, max_nodes=max(max_nodes_index, max_nodes_live, max_nodes_verify))
    if preference == "cdp":
        return cdp
    if preference == "mcp":
        return mcp
    return FallbackAxSnapshotProvider([mcp, cdp])


def _normalize_cdp_nodes(ax_tree_nodes: list[dict[str, Any]], runtime_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runtime_by_backend = {
        int(item["backendDOMNodeId"]): item
        for item in runtime_nodes
        if isinstance(item, dict) and item.get("backendDOMNodeId") is not None
    }
    runtime_by_key = {
        (str(item.get("role", "")).strip().lower(), str(item.get("name", "")).strip().lower()): item
        for item in runtime_nodes
        if isinstance(item, dict)
    }
    child_to_parent: dict[str, str] = {}
    for item in ax_tree_nodes:
        node_id = str(item.get("nodeId", ""))
        for child in item.get("childIds", []) or []:
            child_to_parent[str(child)] = node_id

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(ax_tree_nodes):
        backend_dom_node_id = item.get("backendDOMNodeId")
        role = _coerce_value(item.get("role"))
        name = _coerce_value(item.get("name"))
        props = {
            str(prop.get("name", "")): _coerce_value(prop.get("value"))
            for prop in item.get("properties", []) or []
            if isinstance(prop, dict)
        }
        runtime = runtime_by_backend.get(int(backend_dom_node_id)) if backend_dom_node_id is not None and int(backend_dom_node_id) in runtime_by_backend else runtime_by_key.get((role.lower(), name.lower()))
        candidate = {
            "backend_dom_node_id": backend_dom_node_id,
            "parent_ax_node_id": child_to_parent.get(str(item.get("nodeId", ""))),
            "role": role,
            "name": name,
            "focusable": bool(props.get("focusable") or False),
            "disabled": bool(props.get("disabled") or False),
            "visible": True,
            "viewport_ratio": 1.0,
            "pointer_events": "auto",
            "opacity": 1.0,
            "z_index": "",
            "bounds": {},
            "occluded": False,
            "source_confidence": 0.55,
        }
        if isinstance(runtime, dict):
            candidate.update(
                {
                    "role": str(runtime.get("role", role)).strip() or role,
                    "name": str(runtime.get("name", name)).strip() or name,
                    "focusable": bool(runtime.get("focusable", candidate["focusable"])),
                    "disabled": bool(runtime.get("disabled", candidate["disabled"])),
                    "visible": bool(runtime.get("visible", True)),
                    "viewport_ratio": float(runtime.get("viewport_ratio", 1.0) or 0.0),
                    "pointer_events": str(runtime.get("pointer_events", "auto") or "auto"),
                    "opacity": float(runtime.get("opacity", 1.0) or 0.0),
                    "z_index": str(runtime.get("z_index", "") or ""),
                    "bounds": dict(runtime.get("bounds", {}) or {}),
                    "occluded": bool(runtime.get("occluded", False)),
                    "source_confidence": 0.92 if backend_dom_node_id is not None else 0.7,
                }
            )
        normalized.append(_normalize_input_node(candidate, source="cdp", index=index))
    return normalized


def _normalize_input_node(item: dict[str, Any], *, source: str, index: int) -> dict[str, Any]:
    role = str(item.get("role", "") or "").strip()
    name = str(item.get("name", "") or "").strip()
    backend_dom_node_id = item.get("backend_dom_node_id") or item.get("backendDOMNodeId")
    bounds = dict(item.get("bounds", {}) or {})
    visible = bool(item.get("visible", True))
    viewport_ratio = float(item.get("viewport_ratio", item.get("viewportRatio", 1.0)) or 0.0)
    pointer_events = str(item.get("pointer_events", item.get("pointerEvents", "auto")) or "auto")
    opacity = float(item.get("opacity", 1.0) or 0.0)
    occluded = bool(item.get("occluded") or ((item.get("occlusion") or {}).get("occluded") if isinstance(item.get("occlusion"), dict) else False))
    focusable = bool(item.get("focusable", False))
    disabled = bool(item.get("disabled", False))
    interactive = bool(item.get("interactive", False)) or focusable or role.lower() in INTERACTIVE_ROLES
    blocked_reasons: list[str] = []
    if disabled:
        blocked_reasons.append("disabled")
    if not visible or opacity <= 0:
        blocked_reasons.append("hidden")
    if viewport_ratio <= 0:
        blocked_reasons.append("offscreen")
    if pointer_events == "none":
        blocked_reasons.append("pointer_blocked")
    if occluded:
        blocked_reasons.append("occluded")
    block_reason = blocked_reasons[0] if blocked_reasons else ""
    actionable = interactive and not blocked_reasons
    stable_raw = json.dumps(
        {
            "source": source,
            "backend_dom_node_id": backend_dom_node_id,
            "role": role,
            "name": name,
            "bounds": bounds,
            "index": index,
        },
        sort_keys=True,
    )
    ax_node_id = f"ax_{hashlib.sha256(stable_raw.encode('utf-8')).hexdigest()[:12]}"
    return {
        "ax_node_id": ax_node_id,
        "backend_dom_node_id": backend_dom_node_id,
        "parent_ax_node_id": item.get("parent_ax_node_id") or item.get("parentAxNodeId"),
        "role": role,
        "name": name,
        "interactive": interactive,
        "focusable": focusable,
        "disabled": disabled,
        "visible": visible,
        "viewport_ratio": viewport_ratio,
        "pointer_events": pointer_events,
        "opacity": opacity,
        "z_index": str(item.get("z_index", item.get("zIndex", "")) or ""),
        "bounds": bounds,
        "occluded": occluded,
        "actionable": actionable,
        "block_reason": block_reason,
        "blocked_reasons": blocked_reasons,
        "source_confidence": float(item.get("source_confidence", 0.65) or 0.0),
    }


def _build_snapshot(
    nodes: list[dict[str, Any]],
    *,
    source: str,
    mode: str,
    raw: dict[str, Any],
    target_scope: dict[str, Any] | None,
    max_nodes: int,
) -> AxSnapshot:
    ranked = _rank_nodes(nodes, target_scope=target_scope)[:max_nodes]
    interactive_nodes = [item for item in ranked if item.get("interactive")]
    blocked_nodes = [item for item in interactive_nodes if not item.get("actionable")]
    likely_occluded_nodes = [item for item in interactive_nodes if item.get("occluded")]
    diagnostics = {
        "interactive_nodes": len(interactive_nodes),
        "blocked_nodes": len(blocked_nodes),
        "disabled_nodes": len([item for item in blocked_nodes if item.get("block_reason") == "disabled"]),
        "offscreen_nodes": len([item for item in blocked_nodes if item.get("block_reason") == "offscreen"]),
        "likely_occluded_nodes": len(likely_occluded_nodes),
    }
    summary = (
        f"AX: {diagnostics['interactive_nodes']} interactive nodes, "
        f"{diagnostics['blocked_nodes']} blocked, "
        f"{diagnostics['likely_occluded_nodes']} likely occluded"
    )
    targets = []
    for item in interactive_nodes[:12]:
        targets.append(
            {
                "ax_node_id": item["ax_node_id"],
                "role": item["role"],
                "name": item["name"],
                "actionable": bool(item["actionable"]),
                "block_reason": item["block_reason"],
                "bounds": item["bounds"],
                "source_confidence": item["source_confidence"],
            }
        )
    return AxSnapshot(source=source, mode=mode, summary=summary, targets=targets, diagnostics=diagnostics, raw=raw)


def _rank_nodes(nodes: list[dict[str, Any]], *, target_scope: dict[str, Any] | None) -> list[dict[str, Any]]:
    target_text = str((target_scope or {}).get("target_text", "") or "").strip().lower()
    target_role = str((target_scope or {}).get("role", "") or "").strip().lower()

    def _score(item: dict[str, Any]) -> tuple[int, float]:
        score = 0
        if item.get("interactive"):
            score += 10
        if item.get("actionable"):
            score += 8
        if item.get("visible"):
            score += 4
        if target_text and target_text in str(item.get("name", "")).lower():
            score += 12
        if target_role and target_role == str(item.get("role", "")).lower():
            score += 8
        score += min(4, int(float(item.get("viewport_ratio", 0) or 0) * 4))
        return score, float(item.get("source_confidence", 0.0) or 0.0)

    return sorted(nodes, key=_score, reverse=True)


def _coerce_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("value")
    return value
