from __future__ import annotations

from contextlib import asynccontextmanager
import html
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import marketplace_bot.kill_switch as kill_switch
from marketplace_bot.bootstrap import bootstrap_runtime
from marketplace_bot.config import SETTINGS
from marketplace_bot.navigator_models import (
    ActionProposal,
    ApprovalRequest,
    CreateSessionRequest,
    ExecuteResultPayload,
    ObservationPacket,
    OverlayCommandRequest,
    SessionMemory,
)
from marketplace_bot.navigator_runtime import build_navigator_runtime
from marketplace_bot.remote_client import RemoteNavigatorClient
from marketplace_bot.site_intelligence import build_structure_manifest
from marketplace_bot.state_store import StateStore


STATIC_DIR = Path(__file__).resolve().parent / "static"


def _default_setup_context() -> dict[str, str]:
    if "marketplace-simulation.com" in SETTINGS.target_domain:
        return {
            "project_name": "Marketplace Session",
            "goal": "Review this Marketplace Simulation workspace and prepare quarter-aware guidance without changing decisions yet.",
            "domain_pack": "marketplace_simulation",
            "index_mode": "advanced",
        }
    return {
        "project_name": "Navigator Session",
        "goal": "",
        "domain_pack": "generic_web",
        "index_mode": "advanced",
    }


def create_app(
    orchestrator: Any | None = None,
    state_store: StateStore | None = None,
    navigator_runtime: Any | None = None,
) -> FastAPI:
    store = state_store or StateStore(SETTINGS.runtime_dir)
    store.bootstrap()
    navigator_runtime = navigator_runtime or build_navigator_runtime(store)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        bootstrap_runtime()
        yield

    app = FastAPI(title="Live Navigator Companion", lifespan=_lifespan)
    app.state.state_store = store
    app.state.navigator_runtime = navigator_runtime
    app.state.remote_client = RemoteNavigatorClient(SETTINGS.cloud_backend_url) if SETTINGS.use_cloud_backend else None
    app.state.active_session_id = None
    app.state.overlay_map_open = False
    app.state.overlay_sessions_open = False
    app.state.overlay_review_open = False
    app.state.overlay_site_check_preview = {}
    app.state.ui_base_url = ""

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    async def _list_sessions_payload() -> list[dict[str, Any]]:
        if app.state.remote_client is not None:
            payload = await app.state.remote_client.list_sessions()
            return list(payload.get("sessions", []))
        return [item.model_dump(mode="json") for item in app.state.navigator_runtime.companion.list_sessions()]

    async def _get_session_memory(session_id: str) -> SessionMemory | None:
        if app.state.remote_client is not None:
            try:
                payload = await app.state.remote_client.get_session(session_id)
            except Exception:
                return None
            return SessionMemory.model_validate(payload)
        return app.state.navigator_runtime.companion.get_session(session_id)

    def _site_ready(session: SessionMemory) -> bool:
        return bool(session.last_indexed_at) and not bool(session.site_check_required) and not bool(session.degraded_reason)

    def _remember_ui_base_url(request: Request | None = None) -> None:
        if request is None:
            return
        app.state.ui_base_url = str(request.base_url).rstrip("/")

    def _ui_base_url() -> str:
        value = str(getattr(app.state, "ui_base_url", "") or "").rstrip("/")
        return value or "http://127.0.0.1:8002"

    def _map_url(session_id: str) -> str:
        return f"{_ui_base_url()}/map?session_id={session_id}"

    def _review_url(session_id: str) -> str:
        return f"{_ui_base_url()}/review?session_id={session_id}"

    def _site_check_details_payload(raw: dict[str, Any] | None) -> dict[str, Any]:
        source = dict(raw or {})

        def _count(name: str) -> int:
            direct = source.get(f"{name}_count")
            if direct is not None:
                return int(direct or 0)
            values = source.get(name, [])
            if isinstance(values, list):
                return len(values)
            return int(values or 0)

        strategy = str(source.get("strategy", "")).strip().lower()
        change_status = str(source.get("change_status", "")).strip().lower()
        payload = {
            "change_status": change_status,
            "strategy": strategy,
            "matched_nodes": int(source.get("matched_nodes", 0) or 0),
            "changed_nodes_count": _count("changed_nodes"),
            "new_nodes_count": _count("new_nodes"),
            "removed_nodes_count": _count("removed_nodes"),
            "previous_node_count": int(source.get("previous_node_count", 0) or 0),
            "current_node_count": int(source.get("current_node_count", 0) or 0),
        }
        return payload if any(payload.values()) else {}

    async def _record_execution(payload: ExecuteResultPayload) -> dict[str, Any]:
        if app.state.remote_client is not None:
            return await app.state.remote_client.execute_result(payload)
        session = app.state.navigator_runtime.companion.record_execution(payload)
        return session.model_dump(mode="json")

    def _resume_status_message(session: SessionMemory) -> str:
        if session.status == "review_batch_ready" and session.review_batch is not None:
            return "Cached review restored. You can read it now. Re-index only if you want a fresh structure check before editing."
        if session.status == "index_summary_ready" and session.index_summary is not None:
            return "Cached index summary restored. Re-index when you want to verify what changed since the last visit."
        return "Session resumed. Run the site index to verify structure changes."

    async def _sync_overlay(panel: dict[str, Any]) -> None:
        await app.state.navigator_runtime.bridge.sync_agent_overlay(panel)

    async def _sync_setup_overlay(
        message: str = "Overlay connected. Start or resume a session here.",
        active_session: SessionMemory | None = None,
    ) -> None:
        setup_defaults = _default_setup_context()
        smart_scan_available = bool(active_session and (active_session.last_indexed_at or active_session.site_memory_key))
        await _sync_overlay(
            {
                "view": "setup",
                "title": "Live Navigator",
                "stage": "idle",
                "status": message,
                "goal": active_session.goal.raw_goal if active_session is not None else setup_defaults["goal"],
                "project_name": active_session.project_name if active_session is not None else setup_defaults["project_name"],
                "domain_pack": active_session.domain_pack if active_session is not None else setup_defaults["domain_pack"],
                "index_mode": active_session.index_mode if active_session is not None else setup_defaults["index_mode"],
                "active_session_id": active_session.session_id if active_session is not None else "",
                "sessions": await _list_sessions_payload(),
                "logs": [],
                "watch_mode": "off",
                "progress": 0,
                "current_step": "Waiting for a session.",
                "mode": active_session.mode if active_session is not None else ("complex_workspace" if setup_defaults["domain_pack"] == "marketplace_simulation" else "review_only"),
                "review_ready": False,
                "smart_scan_available": smart_scan_available,
                "activity_log_tail": list((active_session.activity_log_tail if active_session is not None else [])[:5]),
                "structure_map_summary": dict(active_session.structure_map_summary if active_session is not None else {}),
                "coverage_summary": dict(active_session.coverage_summary if active_session is not None else {}),
                "degraded_reason": str(active_session.degraded_reason if active_session is not None else ""),
                "insufficiently_grounded": bool(active_session.insufficiently_grounded) if active_session is not None else False,
            }
        )

    async def _panel_for_session(
        session: SessionMemory,
        status: str,
        *,
        progress: int | None = None,
        current_step: str | None = None,
        watch_mode: str = "off",
        site_check_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        visible_actions = [] if session.mode == "review_only" else list(session.pending_approvals or [])
        if not visible_actions and session.review_batch is not None:
            visible_actions = [] if session.mode == "review_only" else list(session.review_batch.actions or [])
        details = _site_check_details_payload(site_check_details)
        if not details:
            details = _site_check_details_payload(session.artifacts.get("site_check_details", {}))
        if not details:
            details = _site_check_details_payload(app.state.overlay_site_check_preview.get(session.session_id, {}))
        last_capture_at = ""
        last_capture_path = ""
        last_capture_page = ""
        last_capture_region: dict[str, Any] = {}
        if session.last_observation is not None:
            last_capture_at = str(session.last_observation.captured_at or "")
            last_capture_path = str(session.last_observation.screenshot_path or "")
            last_capture_page = str(session.last_observation.page_title or session.last_observation.page_url or "")
            last_capture_region = dict(session.last_observation.browser_metadata.get("page_region", {}) or {})
        return {
            "view": "session",
            "session_id": session.session_id,
            "title": session.project_name,
            "site_label": session.current_site or session.site_origin or session.project_name,
            "domain_pack": session.domain_pack,
            "mode": session.mode,
            "stage": session.status,
            "status": status,
            "goal": session.goal.raw_goal,
            "index_mode": session.index_mode,
            "site_check_required": session.site_check_required,
            "site_ready": _site_ready(session),
            "review_ready": bool(session.review_ready),
            "strategic_summary": session.strategic_summary,
            "site_check_summary": session.site_check_summary,
            "live_advice": session.live_advice,
            "actions": [item.model_dump(mode="json") for item in visible_actions],
            "active_session_id": session.session_id,
            "progress": progress,
            "current_step": current_step,
            "watch_mode": watch_mode,
            "sessions": await _list_sessions_payload(),
            "logs": session.event_log,
            "index_summary": session.index_summary.model_dump(mode="json") if session.index_summary else {},
            "review_batch": session.review_batch.model_dump(mode="json") if session.review_batch else {},
            "inline_notes": list(session.artifacts.get("inline_notes", [])),
            "current_site": session.current_site,
            "site_origin": session.site_origin,
            "map_url": _map_url(session.session_id),
            "review_url": _review_url(session.session_id),
            "map_open": bool(app.state.overlay_map_open),
            "sessions_open": bool(app.state.overlay_sessions_open),
            "review_open": bool(app.state.overlay_review_open),
            "auto_collapse_rail": session.status in {"indexing", "planning", "applying_batch", "live_advice"},
            "site_check_details": details,
            "ax_summary": str(session.artifacts.get("ax_summary", "")),
            "ax_diagnostics": dict(session.artifacts.get("ax_diagnostics", {}) or {}),
            "structure_map_preview": list(session.artifacts.get("structure_map_preview", []) or []),
            "structure_map_total": int(session.artifacts.get("structure_map_total", 0) or 0),
            "structure_manifest": dict(session.artifacts.get("normalized_structure_manifest", {}) or {}),
            "activity_log_tail": list(session.activity_log_tail[:5]),
            "structure_map_summary": dict(session.structure_map_summary or {}),
            "coverage_summary": dict(session.coverage_summary or {}),
            "degraded_reason": session.degraded_reason,
            "insufficiently_grounded": bool(session.insufficiently_grounded or (session.review_batch.insufficiently_grounded if session.review_batch else False)),
            "last_capture_at": last_capture_at,
            "last_capture_path": last_capture_path,
            "last_capture_page": last_capture_page,
            "last_capture_region": last_capture_region,
        }

    def _map_page_html(session: SessionMemory) -> str:
        manifest = build_structure_manifest(dict(session.indexed_context.get("site_index", {}) or {}))
        stored_manifest = dict(session.artifacts.get("normalized_structure_manifest", {}) or {})
        manifest_nodes = [item for item in manifest.get("nodes", []) if isinstance(item, dict)]
        if not manifest_nodes:
            manifest = stored_manifest
        nodes = [dict(item) for item in manifest.get("nodes", []) if isinstance(item, dict)]
        payload = {
            "sessionId": session.session_id,
            "projectName": session.project_name,
            "siteLabel": session.current_site or session.site_origin or session.project_name,
            "mode": session.mode,
            "editableQuarter": session.structure_map_summary.get("editable_quarter"),
            "activeNode": session.structure_map_summary.get("active_node"),
            "coverage": dict(session.coverage_summary or {}),
            "degradedReason": session.degraded_reason,
            "nodes": [
                {
                    "key": str(item.get("key", "")),
                    "title": str(item.get("title", "")),
                    "url": str(item.get("url", "")),
                    "parentKey": str(item.get("parent_key", "")),
                    "section_count": int(item.get("section_count", 0) or 0),
                    "quarter_number": item.get("quarter_number"),
                    "editable": bool(item.get("editable", False)),
                }
                for item in nodes
            ],
        }
        payload_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
        coverage = payload["coverage"]
        mode_label = "Complex Workspace" if session.mode == "complex_workspace" else "Review Only"
        editable_quarter = payload.get("editableQuarter")
        editable_text = f"Quarter {editable_quarter}" if editable_quarter not in (None, "") else "None"
        degraded_markup = (
            f"<p class='graph-alert'>{html.escape(session.degraded_reason)}</p>"
            if session.degraded_reason
            else ""
        )
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Website Structure Graph</title>
    <style>
      :root {{
        color-scheme: dark;
        --bg: #07111f;
        --panel: rgba(10, 18, 32, 0.88);
        --panel-strong: rgba(17, 24, 39, 0.94);
        --border: rgba(125, 211, 252, 0.18);
        --text: #e5eefc;
        --muted: #9fb4d1;
        --accent: #5eead4;
        --accent-2: #60a5fa;
        --warning: #fb7185;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100dvh;
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(94, 234, 212, 0.14), transparent 28%),
          radial-gradient(circle at top right, rgba(96, 165, 250, 0.16), transparent 30%),
          linear-gradient(180deg, #08111d 0%, #0d1728 100%);
      }}
      .graph-shell {{
        display: grid;
        gap: 20px;
        padding: 28px;
      }}
      .graph-header, .graph-panel {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: 0 24px 80px rgba(3, 7, 18, 0.34);
        backdrop-filter: blur(18px);
      }}
      .graph-header {{
        padding: 24px 26px;
        display: grid;
        gap: 16px;
      }}
      .graph-kicker {{
        margin: 0;
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.16em;
        font-size: 12px;
        font-weight: 700;
      }}
      .graph-title {{
        margin: 0;
        font-size: clamp(30px, 3.4vw, 52px);
        line-height: 0.95;
        font-weight: 700;
      }}
      .graph-summary {{
        margin: 0;
        color: var(--muted);
        font-size: 16px;
        line-height: 1.5;
        max-width: 72ch;
      }}
      .coverage-badges {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }}
      .coverage-badges span {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 10px 14px;
        border-radius: 999px;
        background: rgba(148, 163, 184, 0.1);
        border: 1px solid rgba(148, 163, 184, 0.16);
        font-size: 13px;
        color: var(--text);
      }}
      .graph-alert {{
        margin: 0;
        padding: 12px 14px;
        border-radius: 16px;
        border: 1px solid rgba(251, 113, 133, 0.26);
        background: rgba(127, 29, 29, 0.26);
        color: #fecdd3;
      }}
      .graph-layout {{
        display: grid;
        grid-template-columns: minmax(0, 1.9fr) minmax(280px, 0.9fr);
        gap: 20px;
      }}
      .graph-panel {{
        padding: 18px;
      }}
      .graph-panel h2 {{
        margin: 0 0 12px;
        font-size: 16px;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        color: var(--muted);
      }}
      .graph-canvas {{
        overflow: auto;
        border-radius: 18px;
        background: linear-gradient(180deg, rgba(8, 12, 20, 0.92), rgba(10, 16, 30, 0.96));
        border: 1px solid rgba(148, 163, 184, 0.12);
        min-height: 520px;
      }}
      .graph-canvas svg {{
        display: block;
        width: 100%;
        min-width: 960px;
        min-height: 520px;
      }}
      .graph-legend {{
        display: grid;
        gap: 12px;
      }}
      .graph-list {{
        list-style: none;
        margin: 0;
        padding: 0;
        display: grid;
        gap: 10px;
      }}
      .graph-list li {{
        padding: 12px 14px;
        border-radius: 16px;
        background: var(--panel-strong);
        border: 1px solid rgba(148, 163, 184, 0.12);
      }}
      .graph-list strong {{
        display: block;
        margin-bottom: 4px;
      }}
      .graph-list span {{
        display: block;
        color: var(--muted);
        font-size: 13px;
        line-height: 1.45;
        overflow-wrap: anywhere;
      }}
      @media (max-width: 1100px) {{
        .graph-layout {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="graph-shell">
      <section class="graph-header">
        <p class="graph-kicker">Live Navigator</p>
        <h1 class="graph-title">Website Structure Graph</h1>
        <p class="graph-summary">{html.escape(payload["siteLabel"])}. Mode: {html.escape(mode_label)}. Active node: {html.escape(str(payload.get("activeNode") or "Unconfirmed"))}. Editable quarter: {html.escape(editable_text)}.</p>
        <div class="coverage-badges">
          <span>Discovered {int(coverage.get("discovered_nodes", 0) or 0)}</span>
          <span>Indexed {int(coverage.get("indexed_nodes", 0) or 0)}</span>
          <span>Skipped {int(coverage.get("skipped_nodes", 0) or 0)}</span>
          <span>Blocked {int(coverage.get("blocked_nodes", 0) or 0)}</span>
          <span>Alias-collapsed {int(coverage.get("alias_collapsed_nodes", 0) or 0)}</span>
        </div>
        {degraded_markup}
      </section>
      <section class="graph-layout">
        <section class="graph-panel">
          <h2>Graph</h2>
          <div class="graph-canvas">
            <svg id="graph-root" viewBox="0 0 960 520" aria-label="Website structure graph"></svg>
          </div>
        </section>
        <aside class="graph-panel">
          <h2>Node Details</h2>
          <div class="graph-legend">
            <ul class="graph-list" id="node-list"></ul>
          </div>
        </aside>
      </section>
    </main>
    <script>
      const mapData = {payload_json};
      const svg = document.getElementById("graph-root");
      const nodeList = document.getElementById("node-list");
      const ns = "http://www.w3.org/2000/svg";
      const sourceNodes = Array.isArray(mapData.nodes) ? mapData.nodes : [];
      const sourceNodesByKey = new Map(sourceNodes.map((node) => [String(node.key || ""), node]));
      const roots = sourceNodes.filter((node) => !node.parentKey || !sourceNodesByKey.has(String(node.parentKey || "")));
      const editableQuarter = Number(mapData.editableQuarter || 0);
      const primaryRoots = roots.filter((node) => {{
        if (!editableQuarter) return true;
        const quarterNumber = Number(node.quarter_number || 0);
        if (!quarterNumber) return true;
        return quarterNumber === editableQuarter;
      }});
      const unattachedRoots = roots.filter((node) => !primaryRoots.includes(node));
      const nodes = sourceNodes.map((node) => ({{ ...node }}));
      if (unattachedRoots.length) {{
        const unattachedRootKeys = new Set(unattachedRoots.map((node) => String(node.key || "")));
        for (const node of nodes) {{
          if (unattachedRootKeys.has(String(node.key || ""))) {{
            node.parentKey = "__unattached_nodes__";
          }}
        }}
        nodes.push({{
          key: "__unattached_nodes__",
          title: "Unattached Nodes",
          url: "",
          parentKey: "",
          section_count: unattachedRoots.length,
          quarter_number: null,
          editable: false,
          syntheticBucket: true,
        }});
      }}
      const nodesByKey = new Map(nodes.map((node) => [String(node.key || ""), node]));
      const childrenByParent = new Map();
      for (const node of nodes) {{
        const parentKey = String(node.parentKey || "");
        if (!childrenByParent.has(parentKey)) {{
          childrenByParent.set(parentKey, []);
        }}
        childrenByParent.get(parentKey).push(node);
      }}
      for (const childList of childrenByParent.values()) {{
        childList.sort((left, right) => String(left.title || "").localeCompare(String(right.title || "")));
      }}
      const renderRoots = nodes.filter((node) => !node.parentKey || !nodesByKey.has(String(node.parentKey || "")));
      const horizontalGap = 260;
      const verticalGap = 120;
      let leafCursor = 0;
      let maxDepth = 0;
      const positions = new Map();
      const visit = (node, depth) => {{
        maxDepth = Math.max(maxDepth, depth);
        const children = childrenByParent.get(String(node.key || "")) || [];
        let y = 120;
        if (!children.length) {{
          y = 120 + leafCursor * verticalGap;
          leafCursor += 1;
        }} else {{
          const childYs = children.map((child) => visit(child, depth + 1));
          y = (childYs[0] + childYs[childYs.length - 1]) / 2;
        }}
        positions.set(String(node.key || ""), {{
          x: 140 + depth * horizontalGap,
          y,
          node,
          depth,
        }});
        return y;
      }};
      renderRoots.forEach((root) => visit(root, 0));
      const width = Math.max(960, (maxDepth + 1) * horizontalGap + 240);
      const height = Math.max(520, Math.max(1, leafCursor) * verticalGap + 120);
      svg.setAttribute("viewBox", `0 0 ${{width}} ${{height}}`);

      const defs = document.createElementNS(ns, "defs");
      defs.innerHTML = `
        <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow dx="0" dy="10" stdDeviation="12" flood-color="rgba(8, 145, 178, 0.28)" />
        </filter>
      `;
      svg.appendChild(defs);

      const groupLabels = new Map();
      for (const entry of positions.values()) {{
        if (entry.node.syntheticBucket) continue;
        const label = entry.node.quarter_number ? `Quarter ${{entry.node.quarter_number}}` : "General";
        if (!groupLabels.has(label) || entry.depth < groupLabels.get(label).depth) {{
          groupLabels.set(label, {{ x: entry.x, depth: entry.depth }});
        }}
      }}
      for (const [label, entry] of groupLabels.entries()) {{
        const title = document.createElementNS(ns, "text");
        title.setAttribute("x", String(entry.x));
        title.setAttribute("y", "54");
        title.setAttribute("text-anchor", "middle");
        title.setAttribute("fill", "#93c5fd");
        title.setAttribute("font-size", "16");
        title.setAttribute("font-weight", "700");
        title.textContent = label;
        svg.appendChild(title);
      }}

      nodes.forEach((node) => {{
        if (!node.parentKey) return;
        const from = positions.get(String(node.parentKey || ""));
        const to = positions.get(String(node.key || ""));
        if (!from || !to) return;
        const path = document.createElementNS(ns, "path");
        const midX = (from.x + to.x) / 2;
        path.setAttribute(
          "d",
          `M ${{from.x + 88}} ${{from.y}} C ${{midX}} ${{from.y}}, ${{midX}} ${{to.y}}, ${{to.x - 88}} ${{to.y}}`
        );
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "rgba(125, 211, 252, 0.24)");
        path.setAttribute("stroke-width", "3");
        path.setAttribute("stroke-linecap", "round");
        svg.appendChild(path);
      }});

      Array.from(positions.values())
        .sort((left, right) => left.y - right.y || left.depth - right.depth)
        .forEach((entry) => {{
        const {{ node, x, y }} = entry;
        const bucket = !!node.syntheticBucket;
        const active = String(node.title || "") === String(mapData.activeNode || "");
        const editable = !!node.editable;
        const group = document.createElementNS(ns, "g");
        group.setAttribute("filter", "url(#glow)");

        const rect = document.createElementNS(ns, "rect");
        rect.setAttribute("x", String(x - 88));
        rect.setAttribute("y", String(y - 36));
        rect.setAttribute("width", "176");
        rect.setAttribute("height", "74");
        rect.setAttribute("rx", "20");
        rect.setAttribute("fill", bucket ? "rgba(51, 65, 85, 0.9)" : active ? "rgba(14, 116, 144, 0.94)" : editable ? "rgba(15, 118, 110, 0.88)" : "rgba(15, 23, 42, 0.94)");
        rect.setAttribute("stroke", bucket ? "rgba(191, 219, 254, 0.5)" : active ? "#67e8f9" : editable ? "#5eead4" : "rgba(148, 163, 184, 0.28)");
        rect.setAttribute("stroke-width", active ? "3" : "2");
        if (bucket) rect.setAttribute("stroke-dasharray", "8 6");
        group.appendChild(rect);

        const title = document.createElementNS(ns, "text");
        title.setAttribute("x", String(x));
        title.setAttribute("y", String(y - 4));
        title.setAttribute("text-anchor", "middle");
        title.setAttribute("fill", "#f8fafc");
        title.setAttribute("font-size", "16");
        title.setAttribute("font-weight", "700");
        title.textContent = String(node.title || "Untitled");
        group.appendChild(title);

        const meta = document.createElementNS(ns, "text");
        meta.setAttribute("x", String(x));
        meta.setAttribute("y", String(y + 18));
        meta.setAttribute("text-anchor", "middle");
        meta.setAttribute("fill", bucket ? "#dbeafe" : editable ? "#ccfbf1" : "#cbd5e1");
        meta.setAttribute("font-size", "12");
        meta.textContent = bucket
          ? `${{node.section_count || 0}} disconnected roots`
          : editable
          ? `Editable · ${{node.section_count || 0}} sections`
          : `${{node.section_count || 0}} sections`;
        group.appendChild(meta);

        svg.appendChild(group);

        if (bucket) return;

        const item = document.createElement("li");
        const parentNode = nodesByKey.get(String(node.parentKey || ""));
        item.innerHTML = `
          <strong>${{String(node.title || "Untitled")}}</strong>
          <span>${{editable ? "Editable node" : "Reference node"}}${{node.quarter_number ? ` · Quarter ${{node.quarter_number}}` : ""}}${{parentNode ? ` · Parent: ${{String(parentNode.title || parentNode.key || "Unknown")}}` : ""}}</span>
          <span>${{String(node.url || "No URL captured")}}</span>
        `;
        nodeList.appendChild(item);
      }});

      if (!nodes.length) {{
        nodeList.innerHTML = "<li><strong>No indexed nodes yet.</strong><span>Run Index Site First before opening the graph view.</span></li>";
        const empty = document.createElementNS(ns, "text");
        empty.setAttribute("x", "480");
        empty.setAttribute("y", "260");
        empty.setAttribute("text-anchor", "middle");
        empty.setAttribute("fill", "#94a3b8");
        empty.setAttribute("font-size", "20");
        empty.textContent = "No structure manifest has been captured yet.";
        svg.appendChild(empty);
      }}
    </script>
  </body>
</html>"""

    def _review_page_html(session: SessionMemory) -> str:
        batch = session.review_batch
        if batch is None:
            body_markup = "<p class='review-empty'>No review is available yet. Run Index Site First and Refresh Review before opening this page.</p>"
            summary = "No review available."
            focus = session.current_site or session.project_name
            rationale_markup = ""
        else:
            rows = []
            for item in batch.items:
                rows.append(
                    "<tr>"
                    f"<td>{html.escape(str(item.title or item.page_hint or 'Recommendation'))}</td>"
                    f"<td>{html.escape(str(item.page_hint or 'Current page'))}</td>"
                    f"<td>{html.escape(str(item.current_value or 'Unconfirmed'))}</td>"
                    f"<td>{html.escape(str(item.recommended_value or item.recommended_range or 'Review manually'))}</td>"
                    f"<td>{html.escape(str(item.why_it_matters or ''))}</td>"
                    f"<td>{html.escape(str(item.evidence or ''))}</td>"
                    "</tr>"
                )
            body_markup = (
                "<table class='review-table'>"
                "<thead><tr><th>Recommendation</th><th>Page</th><th>Current value</th><th>Recommended</th><th>Why it matters</th><th>Evidence</th></tr></thead>"
                f"<tbody>{''.join(rows) or '<tr><td colspan=\"6\">No review items yet.</td></tr>'}</tbody>"
                "</table>"
            )
            summary = batch.summary or "Rendered Review"
            focus = batch.current_focus or session.current_site or session.project_name
            rationale_markup = "".join(f"<li>{html.escape(str(item))}</li>" for item in batch.rationale)
            rationale_markup = f"<ul class='review-rationale'>{rationale_markup}</ul>" if rationale_markup else ""
        return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Rendered Review</title>
    <style>
      :root {{
        color-scheme: dark;
        --bg: #08111d;
        --panel: rgba(9, 16, 28, 0.92);
        --border: rgba(96, 165, 250, 0.16);
        --text: #e2e8f0;
        --muted: #9fb4d1;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100dvh;
        background:
          radial-gradient(circle at top right, rgba(56, 189, 248, 0.14), transparent 28%),
          linear-gradient(180deg, #07111f 0%, #0c1728 100%);
        color: var(--text);
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      }}
      main {{
        max-width: 1400px;
        margin: 0 auto;
        padding: 28px;
        display: grid;
        gap: 18px;
      }}
      .review-panel {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: 0 24px 80px rgba(3, 7, 18, 0.34);
        backdrop-filter: blur(18px);
        padding: 24px;
      }}
      .review-kicker {{
        margin: 0 0 8px;
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.16em;
        font-size: 12px;
        font-weight: 700;
      }}
      h1 {{
        margin: 0 0 10px;
        font-size: clamp(30px, 3vw, 48px);
        line-height: 0.96;
      }}
      .review-summary, .review-focus {{
        margin: 0;
        color: var(--muted);
        line-height: 1.5;
      }}
      .review-focus {{
        color: var(--text);
        font-weight: 700;
      }}
      .review-rationale {{
        margin: 14px 0 0;
        padding-left: 18px;
        display: grid;
        gap: 8px;
      }}
      .review-table {{
        width: 100%;
        border-collapse: collapse;
      }}
      .review-table th, .review-table td {{
        padding: 14px 12px;
        border-bottom: 1px solid rgba(148, 163, 184, 0.12);
        text-align: left;
        vertical-align: top;
      }}
      .review-table th {{
        color: #bfdbfe;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}
      .review-empty {{
        margin: 0;
        color: var(--muted);
      }}
      @media (max-width: 900px) {{
        main {{ padding: 16px; }}
        .review-panel {{ padding: 18px; overflow: auto; }}
        .review-table {{ min-width: 780px; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="review-panel">
        <p class="review-kicker">Live Navigator</p>
        <h1>Rendered Review</h1>
        <p class="review-focus">{html.escape(str(focus))}</p>
        <p class="review-summary">{html.escape(str(summary))}</p>
        {rationale_markup}
      </section>
      <section class="review-panel">
        {body_markup}
      </section>
    </main>
  </body>
</html>"""

    async def _run_index(session: SessionMemory) -> SessionMemory:
        if session.status == "indexing":
            raise HTTPException(status_code=409, detail="This session is already indexing. Wait for the current crawl to finish or stop it first.")
        app.state.overlay_site_check_preview[session.session_id] = {}
        session = app.state.navigator_runtime.companion.mark_indexing_progress(
            session.session_id,
            "Checking the current structure fingerprint against saved local memory.",
            site_check_details={},
        )
        await _sync_overlay(
            await _panel_for_session(
                session,
                "The agent is controlling the browser and indexing visible workflow areas.",
                progress=8,
                current_step="Checking the current structure fingerprint against saved local memory.",
            )
        )

        async def _index_progress(step: str, progress: int, site_check_details: dict[str, Any] | None = None) -> None:
            if site_check_details is not None:
                app.state.overlay_site_check_preview[session.session_id] = _site_check_details_payload(site_check_details)
            latest = app.state.navigator_runtime.companion.mark_indexing_progress(
                session.session_id,
                step,
                site_check_details=app.state.overlay_site_check_preview.get(session.session_id, {}),
            )
            await _sync_overlay(
                await _panel_for_session(
                    latest,
                    "The agent is controlling the browser and indexing visible workflow areas.",
                    progress=progress,
                    current_step=step,
                    site_check_details=app.state.overlay_site_check_preview.get(session.session_id, {}),
                )
            )

        observation = await app.state.navigator_runtime.bridge.capture_site_index(
            session_id=session.session_id,
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            index_mode=session.index_mode,
            prior_actions=session.action_history,
            progress_callback=_index_progress,
        )
        observation = ObservationPacket.model_validate(observation)
        if app.state.remote_client is not None:
            indexed_payload = await app.state.remote_client.index_site(session.session_id, observation)
            indexed_session = SessionMemory.model_validate(indexed_payload)
        else:
            indexed_session = await app.state.navigator_runtime.companion.index_site(session.session_id, observation)
        app.state.active_session_id = indexed_session.session_id
        await app.state.navigator_runtime.bridge.focus_active_page()
        if indexed_session.degraded_reason:
            app.state.overlay_site_check_preview.pop(session.session_id, None)
            await _sync_overlay(
                await _panel_for_session(
                    indexed_session,
                    indexed_session.degraded_reason,
                    progress=100,
                    current_step="Coverage degraded.",
                )
            )
            return indexed_session
        await _sync_overlay(
            await _panel_for_session(
                indexed_session,
                "Index complete. Preparing the full review for the current workflow.",
                progress=96,
                current_step="Preparing the detailed review from indexed context.",
            )
        )
        await app.state.navigator_runtime.companion.prepare_review_batch(indexed_session.session_id, indexed_session.last_observation)
        reviewed_session = await _get_session_memory(indexed_session.session_id) or indexed_session
        app.state.overlay_site_check_preview.pop(session.session_id, None)
        await _sync_overlay(
            await _panel_for_session(
                reviewed_session,
                "Review ready. Read the summary first, then activate Live Notes or apply manually.",
                progress=100,
                current_step="Review ready.",
            )
        )
        return reviewed_session

    async def _refresh_live_advice(session: SessionMemory, *, force: bool = False) -> dict[str, Any]:
        await _sync_overlay(
            await _panel_for_session(
                session,
                "Preparing live notes for the current page.",
                progress=24,
                watch_mode="live_advice",
                current_step="Reading the current page so the review can be matched to visible controls.",
            )
        )
        observation = await app.state.navigator_runtime.bridge.capture_observation(
            session_id=session.session_id,
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            prior_actions=session.action_history,
        )
        observation = ObservationPacket.model_validate(observation)
        signature = str(observation.browser_metadata.get("page_signature", ""))
        ignored = False
        if not force and not app.state.navigator_runtime.companion.update_page_signature(session.session_id, signature):
            ignored = True
            refreshed = app.state.navigator_runtime.companion.get_session(session.session_id) or session
        else:
            refreshed = await app.state.navigator_runtime.companion.refresh_live_advice_from_review(session.session_id, observation)
        await _sync_overlay(
            await _panel_for_session(
                refreshed,
                "Live notes are following the current page.",
                progress=100,
                watch_mode="live_advice",
                current_step="Matched the current page against the saved review notes.",
            )
        )
        payload = refreshed.model_dump(mode="json")
        payload["ok"] = True
        payload["ignored"] = ignored
        return payload

    async def _prepare_review_batch(session: SessionMemory) -> dict[str, Any]:
        observation = await app.state.navigator_runtime.bridge.capture_observation(
            session_id=session.session_id,
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            prior_actions=session.action_history,
        )
        observation = ObservationPacket.model_validate(observation)
        batch = await app.state.navigator_runtime.companion.prepare_review_batch(session.session_id, observation)
        refreshed = app.state.navigator_runtime.companion.get_session(session.session_id) or session
        await _sync_overlay(
            await _panel_for_session(
                refreshed,
                "Detailed review ready. Read it first, then activate Live Notes or apply manually.",
                current_step="Prepared a quarter-wide review from indexed context.",
            )
        )
        return batch.model_dump(mode="json")

    async def _apply_review_batch(session: SessionMemory) -> dict[str, Any]:
        if session.review_batch is None or not session.review_batch.apply_ready or not session.review_batch.actions:
            raise HTTPException(status_code=409, detail="Apply is beta and no executable review batch is ready. Manual application is safer for this review.")
        app.state.navigator_runtime.companion.auto_approve_executable_actions(session.session_id)
        refreshed = app.state.navigator_runtime.companion.get_session(session.session_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Session not found")
        approved = [item for item in refreshed.pending_approvals if item.status == "approved"]
        if not approved:
            raise HTTPException(status_code=409, detail="Apply is beta and the current review only contains confirmation-gated actions. Approve them explicitly before execution.")
        refreshed = app.state.navigator_runtime.companion.mark_applying_batch(session.session_id)
        await _sync_overlay(
            await _panel_for_session(
                refreshed,
                "Applying the prepared review batch locally. Apply is beta, manual application is safer.",
                current_step="Executing the grouped change set.",
            )
        )
        results = await app.state.navigator_runtime.bridge.execute_actions(approved)
        await _record_execution(ExecuteResultPayload(session_id=session.session_id, results=results))
        final_session = app.state.navigator_runtime.companion.finalize_review_batch(session.session_id)
        await _sync_overlay(
            await _panel_for_session(
                final_session,
                "Review batch applied. Apply is beta, manual application is still safer for critical fields.",
                current_step="Batch complete.",
            )
        )
        return final_session.model_dump(mode="json")

    async def _handle_overlay_command(message: dict[str, Any]) -> dict[str, Any]:
        request = OverlayCommandRequest.model_validate(message)
        payload = request.payload

        async def _start_session() -> dict[str, Any]:
            session = await app.state.navigator_runtime.companion.create_session(CreateSessionRequest(**payload))
            app.state.active_session_id = session.session_id
            app.state.overlay_map_open = False
            app.state.overlay_sessions_open = False
            app.state.overlay_review_open = False
            await app.state.navigator_runtime.bridge.focus_active_page()
            await _sync_overlay(await _panel_for_session(session, "Session created. Run the site index first."))
            return {"ok": True, "session_id": session.session_id}

        async def _resume_session() -> dict[str, Any]:
            session_id = str(payload.get("session_id", "")).strip()
            session = app.state.navigator_runtime.companion.resume_session(session_id)
            app.state.active_session_id = session.session_id
            app.state.overlay_map_open = False
            app.state.overlay_sessions_open = False
            app.state.overlay_review_open = False
            await app.state.navigator_runtime.bridge.focus_active_page()
            await _sync_overlay(await _panel_for_session(session, _resume_status_message(session)))
            return {"ok": True, "session_id": session.session_id}

        setup_handlers = {
            "start_session": _start_session,
            "resume_session": _resume_session,
        }
        if request.command in setup_handlers:
            return await setup_handlers[request.command]()

        session_id = str(payload.get("session_id") or app.state.active_session_id or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        async def _start_index() -> dict[str, Any]:
            indexed = await _run_index(session)
            return {"ok": True, "session_id": indexed.session_id}

        async def _enter_live_advice() -> dict[str, Any]:
            if not _site_ready(session):
                raise HTTPException(status_code=409, detail="Index the site first before enabling Live Notes.")
            app.state.navigator_runtime.companion.enter_live_advice_mode(session_id)
            return await _refresh_live_advice(session, force=True)

        async def _page_changed() -> dict[str, Any]:
            refreshed = await _get_session_memory(session_id)
            if refreshed is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if refreshed.status != "live_advice":
                return {"ok": True, "ignored": True}
            signature = str(payload.get("page_signature", "")).strip()
            if not signature:
                return await _refresh_live_advice(refreshed, force=True)
            if not app.state.navigator_runtime.companion.update_page_signature(session_id, signature):
                return {"ok": True, "ignored": True}
            return await _refresh_live_advice(refreshed, force=True)

        async def _prepare_review_batch_command() -> dict[str, Any]:
            if not _site_ready(session):
                raise HTTPException(status_code=409, detail="Index the site first before preparing a review batch.")
            batch = await _prepare_review_batch(session)
            return {"ok": True, **batch}

        async def _apply_review_batch_command() -> dict[str, Any]:
            if session.review_batch is None and not session.pending_approvals:
                raise HTTPException(status_code=409, detail="Prepare a review batch first.")
            updated = await _apply_review_batch(session)
            return {"ok": True, **updated}

        async def _open_map() -> dict[str, Any]:
            return {"ok": True, "map_url": _map_url(session.session_id)}

        async def _open_sessions() -> dict[str, Any]:
            app.state.overlay_sessions_open = not bool(app.state.overlay_sessions_open)
            status = "Saved sessions are visible in the rail." if app.state.overlay_sessions_open else "Saved sessions are hidden."
            await _sync_overlay(await _panel_for_session(session, status))
            return {"ok": True}

        async def _open_review() -> dict[str, Any]:
            return {"ok": True, "review_url": _review_url(session.session_id)}

        async def _show_setup() -> dict[str, Any]:
            app.state.overlay_map_open = False
            app.state.overlay_sessions_open = False
            app.state.overlay_review_open = False
            await _sync_setup_overlay("Start a new session or resume another one.", active_session=session)
            return {"ok": True, "active_session_id": session.session_id}

        async def _stop_session() -> dict[str, Any]:
            app.state.active_session_id = None
            await app.state.navigator_runtime.bridge.clear_agent_overlay()
            await _sync_setup_overlay("Session stopped. Start or resume a session when you are ready.")
            return {"ok": True}

        session_handlers = {
            "start_index": _start_index,
            "enter_live_advice": _enter_live_advice,
            "page_changed": _page_changed,
            "prepare_review_batch": _prepare_review_batch_command,
            "apply_review_batch": _apply_review_batch_command,
            "open_map": _open_map,
            "open_sessions": _open_sessions,
            "open_review": _open_review,
            "show_setup": _show_setup,
            "stop_session": _stop_session,
        }
        handler = session_handlers.get(request.command)
        if handler is not None:
            return await handler()

        raise HTTPException(status_code=400, detail=f"Unsupported command: {request.command}")

    app.state.navigator_runtime.bridge.register_command_handler(_handle_overlay_command)

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/architecture")
    async def architecture() -> FileResponse:
        return FileResponse(STATIC_DIR / "architecture.html")

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/bootstrap-overlay")
    async def bootstrap_overlay(request: Request) -> dict[str, Any]:
        try:
            _remember_ui_base_url(request)
            await app.state.navigator_runtime.bridge.bootstrap_overlay()
            await app.state.navigator_runtime.bridge.close_local_ui_tabs(str(request.base_url).rstrip("/"))
            await _sync_setup_overlay()
            await app.state.navigator_runtime.bridge.focus_active_page()
            return {"ok": True}
        except Exception as exc:
            message = str(exc) or "Open a target website tab in the controlled Chrome window first, then retry the overlay."
            return {"ok": False, "message": message}

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.post("/api/overlay/command")
    async def overlay_command(http_request: Request, request: OverlayCommandRequest) -> dict[str, Any]:
        _remember_ui_base_url(http_request)
        return await _handle_overlay_command(request.model_dump(mode="json"))

    @app.get("/map", response_class=HTMLResponse)
    async def map_view(session_id: str) -> HTMLResponse:
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return HTMLResponse(_map_page_html(session))

    @app.get("/review", response_class=HTMLResponse)
    async def review_view(session_id: str) -> HTMLResponse:
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return HTMLResponse(_review_page_html(session))

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        sessions = await _list_sessions_payload()
        active_session = None
        if app.state.active_session_id:
            active_session = await _get_session_memory(app.state.active_session_id)
        return {
            "kill_switch_active": kill_switch.KILL_SWITCH_ACTIVE,
            "consecutive_executor_failures": kill_switch.CONSECUTIVE_EXECUTOR_FAILURES,
            "session_count": len(sessions),
            "cloud_backend_url": SETTINGS.cloud_backend_url,
            "gcp_project_id": SETTINGS.gcp_project_id,
            "use_cloud_backend": SETTINGS.use_cloud_backend,
            "gemini_index_model": SETTINGS.gemini_index_model,
            "gemini_live_model": SETTINGS.gemini_live_model,
            "active_session_id": app.state.active_session_id,
            "active_session_status": active_session.status if active_session is not None else "",
            "active_session_mode": active_session.mode if active_session is not None else "",
        }

    @app.post("/api/stop")
    async def stop() -> dict[str, Any]:
        app.state.active_session_id = None
        app.state.overlay_map_open = False
        app.state.overlay_sessions_open = False
        app.state.overlay_review_open = False
        await app.state.navigator_runtime.bridge.clear_agent_overlay()
        await _sync_setup_overlay("Session stopped. Start or resume a session when you are ready.")
        return {"status": "stopped"}

    @app.post("/api/kill-switch")
    async def kill() -> dict[str, Any]:
        kill_switch.activate_kill_switch("manual_api_request")
        return {"status": "kill_switch_active"}

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": await _list_sessions_payload()}

    @app.post("/api/sessions")
    async def create_session(request: CreateSessionRequest) -> dict[str, Any]:
        session = await app.state.navigator_runtime.companion.create_session(request)
        app.state.active_session_id = session.session_id
        app.state.overlay_map_open = False
        app.state.overlay_sessions_open = False
        app.state.overlay_review_open = False
        await _sync_overlay(await _panel_for_session(session, "Session created. Run the site index first."))
        await app.state.navigator_runtime.bridge.focus_active_page()
        return session.model_dump(mode="json")

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session.model_dump(mode="json")

    @app.post("/api/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> dict[str, Any]:
        session = app.state.navigator_runtime.companion.resume_session(session_id)
        app.state.active_session_id = session.session_id
        app.state.overlay_map_open = False
        app.state.overlay_sessions_open = False
        app.state.overlay_review_open = False
        await _sync_overlay(await _panel_for_session(session, _resume_status_message(session)))
        await app.state.navigator_runtime.bridge.focus_active_page()
        return session.model_dump(mode="json")

    @app.post("/api/observe")
    async def observe(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        observation = await app.state.navigator_runtime.bridge.capture_observation(
            session_id=session.session_id,
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            prior_actions=session.action_history,
        )
        observation = ObservationPacket.model_validate(observation)
        stored = app.state.navigator_runtime.companion.store_observation(observation)
        return stored.model_dump(mode="json")

    @app.post("/api/index-site")
    async def index_site(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        indexed_session = await _run_index(session)
        return indexed_session.model_dump(mode="json")

    @app.post("/api/live-advice/start")
    async def live_advice_start(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if not _site_ready(session):
            raise HTTPException(status_code=409, detail="Index the site first before enabling Live Notes.")
        app.state.navigator_runtime.companion.enter_live_advice_mode(session_id)
        return await _refresh_live_advice(session, force=True)

    @app.post("/api/plan")
    async def plan(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if not _site_ready(session):
            raise HTTPException(status_code=409, detail="Index the site first before requesting review-derived notes.")
        observation = payload.get("observation")
        packet = ObservationPacket.model_validate(observation) if observation else None
        response = await app.state.navigator_runtime.companion.plan(session_id, packet)
        refreshed = app.state.navigator_runtime.companion.get_session(session_id)
        if refreshed is not None:
            await _sync_overlay(await _panel_for_session(refreshed, "Planning the next grounded steps."))
        return response.model_dump(mode="json")

    @app.post("/api/review-batch")
    async def review_batch(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if not _site_ready(session):
            raise HTTPException(status_code=409, detail="Index the site first before preparing a review batch.")
        return await _prepare_review_batch(session)

    @app.post("/api/review-batch/apply")
    async def review_batch_apply(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return await _apply_review_batch(session)

    @app.post("/api/sessions/{session_id}/approve")
    async def approve(session_id: str, payload: ApprovalRequest) -> dict[str, Any]:
        selected = app.state.navigator_runtime.companion.approve_actions(session_id, payload.action_ids)
        return {"session_id": session_id, "approved_actions": selected}

    @app.post("/api/execute-approved")
    async def execute_approved(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        approved = [item for item in session.pending_approvals if item.status == "approved"]
        await _sync_overlay(await _panel_for_session(session, "Executing approved actions locally."))
        results = await app.state.navigator_runtime.bridge.execute_actions(approved)
        updated = await _record_execution(ExecuteResultPayload(session_id=session_id, results=results))
        refreshed = app.state.navigator_runtime.companion.get_session(session_id)
        if refreshed is not None:
            await _sync_overlay(await _panel_for_session(refreshed, "Agent is ready for the next step."))
        return updated

    @app.post("/api/autonomous-step")
    async def autonomous_step(payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", "")).strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        session = await _get_session_memory(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if not _site_ready(session):
            raise HTTPException(status_code=409, detail="Index the site first before enabling autonomous execution.")
        observation = await app.state.navigator_runtime.bridge.capture_observation(
            session_id=session.session_id,
            active_goal=session.goal.raw_goal,
            domain_pack=session.domain_pack,
            safety_mode=session.goal.safety_mode,
            prior_actions=session.action_history,
        )
        observation = ObservationPacket.model_validate(observation)
        await app.state.navigator_runtime.companion.plan(session_id, observation)
        app.state.navigator_runtime.companion.auto_approve_executable_actions(session_id)
        refreshed = await _get_session_memory(session_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Session not found")
        approved = [ActionProposal.model_validate(item) for item in refreshed.pending_approvals if item.status == "approved"]
        await _sync_overlay(await _panel_for_session(refreshed, "Autonomous mode is applying approved actions."))
        results = await app.state.navigator_runtime.bridge.execute_actions(approved)
        updated = await _record_execution(ExecuteResultPayload(session_id=session_id, results=results))
        final_session = app.state.navigator_runtime.companion.get_session(session_id)
        if final_session is not None:
            await _sync_overlay(await _panel_for_session(final_session, "Autonomous step complete."))
        return updated

    return app


app = create_app()
