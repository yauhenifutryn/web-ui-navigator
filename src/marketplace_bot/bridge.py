from __future__ import annotations

import asyncio
import base64
import html
import inspect
import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen
from weakref import WeakKeyDictionary

from marketplace_bot.agents.crawler import Crawler
from marketplace_bot.ax_snapshot import AxSnapshot, AxSnapshotProvider, build_ax_snapshot_provider
from marketplace_bot.config import SETTINGS
from marketplace_bot.logging_json import log_event
from marketplace_bot.navigator_models import ActionProposal, ObservationPacket
from marketplace_bot.site_intelligence import (
    analyze_site_change,
    build_site_memory_key,
    build_structure_manifest,
    choose_index_strategy,
    compute_site_fingerprint,
    compute_structure_digest,
    merge_site_index,
    normalize_site_origin,
)
from marketplace_bot.site_memory_repository import HybridSiteMemoryRepository
from marketplace_bot.state_store import StateStore, utc_now_iso


logger = logging.getLogger(__name__)

OVERLAY_INLINE_NOTE_WIDTH_PX = 260
OVERLAY_RAIL_WIDTH_PX = 360
OVERLAY_DOCK_WIDTH_PX = 520
OVERLAY_AGENT_CURSOR_RIGHT_PX = 435


class LocalBrowserBridge:
    def __init__(
        self,
        state_store: StateStore,
        cdp_url: str,
        target_domain: str,
        artifact_store: Any | None = None,
        site_memory_repository: HybridSiteMemoryRepository | None = None,
        ax_snapshot_provider: AxSnapshotProvider | None = None,
        ax_snapshots_enabled: bool | None = None,
        ax_occlusion_mode: str | None = None,
    ) -> None:
        self.state_store = state_store
        self.cdp_url = cdp_url
        self.target_domain = target_domain
        self.artifact_store = artifact_store
        self.site_memory_repository = site_memory_repository
        self.ax_snapshots_enabled = SETTINGS.ax_snapshots_enabled if ax_snapshots_enabled is None else ax_snapshots_enabled
        self.ax_occlusion_mode = (ax_occlusion_mode or SETTINGS.ax_occlusion_mode).strip().lower()
        self.ax_snapshot_provider = ax_snapshot_provider or build_ax_snapshot_provider(
            SETTINGS.ax_provider_preference,
            max_nodes_index=SETTINGS.ax_max_nodes_index,
            max_nodes_live=SETTINGS.ax_max_nodes_live,
            max_nodes_verify=SETTINGS.ax_max_nodes_verify,
        )
        self._crawler: Crawler | None = None
        self._controller_lock = asyncio.Lock()
        self._page_runtime_tokens: WeakKeyDictionary[Any, str] = WeakKeyDictionary()
        self._bound_pages: set[str] = set()
        self._navigation_hooked_pages: set[str] = set()
        self._command_handler: Any | None = None
        self._last_panel: dict[str, Any] | None = None
        self._last_target_hint: str = ""
        self._overlay_session_id: str = ""
        self._site_index_tasks: dict[str, asyncio.Task[ObservationPacket]] = {}
        self._site_index_tasks_lock = asyncio.Lock()
        self._site_index_serial_lock = asyncio.Lock()

    @staticmethod
    def _is_local_url(url: str) -> bool:
        host = urlparse(url).netloc
        return host.startswith("127.0.0.1") or host.startswith("localhost")

    def register_command_handler(self, handler: Any) -> None:
        self._command_handler = handler

    async def bootstrap_overlay(self) -> None:
        await self._ensure_controller()
        page = await self._get_target_page()
        await self._ensure_page_runtime(page)
        log_event("bridge", "bootstrap_overlay_ready", url=getattr(page, "url", ""))

    async def capture_observation(
        self,
        session_id: str,
        active_goal: str,
        domain_pack: str,
        safety_mode: str,
        prior_actions: list[dict[str, Any]] | None = None,
    ) -> ObservationPacket:
        crawler = await self._ensure_controller()
        page = await self._get_target_page()
        observation = await self._build_observation(
            page=page,
            crawler=crawler,
            session_id=session_id,
            active_goal=active_goal,
            domain_pack=domain_pack,
            safety_mode=safety_mode,
            prior_actions=prior_actions,
            browser_metadata={"target_domain": self.target_domain},
            ax_capture_mode="live",
        )
        log_event("bridge", "observation_captured", session_id=session_id, url=observation.page_url, title=observation.page_title)
        return observation

    async def capture_site_index(
        self,
        session_id: str,
        active_goal: str,
        domain_pack: str,
        safety_mode: str,
        index_mode: str = "adaptive",
        prior_actions: list[dict[str, Any]] | None = None,
        progress_callback: Any | None = None,
    ) -> ObservationPacket:
        async with self._site_index_tasks_lock:
            existing_task = self._site_index_tasks.get(session_id)
            if existing_task is not None and not existing_task.done():
                log_event("bridge", "site_index_joined_existing_run", session_id=session_id)
                task = existing_task
            else:
                task = asyncio.create_task(
                    self._capture_site_index_once(
                        session_id=session_id,
                        active_goal=active_goal,
                        domain_pack=domain_pack,
                        safety_mode=safety_mode,
                        index_mode=index_mode,
                        prior_actions=prior_actions,
                        progress_callback=progress_callback,
                    )
                )
                self._site_index_tasks[session_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            async with self._site_index_tasks_lock:
                if self._site_index_tasks.get(session_id) is task:
                    self._site_index_tasks.pop(session_id, None)

    async def _capture_site_index_once(
        self,
        session_id: str,
        active_goal: str,
        domain_pack: str,
        safety_mode: str,
        index_mode: str = "adaptive",
        prior_actions: list[dict[str, Any]] | None = None,
        progress_callback: Any | None = None,
    ) -> ObservationPacket:
        async with self._site_index_serial_lock:
            crawler = await self._ensure_controller()
            page = await self._get_target_page()
            starting_url = page.url
            if progress_callback is not None:
                await progress_callback("Reading the current page and visible navigation.", 12)
            probe = await self._build_site_probe(page, crawler, domain_pack)
            existing_memory = None
            if self.site_memory_repository is not None:
                existing_memory = self.site_memory_repository.get_by_origin(domain_pack, str(probe.get("site_origin", "")))
            change_report = analyze_site_change(existing_memory, probe)
            strategy = choose_index_strategy(
                index_mode,
                existing_memory.model_dump(mode="json") if existing_memory is not None else None,
                change_report,
            )
            if progress_callback is not None:
                await progress_callback(
                    f"Fingerprint check complete. Strategy selected: {strategy}.",
                    28,
                    {**change_report, "strategy": strategy},
                )

            if domain_pack == "marketplace_simulation" and strategy == "full":
                if progress_callback is not None:
                    await progress_callback("Opening quarter navigation and indexing the full simulation workspace.", 44)
                site_index = await crawler.scrape_completed_quarters_world_state(page)
                source = "simulation_full_crawl"
            elif domain_pack != "marketplace_simulation" and strategy == "full":
                if progress_callback is not None:
                    await progress_callback("Exploring the current site and linked pages that look relevant to the goal.", 44)
                site_index = await self._explore_generic_site_index(page, crawler, active_goal, max_pages=12)
                source = "generic_goal_crawl"
            elif strategy == "lightweight":
                if progress_callback is not None:
                    await progress_callback("Running a lightweight scan of the current page and the closest relevant links.", 44)
                if domain_pack == "marketplace_simulation":
                    site_index = await self._lightweight_site_index(page, crawler, domain_pack)
                    source = "lightweight_probe"
                else:
                    site_index = await self._explore_generic_site_index(page, crawler, active_goal, max_pages=3)
                    source = "generic_lightweight_crawl"
            else:
                if progress_callback is not None:
                    await progress_callback("Refreshing the current page and nearby linked workflow areas.", 44)
                if domain_pack == "marketplace_simulation":
                    site_index = await self._partial_site_index(page, crawler, domain_pack)
                    source = "partial_refresh" if existing_memory is not None else "generic_site_scan"
                else:
                    site_index = await self._explore_generic_site_index(page, crawler, active_goal, max_pages=7)
                    source = "generic_partial_refresh" if existing_memory is not None else "generic_site_scan"

            if page.url != starting_url:
                try:
                    await page.goto(starting_url, wait_until="domcontentloaded", timeout=15000)
                    await self._crawler_wait_after_navigation(crawler, page)
                except Exception:
                    logger.warning("Failed to restore the starting page after site indexing.", exc_info=True)

            previous_site_index = {}
            if existing_memory is not None and strategy != "full":
                previous_site_index = existing_memory.indexed_context.get("site_index", {})
            merged_site_index = merge_site_index(previous_site_index, site_index)
            structure_manifest = build_structure_manifest(merged_site_index)
            merged_site_index = {**merged_site_index, "structure_manifest": structure_manifest}
            site_origin = str(probe.get("site_origin", normalize_site_origin(page.url)))
            memory_key = build_site_memory_key(domain_pack, site_origin)
            site_fingerprint = compute_site_fingerprint(site_origin, merged_site_index)
            structure_digest = compute_structure_digest(merged_site_index)
            if progress_callback is not None:
                await progress_callback("Merging reusable local memory with the latest visible changes.", 78)

            browser_metadata = {
                "target_domain": self.target_domain,
                "site_index": {**merged_site_index, "source": source},
                "site_check": {
                    **change_report,
                    "strategy": strategy,
                    "site_origin": site_origin,
                    "memory_key": memory_key,
                    "site_fingerprint": site_fingerprint,
                    "structure_digest": structure_digest,
                    "index_mode": index_mode,
                    "reused_memory": bool(existing_memory),
                },
                "site_memory_context": {
                    "memory_key": existing_memory.memory_key,
                    "strategic_summary": existing_memory.strategic_summary,
                    "indexed_context": existing_memory.indexed_context,
                    "site_origin": existing_memory.site_origin,
                    "index_mode": existing_memory.index_mode,
                } if existing_memory is not None else {},
            }

            observation = await self._build_observation(
                page=page,
                crawler=crawler,
                session_id=session_id,
                active_goal=active_goal,
                domain_pack=domain_pack,
                safety_mode=safety_mode,
                prior_actions=prior_actions,
                visible_text_override=str(site_index.get("semantic_text", "")),
                dom_summary_override=str(site_index.get("semantic_text", "")),
                browser_metadata=browser_metadata,
                ax_capture_mode="index",
            )
            if progress_callback is not None:
                await progress_callback("Indexing summary is ready.", 94)
            log_event(
                "bridge",
                "site_index_captured",
                session_id=session_id,
                domain_pack=domain_pack,
                source=source,
                index_mode=index_mode,
                change_status=change_report.get("change_status"),
                strategy=strategy,
            )
            return observation

    async def execute_actions(self, actions: list[ActionProposal]) -> list[dict[str, Any]]:
        crawler = await self._ensure_controller()
        page = await self._get_target_page()
        results: list[dict[str, Any]] = []
        for action in actions:
            result = await self._execute_single(page, action)
            results.append(result)
            if action.action == "stop":
                break
        return results

    async def sync_agent_overlay(self, panel: dict[str, Any]) -> None:
        self._last_panel = dict(panel)
        explicit_target_hint = str(panel.get("target_hint", "") or "").strip()
        if explicit_target_hint:
            self._last_target_hint = explicit_target_hint
        else:
            self._last_target_hint = str(panel.get("current_site") or panel.get("site_origin") or self._last_target_hint)
        self._overlay_session_id = str(panel.get("session_id") or self._overlay_session_id)
        page = await self._get_target_page(prefer_url=self._last_target_hint)
        await self._ensure_page_runtime(page)
        page_url = str(getattr(page, "url", "") or "")
        if page_url.startswith("http") and not self._is_local_url(page_url):
            self._last_target_hint = page_url
        overlay_script = (
            r"""
            ({ html, css, stage, panel }) => {
              const rootId = "__live_navigator_overlay_root__";
              let root = document.getElementById(rootId);
              if (!root) {
                root = document.createElement("div");
                root.id = rootId;
                document.documentElement.appendChild(root);
              }
              const shadow = root.shadowRoot || root.attachShadow({ mode: "open" });
              shadow.innerHTML = `<style>${css}</style>${html}`;
              root.setAttribute("data-stage", stage || "idle");
              const shell = shadow.querySelector(".ln-overlay-shell");

              const clearInlineNotes = () => {
                document.querySelectorAll(".__live_navigator_inline_note__").forEach((node) => node.remove());
              };

              const escapeHtml = (value) => {
                return String(value ?? "").replace(/[&<>"']/g, (char) => (
                  {
                    "&": "&amp;",
                    "<": "&lt;",
                    ">": "&gt;",
                    '"': "&quot;",
                    "'": "&#39;",
                  }[char] || char
                ));
              };

              const findAnchor = (note) => {
                const target = String(note.anchor_text || note.field_label || note.page_hint || "").trim().toLowerCase();
                if (!target) return null;
                const selectors = 'table, tr, th, td, label, h1, h2, h3, h4, button, a, p, div, span';
                return Array.from(document.querySelectorAll(selectors)).find((el) => {
                  const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
                  return text && text.includes(target);
                }) || null;
              };

              const renderInlineNotes = (notes = []) => {
                clearInlineNotes();
                const noteWidth = __INLINE_NOTE_WIDTH__;
                const noteHeight = 120;
                const placedNotes = [];
                for (const note of notes.slice(0, 3)) {
                  const anchor = findAnchor(note);
                  if (!anchor) continue;
                  const rect = anchor.getBoundingClientRect();
                  const maxTop = Math.max(12, window.innerHeight - noteHeight);
                  const maxLeft = Math.max(12, window.innerWidth - noteWidth - 12);
                  const candidateLefts = [
                    Math.max(12, Math.min(maxLeft, rect.right + 12)),
                    Math.max(12, Math.min(maxLeft, rect.left - noteWidth - 12)),
                  ];
                  let top = Math.max(12, Math.min(maxTop, rect.top + 8));
                  let left = candidateLefts[0];
                  for (const candidateLeft of candidateLefts) {
                    left = candidateLeft;
                    top = Math.max(12, Math.min(maxTop, rect.top + 8));
                    let attempts = 0;
                    while (placedNotes.some((placed) => Math.abs(placed.left - left) < noteWidth - 24 && Math.abs(placed.top - top) < noteHeight - 18)) {
                      if (top >= maxTop) break;
                      top = Math.min(maxTop, top + noteHeight + 12);
                      attempts += 1;
                      if (attempts > 4) break;
                    }
                    if (!placedNotes.some((placed) => Math.abs(placed.left - left) < noteWidth - 24 && Math.abs(placed.top - top) < noteHeight - 18)) {
                      break;
                    }
                  }
                  const host = document.createElement("div");
                  host.className = "__live_navigator_inline_note__";
                  placedNotes.push({ top, left });
                  host.style.cssText = [
                    "position:fixed",
                    `top:${top}px`,
                    `left:${left}px`,
                    `width:${noteWidth}px`,
                    "z-index:2147483646",
                    "pointer-events:none",
                    "background:linear-gradient(180deg, rgba(10,16,32,0.96), rgba(9,20,45,0.92))",
                    "border:1px solid rgba(93,168,255,0.28)",
                    "border-radius:14px",
                    "box-shadow:0 12px 36px rgba(15,23,42,0.32)",
                    "padding:10px 12px",
                    "color:#e2e8f0",
                    "font:500 12px/1.4 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
                    "backdrop-filter:blur(24px)",
                  ].join(';');
                  host.innerHTML = `<div style="font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#7dd3fc;margin-bottom:4px;">Live Note</div><div style="font-weight:700;color:#f8fafc;margin-bottom:3px;font-size:12px;">${escapeHtml(note.title || "Suggested change")}</div><div style="color:#cbd5e1;margin-bottom:4px;font-size:11px;">${escapeHtml(note.body || "")}</div>`;
                  document.documentElement.appendChild(host);
                }
              };

              const persistState = () => {
                try {
                  const state = {
                    sessionId: panel.session_id || "",
                    watchMode: panel.watch_mode || "off",
                    railCollapsed: !!overlayState.railCollapsed,
                    lastAutoCollapseStage: overlayState.lastAutoCollapseStage || "",
                  };
                  window.localStorage.setItem("__live_navigator_overlay_state__", JSON.stringify(state));
                } catch (_err) {}
              };

              const invoke = async (command, payload = {}) => {
                if (typeof window.__liveNavigatorCommand !== "function") {
                  return { ok: false, error: "Overlay command bridge is unavailable." };
                }
                return window.__liveNavigatorCommand({ command, payload });
              };

              const readSetupPayload = () => {
                const get = (selector) => shadow.querySelector(selector)?.value || "";
                return {
                  project_name: get('[data-field="project_name"]') || "Navigator Session",
                  goal: get('[data-field="goal"]') || "Help me navigate this website.",
                  domain_hint: get('[data-field="domain_hint"]') || null,
                  index_mode: get('[data-field="index_mode"]') || "advanced",
                };
              };

              shadow.querySelectorAll("[data-command]").forEach((button) => {
                button.onclick = async () => {
                  button.classList.add("ln-clicked");
                  button.disabled = true;
                  try {
                    const command = button.getAttribute("data-command");
                    const payload = command === "start_session"
                      ? readSetupPayload()
                      : { session_id: panel.session_id || "" };
                    if (command === "open_map" && panel.map_url) {
                      window.open(panel.map_url, "_blank", "noopener,noreferrer");
                      return;
                    }
                    if (command === "open_review" && panel.review_url) {
                      window.open(panel.review_url, "_blank", "noopener,noreferrer");
                      return;
                    }
                    const sessionId = button.getAttribute("data-session-id");
                    if (sessionId) payload.session_id = sessionId;
                    await invoke(command, payload);
                  } finally {
                    window.setTimeout(() => button.classList.remove("ln-clicked"), 160);
                    button.disabled = false;
                  }
                };
              });

              const setupForm = shadow.querySelector("[data-setup-form]");
              if (setupForm) {
                setupForm.onsubmit = async (event) => {
                  event.preventDefault();
                  await invoke("start_session", readSetupPayload());
                };
              }

              const setupResume = shadow.querySelector("[data-command=" + '"resume_session"' + "]");
              if (setupResume) {
                setupResume.onclick = async () => {
                  const selected = shadow.querySelector('[data-field="resume_session_id"]')?.value || "";
                  if (!selected) return;
                  await invoke("resume_session", { session_id: selected });
                };
              }

              let persistedState = {};
              try {
                persistedState = JSON.parse(window.localStorage.getItem("__live_navigator_overlay_state__") || "{}") || {};
              } catch (_err) {}
              const overlayState = window.__liveNavigatorOverlayRuntime || { watcherInstalled: false, timer: null, lastSignature: "" };
              if (typeof overlayState.railCollapsed !== "boolean" && typeof persistedState.railCollapsed === "boolean") {
                overlayState.railCollapsed = persistedState.railCollapsed;
              }
              if (!overlayState.lastAutoCollapseStage && typeof persistedState.lastAutoCollapseStage === "string") {
                overlayState.lastAutoCollapseStage = persistedState.lastAutoCollapseStage;
              }
              const computeSignature = () => {
                const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], [role="tab"]'))
                  .slice(0, 12)
                  .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
                  .filter(Boolean);
                return JSON.stringify({
                  href: location.href,
                  title: document.title,
                  buttons,
                });
              };
              const notifyPageChange = () => {
                if ((panel.watch_mode || "off") !== "live_advice" || !panel.session_id) return;
                const signature = computeSignature();
                if (signature === overlayState.lastSignature) return;
                overlayState.lastSignature = signature;
                clearTimeout(overlayState.timer);
                overlayState.timer = setTimeout(() => {
                  invoke("page_changed", { session_id: panel.session_id, page_signature: signature });
                }, 600);
              };
              if (!overlayState.watcherInstalled) {
                const observer = new MutationObserver(() => notifyPageChange());
                observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
                window.addEventListener("popstate", notifyPageChange);
                window.addEventListener("hashchange", notifyPageChange);
                overlayState.watcherInstalled = true;
              }
              const applyRailState = (collapsed) => {
                if (shell) {
                  shell.setAttribute("data-rail-collapsed", collapsed ? "true" : "false");
                }
                const toggle = shadow.querySelector("[data-rail-toggle]");
                if (toggle) {
                  toggle.textContent = collapsed ? "‹" : "›";
                  toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
                  toggle.setAttribute("aria-label", collapsed ? "Open details panel" : "Collapse details panel");
                }
              };
              const autoCollapseRail = !!panel.auto_collapse_rail;
              if (!autoCollapseRail) {
                overlayState.railCollapsed = false;
                overlayState.lastAutoCollapseStage = "";
              } else if (overlayState.lastAutoCollapseStage !== String(panel.stage || "")) {
                overlayState.railCollapsed = true;
                overlayState.lastAutoCollapseStage = String(panel.stage || "");
              }
              applyRailState(!!overlayState.railCollapsed);
              const railToggle = shadow.querySelector("[data-rail-toggle]");
              if (railToggle) {
                railToggle.onclick = async () => {
                  overlayState.railCollapsed = !overlayState.railCollapsed;
                  applyRailState(!!overlayState.railCollapsed);
                  persistState();
                };
              }
              overlayState.watchMode = panel.watch_mode || "off";
              overlayState.sessionId = panel.session_id || "";
              window.__liveNavigatorOverlayRuntime = overlayState;
              persistState();
              renderInlineNotes(Array.isArray(panel.inline_notes) ? panel.inline_notes : []);
              if (overlayState.watchMode === "live_advice") {
                notifyPageChange();
              }
            }
            """
        ).replace("__INLINE_NOTE_WIDTH__", str(OVERLAY_INLINE_NOTE_WIDTH_PX))
        await page.evaluate(
            overlay_script,
            {
                "html": self._overlay_html(panel),
                "css": self._overlay_css(),
                "stage": str(panel.get("stage", "idle")),
                "panel": panel,
            },
        )
        log_event("bridge", "overlay_synced", stage=str(panel.get("stage", "idle")), title=str(panel.get("title", "")))

    async def clear_agent_overlay(self) -> None:
        page = await self._get_target_page(prefer_url=self._last_target_hint)
        await page.evaluate(
            """
            () => {
              const root = document.getElementById("__live_navigator_overlay_root__");
              if (root) root.remove();
              document.querySelectorAll(".__live_navigator_inline_note__").forEach((node) => node.remove());
              try { window.localStorage.removeItem("__live_navigator_overlay_state__"); } catch (_err) {}
            }
            """
        )
        self._last_panel = None
        self._overlay_session_id = ""

    async def focus_active_page(self) -> None:
        page = await self._get_target_page(prefer_url=self._last_target_hint)
        await page.bring_to_front()
        log_event("bridge", "active_page_focused", url=getattr(page, "url", ""))

    async def close_local_ui_tabs(self, ui_url: str) -> None:
        crawler = await self._ensure_controller()
        closed = 0
        for page in list(crawler.list_pages()):
            url = getattr(page, "url", "") or ""
            if url.startswith(ui_url):
                try:
                    await page.close()
                    closed += 1
                except Exception:
                    continue
        if closed:
            log_event("bridge", "local_ui_tabs_closed", ui_url=ui_url, closed=closed)

    async def _ensure_controller(self) -> Crawler:
        async with self._controller_lock:
            if self._crawler is not None and self._crawler_has_usable_browser(self._crawler):
                return self._crawler
            if self._crawler is not None:
                try:
                    await self._crawler.close()
                except Exception:
                    logger.warning("Failed to close a stale crawler controller.", exc_info=True)
                self._crawler = None
            crawler = Crawler(
                cdp_url=self.cdp_url,
                state_store=self.state_store,
                target_domain=self.target_domain,
            )
            await crawler.attach()
            self._crawler = crawler
            log_event("bridge", "controller_attached", cdp_url=self.cdp_url)
            return crawler

    async def _get_target_page(self, prefer_url: str = "") -> Any:
        crawler = await self._ensure_controller()
        pages = self._crawler_pages(crawler)
        if not pages:
            raise RuntimeError("Open a target website tab in the controlled Chrome window first, then retry the overlay.")

        def _is_eligible(page: Any) -> bool:
            url = getattr(page, "url", "") or ""
            if prefer_url and prefer_url in url:
                return True
            if self.target_domain and self.target_domain in url:
                return True
            return url.startswith("http") and not self._is_local_url(url)

        def _score(page: Any) -> tuple[int, int]:
            url = getattr(page, "url", "") or ""
            score = 0
            if prefer_url and prefer_url in url:
                score += 8
            if self.target_domain and self.target_domain in url:
                score += 6
            if url.startswith("http") and not self._is_local_url(url):
                score += 4
            return (score, 1)

        eligible_pages = [page for page in pages if _is_eligible(page)]
        if not eligible_pages:
            promoted = await self._promote_cdp_target_into_page(pages)
            if promoted is None:
                raise RuntimeError("Open a target website tab in the controlled Chrome window first, then retry the overlay.")
            await self._ensure_page_runtime(promoted)
            return promoted

        chosen = max(eligible_pages, key=_score)
        await self._ensure_page_runtime(chosen)
        return chosen

    async def _promote_cdp_target_into_page(self, pages: list[Any]) -> Any | None:
        target_url = self._discover_target_url_from_cdp()
        if not target_url:
            return None

        candidates: list[Any] = []
        for page in pages:
            url = getattr(page, "url", "") or ""
            if not url:
                candidates.append(page)
        for page in pages:
            url = getattr(page, "url", "") or ""
            if url.startswith("http") and "127.0.0.1" not in url and "localhost" not in url and page not in candidates:
                candidates.append(page)
        for page in pages:
            url = getattr(page, "url", "") or ""
            if "127.0.0.1" in url or "localhost" in url:
                continue
            if page not in candidates:
                candidates.append(page)

        last_error: Exception | None = None
        for candidate in candidates:
            try:
                await candidate.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                log_event("bridge", "promoted_raw_cdp_target", target_url=target_url, strategy="existing_page")
                return candidate
            except Exception as exc:
                last_error = exc
                log_event("bridge", "promote_existing_page_failed", target_url=target_url, error=str(exc))

        fresh_page = await self._open_fresh_page_for_target(target_url)
        if fresh_page is not None:
            log_event("bridge", "promoted_raw_cdp_target", target_url=target_url, strategy="fresh_page")
            return fresh_page

        if last_error is not None:
            raise last_error
        return None

    async def _open_fresh_page_for_target(self, target_url: str) -> Any | None:
        crawler = await self._ensure_controller()
        for context in self._crawler_contexts(crawler):
            new_page = getattr(context, "new_page", None)
            if new_page is None:
                continue
            try:
                page = await new_page()
                await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                return page
            except Exception as exc:
                log_event("bridge", "promote_fresh_page_failed", target_url=target_url, error=str(exc))
        return None

    def _discover_target_url_from_cdp(self) -> str:
        endpoint = self.cdp_url.rstrip("/") + "/json/list"
        try:
            with urlopen(endpoint, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return ""

        for item in payload if isinstance(payload, list) else []:
            url = str(item.get("url", "") or "")
            if not url.startswith("http"):
                continue
            host = urlparse(url).netloc
            if host.startswith("127.0.0.1") or host.startswith("localhost"):
                continue
            if self.target_domain and self.target_domain in url:
                return url
        for item in payload if isinstance(payload, list) else []:
            url = str(item.get("url", "") or "")
            if not url.startswith("http"):
                continue
            host = urlparse(url).netloc
            if host.startswith("127.0.0.1") or host.startswith("localhost"):
                continue
            return url
        return ""

    def _page_runtime_token(self, page: Any) -> str:
        token = self._page_runtime_tokens.get(page)
        if token is None:
            token = uuid.uuid4().hex
            self._page_runtime_tokens[page] = token
        return token

    @staticmethod
    def _crawler_pages(crawler: Any) -> list[Any]:
        if hasattr(crawler, "list_pages"):
            return list(crawler.list_pages())
        return list(crawler._all_pages())

    @staticmethod
    def _crawler_has_usable_browser(crawler: Any) -> bool:
        if hasattr(crawler, "has_usable_browser"):
            return bool(crawler.has_usable_browser())
        return bool(getattr(crawler, "_browser", None)) and bool(LocalBrowserBridge._crawler_pages(crawler))

    @staticmethod
    def _crawler_contexts(crawler: Any) -> list[Any]:
        if hasattr(crawler, "browser_contexts"):
            return list(crawler.browser_contexts())
        browser = getattr(crawler, "_browser", None)
        return list(getattr(browser, "contexts", []) or [])

    @staticmethod
    def _crawler_detect_quarter_number(crawler: Any, url: str, semantic_text: str) -> int:
        if hasattr(crawler, "detect_quarter_number"):
            return int(crawler.detect_quarter_number(url, semantic_text))
        return int(crawler._detect_quarter_number(url, semantic_text))

    @staticmethod
    async def _crawler_wait_after_navigation(crawler: Any, page: Any) -> None:
        if hasattr(crawler, "wait_after_navigation"):
            await crawler.wait_after_navigation(page)
            return
        await crawler._wait_after_navigation(page)

    @staticmethod
    async def _crawler_scrape_page_snapshot(crawler: Any, page: Any, *, quarter_number: int, editable: bool) -> dict[str, Any]:
        if hasattr(crawler, "scrape_page_snapshot"):
            return await crawler.scrape_page_snapshot(page, quarter_number=quarter_number, editable=editable)
        return await crawler._scrape_page_snapshot(page, quarter_number=quarter_number, editable=editable)

    async def _ensure_page_runtime(self, page: Any) -> None:
        token = self._page_runtime_token(page)
        if token not in self._bound_pages:
            if self._command_handler is not None:
                try:
                    await page.expose_binding("__liveNavigatorCommand", self._handle_overlay_command)
                except Exception:
                    logger.warning("Failed to expose the overlay command binding on the active page.", exc_info=True)
            try:
                await page.add_init_script(
                    script=r"""
                    (() => {
                      if (window.__liveNavigatorInitInstalled) return;
                      window.__liveNavigatorInitInstalled = true;
                      const bootstrap = () => {
                        try {
                          const raw = window.localStorage.getItem("__live_navigator_overlay_state__");
                          if (!raw || typeof window.__liveNavigatorCommand !== "function") return;
                          const state = JSON.parse(raw);
                          if (state && state.sessionId && state.watchMode === "live_advice") {
                            const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], [role="tab"]'))
                              .slice(0, 12)
                              .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
                              .filter(Boolean);
                            const signature = JSON.stringify({ href: location.href, title: document.title, buttons });
                            window.__liveNavigatorCommand({ command: "page_changed", payload: { session_id: state.sessionId, page_signature: signature } });
                          }
                        } catch (_err) {}
                      };
                      window.addEventListener("load", bootstrap, { once: true });
                    })();
                    """
                )
            except Exception:
                logger.warning("Failed to install the overlay init script on the active page.", exc_info=True)
            self._bound_pages.add(token)

        if token not in self._navigation_hooked_pages:
            try:
                page.on("framenavigated", lambda frame: asyncio.create_task(self._handle_navigation(page, frame)))
            except Exception:
                logger.warning("Failed to attach the navigation observer to the active page.", exc_info=True)
            self._navigation_hooked_pages.add(token)

    async def _handle_navigation(self, page: Any, frame: Any) -> None:
        try:
            if frame != page.main_frame:
                return
        except Exception:
            return
        if self._last_panel is None:
            return
        try:
            await asyncio.sleep(0.25)
            await self._ensure_page_runtime(page)
            await self.sync_agent_overlay(self._last_panel)
        except Exception:
            logger.warning("Overlay resync failed after navigation.", exc_info=True)

    async def _handle_overlay_command(self, source: Any, payload: dict[str, Any]) -> dict[str, Any]:
        source_page = getattr(source, "page", None)
        source_url = str(getattr(source_page, "url", "") or "")
        if source_url.startswith("http") and not self._is_local_url(source_url):
            self._last_target_hint = source_url
        if self._command_handler is None:
            return {"ok": False, "error": "No overlay command handler is registered."}
        result = self._command_handler(payload if isinstance(payload, dict) else {})
        if inspect.isawaitable(result):
            result = await result
        return result or {"ok": True}

    async def _execute_navigate(self, page: Any, action: ActionProposal) -> dict[str, Any]:
        if not action.url:
            return {"action_id": action.action_id, "status": "skipped", "detail": "missing_url"}
        await page.goto(action.url, wait_until="domcontentloaded", timeout=15000)
        return {"action_id": action.action_id, "status": "executed", "detail": action.url}

    async def _execute_scroll(self, page: Any, action: ActionProposal) -> dict[str, Any]:
        amount = int(action.value or "700")
        await page.evaluate(f"window.scrollBy(0, {amount})")
        return {"action_id": action.action_id, "status": "executed", "detail": f"scroll:{amount}"}

    async def _execute_wait_for(self, page: Any, action: ActionProposal) -> dict[str, Any]:
        if action.target_text:
            await page.get_by_text(action.target_text, exact=False).first.wait_for(state="visible", timeout=8000)
        else:
            await page.wait_for_load_state("networkidle", timeout=8000)
        return {"action_id": action.action_id, "status": "executed", "detail": "wait_for"}

    async def _execute_locator_action(self, page: Any, action: ActionProposal) -> dict[str, Any]:
        ax_block = await self._verify_action_against_ax(page, action)
        if ax_block is not None:
            return ax_block
        locator = self._resolve_locator(page, action)
        if action.action == "click":
            await locator.click(timeout=5000)
            return {"action_id": action.action_id, "status": "executed", "detail": "clicked"}
        if action.action == "type":
            await locator.click(timeout=5000)
            await locator.fill(action.input_text or action.value or "")
            return {"action_id": action.action_id, "status": "executed", "detail": "typed"}
        if action.action == "select":
            await locator.select_option(label=action.value or "")
            return {"action_id": action.action_id, "status": "executed", "detail": "selected"}
        text = await locator.inner_text()
        return {"action_id": action.action_id, "status": "executed", "detail": text}

    async def _execute_single(self, page: Any, action: ActionProposal) -> dict[str, Any]:
        try:
            handlers = {
                "navigate": self._execute_navigate,
                "scroll": self._execute_scroll,
                "wait_for": self._execute_wait_for,
                "click": self._execute_locator_action,
                "type": self._execute_locator_action,
                "select": self._execute_locator_action,
                "extract": self._execute_locator_action,
            }
            handler = handlers.get(action.action)
            if handler is not None:
                return await handler(page, action)
            if action.action == "suggest_only":
                return {"action_id": action.action_id, "status": "skipped", "detail": "suggest_only"}
            if action.action == "stop":
                return {"action_id": action.action_id, "status": "executed", "detail": "stop"}
            return {"action_id": action.action_id, "status": "skipped", "detail": "unsupported_action"}
        except Exception as exc:
            try:
                dom_text = await page.evaluate("document.body.innerText")
                self.state_store.error_dom_path.write_text(str(dom_text), encoding="utf-8")
            except Exception:
                pass
            log_event("bridge", "action_failed", action_id=action.action_id, action=action.action, error=str(exc))
            return {"action_id": action.action_id, "status": "failed", "error": str(exc)}

    @staticmethod
    def _resolve_locator(page: Any, action: ActionProposal) -> Any:
        selector = action.metadata.get("selector")
        if selector:
            return page.locator(selector).first
        row_text = action.metadata.get("row_text")
        control_type = action.metadata.get("control_type")
        if row_text and control_type == "checkbox":
            return page.locator("tr", has_text=str(row_text)).locator('input[type="checkbox"]').first
        if row_text and control_type == "text_input":
            return page.locator("tr", has_text=str(row_text)).locator('input[type="text"], input:not([type]), textarea').first
        if action.role and action.target_text:
            return page.get_by_role(action.role, name=action.target_text).first
        if action.target_text:
            return page.get_by_text(action.target_text, exact=False).first
        if action.validation_text:
            return page.get_by_text(action.validation_text, exact=False).first
        raise RuntimeError("Action proposal does not include a resolvable target")

    async def _verify_action_against_ax(self, page: Any, action: ActionProposal) -> dict[str, Any] | None:
        if not self.ax_snapshots_enabled or self.ax_snapshot_provider is None:
            return None
        include_occlusion = self.ax_occlusion_mode in {"diagnostic", "always", "verify"}
        try:
            snapshot = await self.ax_snapshot_provider.capture(
                page,
                mode="verify",
                target_scope={
                    "target_text": action.target_text,
                    "role": action.role,
                    "ax_node_id": action.metadata.get("ax_node_id"),
                },
                include_occlusion=include_occlusion,
            )
        except Exception as exc:
            log_event("bridge", "ax_verify_failed", action_id=action.action_id, error=str(exc))
            return None

        match = None
        requested_ax_node_id = str(action.metadata.get("ax_node_id", "") or "").strip()
        requested_name = str(action.target_text or action.validation_text or "").strip().lower()
        requested_role = str(action.role or "").strip().lower()
        for target in snapshot.targets:
            target_name = str(target.get("name", "")).strip().lower()
            target_role = str(target.get("role", "")).strip().lower()
            if requested_ax_node_id and requested_ax_node_id == str(target.get("ax_node_id", "")):
                match = target
                break
            if requested_name and requested_name == target_name and (not requested_role or requested_role == target_role):
                match = target
                break
        if match is None:
            return None

        action.metadata["ax_node_id"] = str(match.get("ax_node_id", ""))
        action.metadata["ax_role"] = str(match.get("role", ""))
        action.metadata["ax_name"] = str(match.get("name", ""))
        action.metadata["ax_bounds"] = dict(match.get("bounds", {}) or {})
        action.metadata["ax_confidence"] = float(match.get("source_confidence", 0.0) or 0.0)
        if not bool(match.get("actionable", True)):
            block_reason = str(match.get("block_reason", "not_actionable") or "not_actionable")
            action.metadata["ax_block_reason"] = block_reason
            log_event("bridge", "ax_action_blocked", action_id=action.action_id, block_reason=block_reason)
            return {"action_id": action.action_id, "status": "skipped", "detail": f"ax_blocked:{block_reason}"}
        log_event("bridge", "ax_action_verified", action_id=action.action_id, ax_node_id=action.metadata["ax_node_id"])
        return None

    def _save_screenshot(self, session_id: str, screenshot_b64: str, filename: str) -> str | None:
        if self.artifact_store is None:
            return None
        artifact = self.artifact_store.save_png_b64(session_id, screenshot_b64, filename)
        return artifact.get("path")

    async def _build_observation(
        self,
        page: Any,
        crawler: Crawler,
        session_id: str,
        active_goal: str,
        domain_pack: str,
        safety_mode: str,
        prior_actions: list[dict[str, Any]] | None = None,
        visible_text_override: str | None = None,
        dom_summary_override: str | None = None,
        browser_metadata: dict[str, Any] | None = None,
        ax_capture_mode: str = "live",
    ) -> ObservationPacket:
        captured_at = utc_now_iso()
        title = await page.title()
        url = page.url
        visible_text = visible_text_override or await crawler.extract_semantic_text(page)
        dom_summary = dom_summary_override or visible_text
        screenshot_bytes = await page.screenshot(type="png", full_page=False)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        screenshot_path = self._save_screenshot(session_id, screenshot_b64, f"{captured_at.replace(':', '-')}.png")
        supplementary_capture = await self._capture_supplementary_region_data(
            page,
            session_id=session_id,
            captured_at=captured_at,
        )
        long_page_capture = await self._capture_long_page_region_data(
            page,
            session_id=session_id,
            captured_at=captured_at,
        )
        metadata = dict(browser_metadata or {})
        metadata.update(await self._extract_browser_metadata(page))
        metadata.update(supplementary_capture.get("browser_metadata", {}))
        metadata.update(long_page_capture.get("browser_metadata", {}))
        metadata.update(await self._capture_ax_metadata(page, session_id=session_id, mode=ax_capture_mode))
        supplementary_screenshots = list(supplementary_capture.get("supplementary_screenshots", []))
        supplementary_screenshots.extend(long_page_capture.get("supplementary_screenshots", []))
        return ObservationPacket(
            session_id=session_id,
            screenshot_b64=screenshot_b64,
            screenshot_path=screenshot_path,
            supplementary_screenshots=supplementary_screenshots,
            page_url=url,
            page_title=title,
            dom_summary=dom_summary[:12000],
            visible_text_summary=visible_text[:32000],
            prior_actions=prior_actions or [],
            active_goal=active_goal,
            domain_pack=domain_pack,
            safety_mode=safety_mode,
            browser_metadata=metadata,
            captured_at=captured_at,
        )

    async def _capture_supplementary_region_data(
        self,
        page: Any,
        *,
        session_id: str,
        captured_at: str,
    ) -> dict[str, Any]:
        try:
            descriptor = await page.evaluate(
                r"""
                () => {
                  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
                  const textOf = (el) => (el?.innerText || el?.textContent || "").replace(/\s+/g, " ").trim();
                  const viewportHeight = window.innerHeight || 0;
                  const viewportWidth = window.innerWidth || 0;
                  const candidates = Array.from(document.querySelectorAll("table, [role='table'], [role='grid']"));
                  let best = null;

                  for (const element of candidates) {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    if (!rect.width || !rect.height || style.display === "none" || style.visibility === "hidden") continue;
                    if (rect.bottom < 0 || rect.top > viewportHeight) continue;
                    const rows = Array.from(element.querySelectorAll("tr, [role='row']"));
                    if (rows.length < 6) continue;

                    let scrollTarget = element;
                    let current = element;
                    let contentHeight = rect.height;
                    let visibleHeight = rect.height;
                    let selector = "";
                    while (current) {
                      const currentStyle = window.getComputedStyle(current);
                      const overflowY = currentStyle.overflowY || "";
                      if ((overflowY.includes("auto") || overflowY.includes("scroll")) && current.scrollHeight > current.clientHeight + 80 && current.clientHeight > 120) {
                        scrollTarget = current;
                        contentHeight = current.scrollHeight;
                        visibleHeight = current.clientHeight;
                        break;
                      }
                      current = current.parentElement;
                    }

                    const selectorKey = `live-nav-capture-${Math.random().toString(36).slice(2, 10)}`;
                    scrollTarget.setAttribute("data-live-nav-capture-target", selectorKey);
                    selector = `[data-live-nav-capture-target="${selectorKey}"]`;

                    const headers = Array.from(element.querySelectorAll("th, [role='columnheader']"))
                      .map((cell) => textOf(cell))
                      .filter(Boolean)
                      .slice(0, 8);
                    const sampleRows = rows
                      .slice(0, 4)
                      .map((row) => Array.from(row.querySelectorAll("th, td, [role='cell'], [role='gridcell']")).map((cell) => textOf(cell)).filter(Boolean).slice(0, 8))
                      .filter((row) => row.length);
                    const score = rows.length * 10 + Math.min(contentHeight, 2000);
                    if (!best || score > best.score) {
                      const clipHeight = Math.max(180, Math.min(scrollTarget.getBoundingClientRect().height || rect.height, 420));
                      best = {
                        score,
                        label: textOf(element.querySelector("caption")) || headers.join(" / ") || textOf(element).slice(0, 80) || "Table region",
                        selector,
                        row_count: rows.length,
                        headers,
                        sample_rows: sampleRows,
                        original_scroll_top: scrollTarget.scrollTop || 0,
                        captures: Array.from({ length: Math.min(3, Math.max(1, Math.ceil(contentHeight / Math.max(visibleHeight, 1)))) }, (_, index) => ({
                          label: `table_slice_${index + 1}`,
                          scroll_top: contentHeight > visibleHeight ? index * Math.max(120, clipHeight - 60) : null,
                          clip: {
                            x: Math.round(clamp(scrollTarget.getBoundingClientRect().left, 0, viewportWidth - 1)),
                            y: Math.round(clamp(scrollTarget.getBoundingClientRect().top, 0, viewportHeight - 1)),
                            width: Math.round(Math.min(scrollTarget.getBoundingClientRect().width, viewportWidth - Math.max(scrollTarget.getBoundingClientRect().left, 0) - 12)),
                            height: Math.round(clipHeight),
                          },
                        })),
                      };
                    }
                  }

                  return best;
                }
                """
            )
        except Exception:
            return {}

        if not isinstance(descriptor, dict):
            return {}
        captures = [item for item in descriptor.get("captures", []) if isinstance(item, dict)][:3]
        if not captures:
            return {}

        supplementary: list[dict[str, Any]] = []
        selector = str(descriptor.get("selector", "") or "")
        original_scroll_top = descriptor.get("original_scroll_top")
        try:
            for item in captures:
                clip = dict(item.get("clip", {}) or {})
                if not clip:
                    continue
                if selector and item.get("scroll_top") is not None:
                    try:
                        await page.evaluate(
                            """
                            ({ selector, scrollTop }) => {
                              const el = document.querySelector(selector);
                              if (el) el.scrollTop = scrollTop;
                            }
                            """,
                            {"selector": selector, "scrollTop": int(item.get("scroll_top", 0) or 0)},
                        )
                    except Exception:
                        pass
                try:
                    screenshot_bytes = await page.screenshot(type="png", full_page=False, clip=clip)
                    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                    screenshot_path = self._save_screenshot(
                        session_id,
                        screenshot_b64,
                        f"{captured_at.replace(':', '-')}-{str(item.get('label', 'capture'))}.png",
                    )
                    supplementary.append(
                        {
                            "label": str(item.get("label", "table_slice")),
                            "screenshot_b64": screenshot_b64,
                            "screenshot_path": screenshot_path,
                        }
                    )
                except Exception:
                    logger.warning("Failed to capture a supplementary table slice.", exc_info=True)
        finally:
            if selector and original_scroll_top is not None:
                try:
                    await page.evaluate(
                        """
                        ({ selector, scrollTop }) => {
                          const el = document.querySelector(selector);
                          if (el) el.scrollTop = scrollTop;
                        }
                        """,
                        {"selector": selector, "scrollTop": int(original_scroll_top or 0)},
                    )
                except Exception:
                    pass

        if not supplementary:
            return {}
        return {
            "supplementary_screenshots": supplementary,
            "browser_metadata": {
                "table_region": {
                    "label": str(descriptor.get("label", "") or ""),
                    "row_count": int(descriptor.get("row_count", 0) or 0),
                    "headers": list(descriptor.get("headers", []) or []),
                    "sample_rows": list(descriptor.get("sample_rows", []) or []),
                }
            },
        }

    async def _capture_long_page_region_data(
        self,
        page: Any,
        *,
        session_id: str,
        captured_at: str,
    ) -> dict[str, Any]:
        try:
            descriptor = await page.evaluate(
                r"""
                () => {
                  const root = document.scrollingElement || document.documentElement || document.body;
                  if (!root) return null;
                  const viewportHeight = window.innerHeight || 0;
                  const scrollHeight = Math.max(
                    root.scrollHeight || 0,
                    document.documentElement?.scrollHeight || 0,
                    document.body?.scrollHeight || 0,
                  );
                  if (!viewportHeight || scrollHeight <= viewportHeight + 220) return null;

                  const maxScrollTop = Math.max(0, scrollHeight - viewportHeight);
                  const candidateTargets = [
                    Math.min(maxScrollTop, Math.round(viewportHeight * 0.8)),
                    Math.min(maxScrollTop, Math.round(maxScrollTop * 0.55)),
                    maxScrollTop,
                  ];
                  const uniqueTargets = [];
                  for (const target of candidateTargets) {
                    if (target <= 120) continue;
                    if (uniqueTargets.some((item) => Math.abs(item - target) < 120)) continue;
                    uniqueTargets.push(target);
                  }
                  if (!uniqueTargets.length) return null;

                  return {
                    label: document.title || "Page depth capture",
                    scroll_height: scrollHeight,
                    viewport_height: viewportHeight,
                    original_scroll_y: window.scrollY || root.scrollTop || 0,
                    captures: uniqueTargets.slice(0, 2).map((scrollTop, index) => ({
                      label: `page_slice_${index + 2}`,
                      scroll_top: scrollTop,
                    })),
                  };
                }
                """
            )
        except Exception:
            return {}

        if not isinstance(descriptor, dict):
            return {}
        scroll_height = int(descriptor.get("scroll_height", 0) or 0)
        viewport_height = int(descriptor.get("viewport_height", 0) or 0)
        captures = [item for item in descriptor.get("captures", []) if isinstance(item, dict)]
        if scroll_height <= viewport_height or not captures:
            return {}

        supplementary: list[dict[str, Any]] = []
        original_scroll_y = int(descriptor.get("original_scroll_y", 0) or 0)
        try:
            for item in captures:
                scroll_top = int(item.get("scroll_top", 0) or 0)
                try:
                    await page.evaluate(
                        """
                        ({ scrollTop }) => {
                          const root = document.scrollingElement || document.documentElement || document.body;
                          if (!root) return;
                          root.scrollTop = scrollTop;
                          window.scrollTo(0, scrollTop);
                        }
                        """,
                        {"scrollTop": scroll_top},
                    )
                    await page.evaluate(
                        """
                        () => new Promise((resolve) => {
                          requestAnimationFrame(() => requestAnimationFrame(resolve));
                        })
                        """
                    )
                    screenshot_bytes = await page.screenshot(type="png", full_page=False)
                except Exception:
                    logger.warning("Failed to capture a long-page supplementary screenshot.", exc_info=True)
                    continue

                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
                screenshot_path = self._save_screenshot(
                    session_id,
                    screenshot_b64,
                    f"{captured_at.replace(':', '-')}-{str(item.get('label', 'page-slice'))}.png",
                )
                supplementary.append(
                    {
                        "label": str(item.get("label", "page_slice")),
                        "screenshot_b64": screenshot_b64,
                        "screenshot_path": screenshot_path,
                    }
                )
        finally:
            try:
                await page.evaluate(
                    """
                    ({ scrollTop }) => {
                      const root = document.scrollingElement || document.documentElement || document.body;
                      if (!root) return;
                      root.scrollTop = scrollTop;
                      window.scrollTo(0, scrollTop);
                    }
                    """,
                    {"scrollTop": original_scroll_y},
                )
            except Exception:
                logger.warning("Failed to restore the original page scroll position after long-page capture.", exc_info=True)

        if not supplementary:
            return {}
        return {
            "supplementary_screenshots": supplementary,
            "browser_metadata": {
                "page_region": {
                    "label": str(descriptor.get("label", "") or ""),
                    "scroll_height": scroll_height,
                    "viewport_height": viewport_height,
                    "slice_count": len(supplementary),
                }
            },
        }

    async def _capture_ax_metadata(self, page: Any, *, session_id: str, mode: str, target_scope: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.ax_snapshots_enabled or self.ax_snapshot_provider is None:
            return {}
        include_occlusion = mode == "verify" and self.ax_occlusion_mode in {"diagnostic", "always", "verify"}
        try:
            snapshot = await self.ax_snapshot_provider.capture(
                page,
                mode=mode,
                target_scope=target_scope,
                include_occlusion=include_occlusion,
            )
        except Exception as exc:
            log_event("bridge", "ax_capture_failed", mode=mode, error=str(exc))
            return {
                "ax_capture_mode": mode,
                "ax_diagnostics": {"error": str(exc)},
            }

        payload = {
            "ax_summary": snapshot.summary,
            "ax_targets": snapshot.targets,
            "ax_diagnostics": snapshot.diagnostics,
            "ax_capture_mode": snapshot.mode,
        }
        if self.artifact_store is not None and getattr(snapshot, "raw", None):
            try:
                artifact = self.artifact_store.save_json(
                    session_id,
                    snapshot.raw,
                    f"{utc_now_iso().replace(':', '-')}-ax-{mode}.json",
                )
                payload["ax_artifact_path"] = artifact.get("path", "")
            except Exception:
                pass
        return payload

    @staticmethod
    async def _extract_browser_metadata(page: Any) -> dict[str, Any]:
        try:
            payload = await page.evaluate(
                r"""
                () => {
                  const visibleText = (el) => {
                    const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
                    return text.slice(0, 120);
                  };
                  const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], [role="tab"]'))
                    .map((el) => ({ text: visibleText(el), tag: el.tagName.toLowerCase() }))
                    .filter((item) => item.text)
                    .slice(0, 24);
                  const checkboxRows = Array.from(document.querySelectorAll('tr'))
                    .map((row) => {
                      const checkbox = row.querySelector('input[type="checkbox"]');
                      if (!checkbox) return null;
                      const rowText = visibleText(row);
                      if (!rowText) return null;
                      return { row_text: rowText, checked: !!checkbox.checked };
                    })
                    .filter(Boolean)
                    .slice(0, 20);
                  const editableRows = Array.from(document.querySelectorAll('tr'))
                    .map((row) => {
                      const textInputs = Array.from(row.querySelectorAll('input[type="text"], input[type="number"], input:not([type]), textarea'));
                      const selects = Array.from(row.querySelectorAll('select'));
                      if (!textInputs.length && !selects.length) return null;
                      const rowText = visibleText(row);
                      if (!rowText) return null;
                      const currentValues = [
                        ...textInputs.map((el) => el.value || ''),
                        ...selects.map((el) => el.value || el.options?.[el.selectedIndex]?.text || ''),
                      ].map((item) => String(item).trim()).filter(Boolean).slice(0, 4);
                      return {
                        row_text: rowText,
                        control_types: [textInputs.length ? 'text_input' : '', selects.length ? 'select' : ''].filter(Boolean),
                        current_values: currentValues,
                      };
                    })
                    .filter(Boolean)
                    .slice(0, 20);
                  const pageSignature = JSON.stringify({
                    href: location.href,
                    title: document.title,
                    buttons: buttons.slice(0, 12).map((item) => item.text),
                    rows: checkboxRows.slice(0, 8).map((item) => item.row_text),
                    editableRows: editableRows.slice(0, 8).map((item) => item.row_text),
                  });
                  return { buttons, checkbox_rows: checkboxRows, editable_rows: editableRows, page_signature: pageSignature };
                }
                """
            )
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {}

    async def _build_site_probe(self, page: Any, crawler: Crawler, domain_pack: str) -> dict[str, Any]:
        title = await page.title()
        url = page.url
        nav_items = await crawler.discover_navigation_items(page)
        quarter_number = self._crawler_detect_quarter_number(crawler, url, await crawler.extract_semantic_text(page))
        site_map_item = {"title": title, "url": url, "section_count": 0}
        if domain_pack == "marketplace_simulation":
            site_map_item["quarter_number"] = quarter_number
            site_map_item["editable"] = True
        return {
            "site_origin": normalize_site_origin(url),
            "navigation_items": nav_items,
            "site_map": [site_map_item],
        }

    async def _lightweight_site_index(self, page: Any, crawler: Crawler, domain_pack: str) -> dict[str, Any]:
        title = await page.title()
        url = page.url
        semantic_text = await crawler.extract_semantic_text(page)
        nav_items = await crawler.discover_navigation_items(page)
        site_map_item = {"title": title, "url": url, "section_count": 0}
        if domain_pack == "marketplace_simulation":
            quarter_number = self._crawler_detect_quarter_number(crawler, url, semantic_text)
            site_map_item["quarter_number"] = quarter_number
            site_map_item["editable"] = True
        return {
            "captured_at": utc_now_iso(),
            "title": title,
            "url": url,
            "semantic_text": semantic_text,
            "navigation_items": nav_items,
            "sections": [],
            "site_map": [site_map_item],
            "completed_quarters": [],
            "quarter_range": {},
            "editable_quarter": site_map_item.get("quarter_number"),
        }

    async def _partial_site_index(self, page: Any, crawler: Crawler, domain_pack: str) -> dict[str, Any]:
        original_url = page.url
        semantic_text = await crawler.extract_semantic_text(page)
        quarter_number = self._crawler_detect_quarter_number(crawler, page.url, semantic_text)
        snapshot = await self._crawler_scrape_page_snapshot(crawler, page, quarter_number=quarter_number, editable=True)
        if original_url and page.url != original_url:
            try:
                await page.goto(original_url, wait_until="domcontentloaded", timeout=15000)
                await self._crawler_wait_after_navigation(crawler, page)
            except Exception:
                logger.warning("Failed to restore the original page after partial indexing.", exc_info=True)
        site_map_item = {
            "title": snapshot.get("title", ""),
            "url": snapshot.get("url", ""),
            "section_count": len(snapshot.get("sections", [])),
        }
        if domain_pack == "marketplace_simulation":
            site_map_item["quarter_number"] = snapshot.get("quarter_number")
            site_map_item["editable"] = snapshot.get("editable", False)
        return {
            "captured_at": snapshot.get("captured_at", utc_now_iso()),
            "title": snapshot.get("title", ""),
            "url": snapshot.get("url", ""),
            "semantic_text": snapshot.get("semantic_text", ""),
            "navigation_items": snapshot.get("navigation_items", []),
            "sections": snapshot.get("sections", []),
            "site_map": [site_map_item],
            "completed_quarters": [snapshot] if domain_pack == "marketplace_simulation" else [],
            "quarter_range": {
                "start": snapshot.get("quarter_number"),
                "end": snapshot.get("quarter_number"),
            } if domain_pack == "marketplace_simulation" else {},
            "editable_quarter": snapshot.get("quarter_number") if domain_pack == "marketplace_simulation" else None,
        }

    async def _explore_generic_site_index(
        self,
        page: Any,
        crawler: Crawler,
        active_goal: str,
        max_pages: int,
    ) -> dict[str, Any]:
        original_url = page.url
        original_title = await page.title()
        goal_terms = self._generic_goal_terms(active_goal)
        seen_urls: set[str] = set()
        queued_urls: set[str] = set()
        site_map: list[dict[str, Any]] = []
        sections: list[dict[str, Any]] = []
        semantic_parts: list[str] = []
        all_navigation: list[str] = []
        frontier: list[dict[str, Any]] = [{"url": original_url, "label": original_title, "score": 100, "depth": 0, "parent_url": ""}]

        while frontier and len(site_map) < max_pages:
            candidate = frontier.pop(0)
            candidate_url = str(candidate.get("url", "") or "").strip()
            if not candidate_url or candidate_url in seen_urls:
                continue
            seen_urls.add(candidate_url)
            candidate_parent_url = str(candidate.get("parent_url", "") or "").strip()
            if page.url != candidate_url:
                try:
                    await page.goto(candidate_url, wait_until="domcontentloaded", timeout=15000)
                    await self._crawler_wait_after_navigation(crawler, page)
                except Exception as exc:
                    log_event("bridge", "generic_exploration_navigation_failed", url=candidate_url, error=str(exc))
                    continue

            title = await page.title()
            semantic_text = await crawler.extract_semantic_text(page)
            nav_items = await crawler.discover_navigation_items(page)
            candidate_links = await self._discover_generic_candidate_links(page, goal_terms)
            all_navigation.extend(nav_items)
            all_navigation.extend(item["label"] for item in candidate_links if item.get("label"))
            semantic_parts.append(f"PAGE: {title}\nURL: {page.url}\n{semantic_text}")
            sections.append(
                {
                    "menu_item": title or candidate.get("label") or page.url,
                    "url": page.url,
                    "parent_url": candidate_parent_url,
                    "semantic_text": semantic_text,
                    "navigation_items": [item["label"] for item in candidate_links[:8] if item.get("label")],
                }
            )
            site_map.append(
                {
                    "key": page.url,
                    "title": title,
                    "url": page.url,
                    "section_count": len(candidate_links[:8]),
                    "parent_key": candidate_parent_url,
                }
            )

            for link in candidate_links:
                link_url = str(link.get("url", "") or "").strip()
                if (
                    not link_url
                    or link_url in seen_urls
                    or link_url in queued_urls
                    or len(site_map) + len(frontier) >= max_pages * 3
                ):
                    continue
                queued_urls.add(link_url)
                frontier.append(
                    {
                        **link,
                        "parent_url": page.url,
                        "depth": int(candidate.get("depth", 0) or 0) + 1,
                    }
                )

            frontier.sort(
                key=lambda item: (
                    -int(item.get("score", 0) or 0),
                    int(item.get("depth", 0) or 0),
                    len(str(item.get("url", ""))),
                )
            )

        if original_url and page.url != original_url:
            try:
                await page.goto(original_url, wait_until="domcontentloaded", timeout=15000)
                await self._crawler_wait_after_navigation(crawler, page)
            except Exception:
                logger.warning("Failed to restore the original page after generic exploration.", exc_info=True)

        dedup_navigation: list[str] = []
        seen_nav: set[str] = set()
        for item in all_navigation:
            label = str(item or "").strip()
            if not label or label in seen_nav:
                continue
            seen_nav.add(label)
            dedup_navigation.append(label)

        return {
            "captured_at": utc_now_iso(),
            "title": original_title,
            "url": original_url,
            "semantic_text": "\n\n".join(part for part in semantic_parts if part).strip(),
            "navigation_items": dedup_navigation,
            "sections": sections,
            "site_map": site_map,
            "completed_quarters": [],
            "quarter_range": {},
            "editable_quarter": None,
        }

    async def _discover_generic_candidate_links(self, page: Any, goal_terms: list[str]) -> list[dict[str, Any]]:
        raw_links = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll("a[href]")).map((anchor) => {
              const href = anchor.getAttribute("href") || "";
              const absoluteUrl = (() => {
                try {
                  return new URL(href, window.location.href).href;
                } catch (_error) {
                  return "";
                }
              })();
              const label = (anchor.innerText || anchor.textContent || anchor.getAttribute("aria-label") || anchor.getAttribute("title") || "")
                .replace(/\\s+/g, " ")
                .trim();
              const style = window.getComputedStyle(anchor);
              const present = style.visibility !== "hidden" && style.display !== "none";
              return { label, url: absoluteUrl, present };
            })
            """
        )
        if not isinstance(raw_links, list):
            return []
        current = urlparse(page.url)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_links:
            if not isinstance(item, dict) or not bool(item.get("present")):
                continue
            url = str(item.get("url", "") or "").strip()
            label = str(item.get("label", "") or "").strip()
            if not url or url in seen:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                continue
            if current.netloc and parsed.netloc and parsed.netloc != current.netloc:
                continue
            if "#" in url and url.split("#", 1)[0] == page.url:
                continue
            if any(url.lower().endswith(ext) for ext in (".m3u8", ".mp4", ".mov", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")):
                continue
            lowered = f"{label} {url}".lower()
            if any(token in lowered for token in ("privacy", "terms", "support", "account", "signin", "sign-in", "bag", "cart")):
                continue
            details = self._score_generic_link_details(label, url, goal_terms)
            candidates.append(
                {
                    "label": label or parsed.path.strip("/") or url,
                    "url": url,
                    **details,
                }
            )
            seen.add(url)

        if any(int(item.get("overlap_count", 0) or 0) > 0 for item in candidates):
            top_score = max(int(item.get("score", 0) or 0) for item in candidates)
            candidates = [
                item
                for item in candidates
                if (
                    int(item.get("score", 0) or 0) >= top_score - 8
                    and int(item.get("leaf_hits", 0) or 0) == 0
                    and int(item.get("path_depth", 0) or 0) <= 2
                )
            ]

        candidates.sort(key=lambda item: (-int(item.get("score", 0) or 0), len(str(item.get("url", "")))))
        return candidates[:12]

    @staticmethod
    def _generic_goal_terms(active_goal: str) -> list[str]:
        stopwords = {
            "about",
            "after",
            "around",
            "based",
            "best",
            "cheapest",
            "compare",
            "configuration",
            "find",
            "from",
            "give",
            "guide",
            "need",
            "options",
            "page",
            "please",
            "price",
            "pricing",
            "show",
            "summary",
            "that",
            "this",
            "website",
            "with",
        }
        terms = [
            token
            for token in re.findall(r"[a-z0-9]{3,}", (active_goal or "").lower())
            if token not in stopwords
        ]
        deduped: list[str] = []
        for term in terms:
            if term not in deduped:
                deduped.append(term)
        return deduped[:8]

    @staticmethod
    def _score_generic_link_details(label: str, url: str, goal_terms: list[str]) -> dict[str, int]:
        parsed = urlparse(url)
        label_tokens = {token for token in re.findall(r"[a-z0-9]+", label.lower()) if token}
        path_tokens = {token for token in re.findall(r"[a-z0-9]+", parsed.path.lower()) if token}
        normalized_tokens = set()
        for token in {*label_tokens, *path_tokens}:
            normalized_tokens.add(token)
            if token.endswith("s") and len(token) > 3:
                normalized_tokens.add(token[:-1])
        normalized_goal_terms = set()
        for term in goal_terms:
            normalized_goal_terms.add(term)
            if term.endswith("s") and len(term) > 3:
                normalized_goal_terms.add(term[:-1])
        path_segments = [segment for segment in parsed.path.split("/") if segment]
        overlap_count = len(normalized_tokens & normalized_goal_terms)
        leaf_tokens = {"buy", "shop", "compare", "pricing", "price", "configure", "configurator", "specs", "spec", "support"}
        leaf_hits = len(path_tokens & leaf_tokens)
        family_branch = overlap_count > 0 and leaf_hits == 0 and len(path_segments) <= 2 and len(label_tokens) <= 3
        score = 0
        score += overlap_count * 7
        if overlap_count and len(label_tokens) <= 2:
            score += 5
        if overlap_count and len(path_segments) <= 1:
            score += 4
        if family_branch:
            score += 6
        score += max(0, 4 - min(len(path_segments), 4)) * 2
        score -= leaf_hits * 6
        if overlap_count == 0:
            score -= 8
            if leaf_hits:
                score -= 4
        if label:
            score += 1
        return {
            "score": score,
            "overlap_count": overlap_count,
            "leaf_hits": leaf_hits,
            "path_depth": len(path_segments),
        }

    @staticmethod
    def _render_review_item_row(item: dict[str, Any]) -> str:
        title_text = html.escape(str(item.get("title") or item.get("field_label") or item.get("page_hint") or "Recommendation"))
        page_hint = html.escape(str(item.get("page_hint") or "Current page"))
        current_value = html.escape(str(item.get("current_value") or "Unconfirmed"))
        recommended = html.escape(str(item.get("recommended_value") or item.get("recommended_range") or "Review manually"))
        why = html.escape(str(item.get("why_it_matters") or item.get("reasoning") or ""))
        evidence = html.escape(str(item.get("evidence") or ""))
        return (
            "<li class='ln-review-item'>"
            f"<strong>{title_text}</strong>"
            f"<span class='ln-review-page'>On: {page_hint}</span>"
            f"<span><strong>Current value</strong>: {current_value}</span>"
            f"<span class='ln-review-rec'><strong>Recommended</strong>: {recommended}</span>"
            f"<span><strong>Why it matters</strong>: {why}</span>"
            f"<span><strong>Evidence</strong>: {evidence}</span>"
            "</li>"
        )

    @staticmethod
    def _overlay_html(panel: dict[str, Any]) -> str:
        inferred_view = "session" if panel.get("session_id") or panel.get("review_batch") or panel.get("index_summary") or panel.get("actions") or panel.get("live_advice") or panel.get("goal") else "setup"
        view = str(panel.get("view", inferred_view))
        stage_raw = str(panel.get("stage", "idle"))
        title = html.escape(str(panel.get("title", "Live Navigator")))
        stage = html.escape(stage_raw.replace("_", " ").title())
        status = html.escape(str(panel.get("status", "Agent is ready.")))
        goal = html.escape(str(panel.get("goal", ""))[:220])
        site_check_summary = html.escape(str(panel.get("site_check_summary", "")))
        current_step = html.escape(str(panel.get("current_step", "")))
        progress = panel.get("progress")
        progress_value = 100 if progress is None else max(0, min(100, int(progress)))
        progress_style = f"width:{progress_value}%;"
        live_advice = panel.get("live_advice", [])[:5]
        logs = panel.get("logs", [])[:8]
        activity_log_tail = panel.get("activity_log_tail", [])[:5]
        sessions = panel.get("sessions", [])[:8]
        structure_map_preview = [str(item) for item in panel.get("structure_map_preview", [])[:8]]
        structure_map_total = max(int(panel.get("structure_map_total", 0) or 0), len(structure_map_preview))
        structure_manifest = dict(panel.get("structure_manifest", {}) or {})
        structure_nodes = [item for item in structure_manifest.get("nodes", []) if isinstance(item, dict)]
        review_batch = panel.get("review_batch") or {}
        review_items = [item for item in review_batch.get("items", []) if isinstance(item, dict)]
        index_summary = panel.get("index_summary") or {}
        actions = panel.get("actions", [])[:5]
        map_open = bool(panel.get("map_open") or panel.get("logs_open"))
        sessions_open = bool(panel.get("sessions_open"))
        review_open = bool(panel.get("review_open"))
        site_check_details = dict(panel.get("site_check_details", {}) or {})
        coverage_summary = dict(panel.get("coverage_summary", {}) or {})
        structure_map_summary = dict(panel.get("structure_map_summary", {}) or {})
        degraded_reason = html.escape(str(panel.get("degraded_reason", "") or ""))
        ax_summary = html.escape(str(panel.get("ax_summary", "") or ""))
        last_capture_at = html.escape(str(panel.get("last_capture_at", "") or ""))
        last_capture_path_raw = str(panel.get("last_capture_path", "") or "")
        last_capture_file = html.escape(last_capture_path_raw.rsplit("/", 1)[-1]) if last_capture_path_raw else ""
        last_capture_page = html.escape(str(panel.get("last_capture_page", "") or ""))
        last_capture_region = dict(panel.get("last_capture_region", {}) or {})
        mode = str(panel.get("mode") or ("complex_workspace" if view == "session" or str(panel.get("domain_pack", "")) == "marketplace_simulation" else "review_only"))
        review_ready = bool(panel.get("review_ready"))
        insufficiently_grounded = bool(panel.get("insufficiently_grounded"))

        preset_labels = {
            "generic_web": "General Web",
            "marketplace_simulation": "Complex Workspace",
        }
        mode_labels = {
            "complex_workspace": "Complex Workspace",
            "review_only": "Review Only",
        }
        scan_labels = {
            "lightweight": "Quick Scan",
            "adaptive": "Smart Scan",
            "advanced": "Deep Scan",
        }
        selected_domain = str(panel.get("domain_pack", "generic_web"))
        selected_index_mode = str(panel.get("index_mode", "adaptive"))
        smart_scan_available = bool(panel.get("smart_scan_available", True))
        if not smart_scan_available and selected_index_mode == "adaptive":
            selected_index_mode = "advanced"
        preset_label = html.escape(preset_labels.get(selected_domain, "Auto Detect"))
        mode_label = html.escape(mode_labels.get(mode, "Review Only"))
        scan_label = html.escape(scan_labels.get(selected_index_mode, "Smart Scan"))
        dock_mode = f"{mode_label} · {scan_label}" if view == "setup" else mode_label
        active_work_stages = {"indexing", "applying_batch", "planning"}
        auto_collapse_stages = {"indexing", "applying_batch", "planning", "live_advice"}
        show_work_controls = str(panel.get("stage", "idle")) in active_work_stages
        auto_collapse_rail = str(panel.get("auto_collapse_rail")).lower() == "true" or stage_raw in auto_collapse_stages
        active_session_id = html.escape(str(panel.get("active_session_id", "")))
        site_ready = bool(panel.get("site_ready"))
        post_index_disabled = "" if site_ready else " disabled"
        review_disabled = "" if review_ready else " disabled"

        advice_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in live_advice) or "<li>Waiting for review output.</li>"
        activity_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in activity_log_tail) or "<li>No recent activity yet.</li>"
        log_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in logs) or "<li>No logs yet.</li>"
        action_items = "".join(
            (
                "<li>"
                f"<span class='ln-action-kind'>{html.escape(str(item.get('action', 'suggest')))}</span>"
                f"<strong>{html.escape(str(item.get('target_text') or item.get('url') or item.get('value') or 'Next step'))}</strong>"
                f"<span>{html.escape(str(item.get('reasoning', '')))}</span>"
                "</li>"
            )
            for item in actions
        ) or "<li><strong>No executable batch.</strong><span>Read the review notes and apply manually if needed.</span></li>"

        session_buttons = "".join(
            f"<button class='ln-session-item' type='button' data-command='resume_session' data-session-id='{html.escape(str(item.get('session_id', '')))}'>{html.escape(str(item.get('project_name') or item.get('session_id') or 'Session'))}</button>"
            for item in sessions
        ) or "<p class='ln-summary'>No saved sessions yet. Start a new one first.</p>"

        active_session_markup = ""
        if active_session_id:
            active_session_markup = (
                "<div class='ln-command-row'>"
                f"<button type='button' class='ln-secondary' data-command='resume_session' data-session-id='{active_session_id}'>Back to Session</button>"
                "</div>"
            )

        setup_markup = f"""
          <div class="ln-setup-shell">
            <div class="ln-setup-scroll">
              <section class="ln-card">
                <p class="ln-label">Start or Resume</p>
                <form id="ln-setup-form" class="ln-setup-form" data-setup-form>
                  <label class="ln-field">
                    <span>Project</span>
                    <input data-field="project_name" type="text" value="{html.escape(str(panel.get('project_name', 'Navigator Session')))}" />
                  </label>
                  <label class="ln-field">
                    <span>Goal</span>
                    <textarea data-field="goal" rows="3">{html.escape(str(panel.get('goal', 'Help me navigate this website.')))}</textarea>
                  </label>
                  <label class="ln-field">
                    <span>Workspace Type</span>
                    <select data-field="domain_hint">
                      <option value="generic_web"{' selected' if selected_domain == 'generic_web' else ''}>General Web</option>
                      <option value="marketplace_simulation"{' selected' if selected_domain == 'marketplace_simulation' else ''}>Complex Workspace</option>
                    </select>
                  </label>
                  <label class="ln-field">
                    <span>How Thorough</span>
                    <select data-field="index_mode">
                      <option value="lightweight"{' selected' if selected_index_mode == 'lightweight' else ''}>Quick Scan</option>
                      <option value="adaptive"{' selected' if selected_index_mode == 'adaptive' else ''}{' disabled' if not smart_scan_available else ''}>{"Smart Scan" if smart_scan_available else "Smart Scan (requires prior memory)"}</option>
                      <option value="advanced"{' selected' if selected_index_mode == 'advanced' else ''}>Deep Scan</option>
                    </select>
                  </label>
                  <p class="ln-summary">General Web is for simpler sites and short workflows. Complex Workspace is for nested, recurring, data-heavy systems such as legacy business apps, internal tools, and Marketplace. Quick Scan reads the current page plus the closest relevant links. Smart Scan checks what changed and reuses memory. Deep Scan is recommended for complex workspaces because it builds reusable structure memory. It also explores linked pages for the current goal.</p>
                  {"<p class='ln-summary ln-beta'>Smart Scan turns on after the first indexed session creates site memory.</p>" if not smart_scan_available else ""}
                </form>
              </section>
              <section class="ln-card">
                <p class="ln-label">Saved Sessions</p>
                <div class="ln-session-list">{session_buttons}</div>
              </section>
            </div>
            <div class="ln-setup-footer">
              <div class="ln-command-row ln-setup-actions">
                <button type="submit" form="ln-setup-form" class="ln-primary">Start New Session</button>
              </div>
              {active_session_markup}
            </div>
          </div>
        """

        index_summary_markup = ""
        if index_summary:
            previous = "".join(f"<li>{html.escape(str(item))}</li>" for item in index_summary.get("previous_period_summary", [])) or "<li>No previous periods indexed yet.</li>"
            top_recs = "".join(f"<li>{html.escape(str(item))}</li>" for item in index_summary.get("top_recommendations", [])) or "<li>No recommendations yet.</li>"
            changes = "".join(f"<li>{html.escape(str(item))}</li>" for item in index_summary.get("detected_changes", [])) or "<li>No structure changes detected.</li>"
            index_summary_markup = f"""
              <section class="ln-card">
                <p class="ln-label">Index Summary</p>
                <p class="ln-summary">{html.escape(str(index_summary.get('strategic_summary', '')))}</p>
                <p class="ln-summary strong">{html.escape(str(index_summary.get('current_focus', '')))}</p>
                <div class="ln-split">
                  <div><p class="ln-label">Previous Quarters</p><ul class="ln-list">{previous}</ul></div>
                  <div><p class="ln-label">Current Focus</p><ul class="ln-list">{top_recs}</ul></div>
                </div>
                <p class="ln-label">Detected Changes</p>
                <ul class="ln-list">{changes}</ul>
              </section>
            """

        structure_checklist_markup = ""
        if site_check_details:
            matched_nodes = int(site_check_details.get("matched_nodes", 0) or 0)
            changed_nodes_count = int(site_check_details.get("changed_nodes_count", 0) or 0)
            new_nodes_count = int(site_check_details.get("new_nodes_count", 0) or 0)
            removed_nodes_count = int(site_check_details.get("removed_nodes_count", 0) or 0)
            current_node_count = int(site_check_details.get("current_node_count", 0) or 0)
            strategy = html.escape(str(site_check_details.get("strategy", "")).strip().lower())
            change_status = html.escape(str(site_check_details.get("change_status", "")).strip().title() or "Pending")
            strategy_summary = f"{strategy} refresh" if strategy else "pending refresh"
            structure_checklist_markup = f"""
              <section class="ln-card">
                <p class="ln-label">Structure Checklist</p>
                <p class="ln-summary strong">{change_status} · {html.escape(strategy_summary)}</p>
                <div class="ln-split">
                  <div><p class="ln-label">Reused</p><p class="ln-summary strong">{matched_nodes}</p></div>
                  <div><p class="ln-label">Changed</p><p class="ln-summary strong">{changed_nodes_count}</p></div>
                </div>
                <div class="ln-split">
                  <div><p class="ln-label">New</p><p class="ln-summary strong">{new_nodes_count}</p></div>
                  <div><p class="ln-label">Removed</p><p class="ln-summary strong">{removed_nodes_count}</p></div>
                </div>
                <p class="ln-label">Visible</p>
                <p class="ln-summary strong">{current_node_count}</p>
              </section>
            """

        review_summary_markup = ""
        if review_batch:
            summary = html.escape(str(review_batch.get("summary", "") or "Review ready."))
            focus = html.escape(str(review_batch.get("current_focus", "") or "Current page"))
            apply_ready = bool(review_batch.get("apply_ready"))
            apply_disabled = "" if apply_ready and mode == "complex_workspace" else " disabled"
            review_apply_button = (
                f'<div class="ln-command-row"><button type="button" class="ln-primary" data-command="apply_review_batch"{apply_disabled}>Apply Review (Beta)</button></div>'
                if mode == "complex_workspace"
                else ""
            )
            review_summary_markup = f"""
              <section class="ln-card">
                <p class="ln-label">Review Snapshot</p>
                <p class="ln-summary strong">{summary}</p>
                <p class="ln-summary">{focus}</p>
                <p class="ln-summary ln-beta">{html.escape(str(review_batch.get('beta_warning', 'Apply is beta. Manual application is safer.')))}</p>
                {review_apply_button}
                {'<p class="ln-summary ln-beta">Review needs a narrower rebuild before it is trustworthy.</p>' if insufficiently_grounded else ''}
              </section>
            """

        capture_markup = ""
        if last_capture_at or last_capture_file or last_capture_page:
            capture_details = []
            if last_capture_at:
                capture_details.append(f"<p class='ln-summary'><strong>Time</strong>: {last_capture_at}</p>")
            if last_capture_file:
                capture_details.append(f"<p class='ln-summary'><strong>Screenshot</strong>: {last_capture_file}</p>")
            if last_capture_page:
                capture_details.append(f"<p class='ln-summary'><strong>Page</strong>: {last_capture_page}</p>")
            slice_count = int(last_capture_region.get("slice_count", 0) or 0)
            scroll_height = int(last_capture_region.get("scroll_height", 0) or 0)
            viewport_height = int(last_capture_region.get("viewport_height", 0) or 0)
            if slice_count > 0:
                below_fold_slices = max(0, slice_count - 1)
                capture_details.append(
                    f"<p class='ln-summary'><strong>Viewport slices</strong>: {slice_count} total, {below_fold_slices} below the fold.</p>"
                )
            if scroll_height > 0 and viewport_height > 0:
                capture_details.append(
                    f"<p class='ln-summary'><strong>Coverage</strong>: {scroll_height}px page, {viewport_height}px viewport.</p>"
                )
            capture_markup = f"""
              <section class="ln-card">
                <p class="ln-label">Last Capture</p>
                {''.join(capture_details)}
              </section>
            """

        sessions_markup = ""
        if sessions_open:
            sessions_markup = f"""
              <section class="ln-card">
                <p class="ln-label">Saved Sessions</p>
                <div class="ln-session-list">{session_buttons}</div>
              </section>
            """

        map_markup = ""
        if map_open:
            manifest_items = "".join(
                f"<li><strong>{html.escape(str(item.get('title') or item.get('url') or 'Node'))}</strong><span>{html.escape(str(item.get('url') or ''))}</span></li>"
                for item in structure_nodes
            ) or "".join(f"<li>{html.escape(item)}</li>" for item in structure_map_preview) or "<li>No indexed nodes yet.</li>"
            map_markup = f"""
              <section class="ln-card">
                <p class="ln-label">Map & Coverage Inspector</p>
                <p class="ln-label">Structure Map</p>
                <p class="ln-summary">Showing {len(structure_map_preview)} of {structure_map_total} indexed nodes.</p>
                <div class="ln-split">
                  <div><p class="ln-label">Discovered</p><p class="ln-summary strong">{int(coverage_summary.get('discovered_nodes', 0) or 0)}</p></div>
                  <div><p class="ln-label">Indexed</p><p class="ln-summary strong">{int(coverage_summary.get('indexed_nodes', 0) or 0)}</p></div>
                </div>
                <div class="ln-split">
                  <div><p class="ln-label">Skipped</p><p class="ln-summary strong">{int(coverage_summary.get('skipped_nodes', 0) or 0)}</p></div>
                  <div><p class="ln-label">Blocked</p><p class="ln-summary strong">{int(coverage_summary.get('blocked_nodes', 0) or 0)}</p></div>
                </div>
                <p class="ln-label">Alias-collapsed</p>
                <p class="ln-summary strong">{int(coverage_summary.get('alias_collapsed_nodes', 0) or 0)}</p>
                <p class="ln-label">Active Node</p>
                <p class="ln-summary">{html.escape(str(structure_map_summary.get('active_node', 'Unconfirmed')))}</p>
                <p class="ln-label">Mode</p>
                <p class="ln-summary">{mode_label}</p>
                {"<p class='ln-label'>Editable Quarter</p><p class='ln-summary'>" + html.escape(str(structure_map_summary.get('editable_quarter', ''))) + "</p>" if structure_map_summary.get("editable_quarter") not in (None, "") else ""}
                <p class="ln-label">Normalized Website Map</p>
                <ul class="ln-action-list">{manifest_items}</ul>
                <p class="ln-label">Recent Events</p>
                <ul class="ln-list">{log_items}</ul>
              </section>
            """

        review_markup = ""
        if review_open and review_batch:
            grouped_rows = [LocalBrowserBridge._render_review_item_row(item) for item in review_items[:24]]
            items_markup = "".join(grouped_rows) or "<li class='ln-review-item'><strong>No review items yet.</strong><span>The current review did not find concrete field-level changes.</span></li>"
            apply_ready = bool(review_batch.get("apply_ready"))
            apply_disabled = "" if apply_ready and mode == "complex_workspace" else " disabled"
            full_review_apply_button = (
                f'<div class="ln-command-row"><button type="button" class="ln-primary" data-command="apply_review_batch"{apply_disabled}>Apply Review (Beta)</button></div>'
                if mode == "complex_workspace"
                else ""
            )
            comparison_details = ""
            comparison_payload = dict(review_batch.get("comparison_payload", {}) or {})
            best_match = dict(comparison_payload.get("best_match", {}) or {})
            if best_match:
                comparison_details = f"<p class='ln-summary'><strong>Best match</strong>: {html.escape(str(best_match.get('name', 'Unconfirmed')))} at {html.escape(str(best_match.get('price', 'unconfirmed price')))}.</p>"
            review_markup = f"""
              <section class="ln-card">
                <p class="ln-label">Full Review</p>
                <p class="ln-summary strong">{html.escape(str(review_batch.get('summary', '')))}</p>
                <p class="ln-summary">{html.escape(str(review_batch.get('current_focus', '')))}</p>
                {comparison_details}
                <ul class="ln-action-list">{items_markup}</ul>
                <p class="ln-summary ln-beta">{html.escape(str(review_batch.get('beta_warning', 'Apply is beta. Manual application is safer.')))}</p>
                {full_review_apply_button}
              </section>
            """

        command_buttons = [
            '<button type="button" class="ln-primary" data-command="start_index">Index Site First</button>',
        ]
        if mode == "complex_workspace":
            command_buttons.append(f'<button type="button" class="ln-secondary" data-command="enter_live_advice"{post_index_disabled}>Live Notes</button>')
        command_buttons.append(f'<button type="button" class="ln-secondary" data-command="prepare_review_batch"{post_index_disabled}>Refresh Review</button>')
        command_buttons.append(f'<button type="button" class="ln-secondary" data-command="open_review"{review_disabled}>See Review</button>')
        command_buttons.append('<button type="button" class="ln-secondary" data-command="show_setup">Start Another Session</button>')
        command_buttons.append('<button type="button" class="ln-muted" data-command="open_sessions">Saved Sessions</button>')

        executable_markup = ""
        if mode == "complex_workspace":
            executable_markup = f"""
              <section class="ln-card">
                <p class="ln-label">Executable Batch</p>
                <ul class="ln-action-list">{action_items}</ul>
              </section>
            """

        session_markup = f"""
          <section class="ln-card">
            <p class="ln-label">Current Mission</p>
            <p class="ln-goal">{goal}</p>
            <div class="ln-command-grid">{''.join(command_buttons)}</div>
          </section>
          <section class="ln-card">
            <p class="ln-label">Status</p>
            <div class="ln-stage-inline"><span class="ln-pulse"></span><strong>{stage}</strong></div>
            <p class="ln-summary">{status}</p>
            <p class="ln-summary">{mode_label}</p>
            {"<p class='ln-summary'>" + ax_summary + "</p>" if ax_summary else ""}
            {"<p class='ln-summary ln-beta'>" + degraded_reason + "</p>" if degraded_reason else ""}
          </section>
          <section class="ln-card">
            <p class="ln-label">Index Progress</p>
            <div class="ln-progress-track"><div class="ln-progress-fill" style="{progress_style}"></div></div>
            <p class="ln-summary">{current_step or site_check_summary}</p>
          </section>
          {capture_markup}
          {structure_checklist_markup}
          {index_summary_markup}
          {review_summary_markup}
          <section class="ln-card">
            <p class="ln-label">How This Works</p>
            <p class="ln-summary">The overlay checks structure changes first, reuses cached memory where possible, prepares a full review, and then keeps the current workflow inspectable through review and coverage surfaces.</p>
          </section>
          <section class="ln-card">
            <p class="ln-label">{'Live Notes' if mode == 'complex_workspace' else 'Summary First'}</p>
            <ul class="ln-list">{advice_items}</ul>
          </section>
          {sessions_markup}
          {review_markup}
          {executable_markup}
          {map_markup}
        """

        body_markup = setup_markup if view == "setup" else session_markup
        close_button_markup = '<button class="ln-close" type="button" data-command="stop_session" aria-label="Stop session">×</button>' if show_work_controls else ''
        rail_toggle_markup = '<button class="ln-rail-toggle" type="button" data-rail-toggle aria-expanded="true" aria-label="Collapse details panel">›</button>' if view == "session" else ""
        dock_markup = ""
        if view == "session":
            degraded_line = f"<span class='ln-site-status ln-beta'>{degraded_reason}</span>" if degraded_reason else ""
            stop_button = '<button type="button" class="ln-dock-stop" data-command="stop_session">Stop</button>' if show_work_controls else ""
            dock_markup = f"""
          <div class="ln-dock">
            <div class="ln-dock-left">
              <span class="ln-site-chip">{html.escape(str(panel.get('site_label', title)))}</span>
              <span class="ln-site-mode">Current phase</span>
              <span class="ln-site-status">{stage}</span>
              <span class="ln-site-mode">Latest events</span>
              <ul class="ln-inline-list">{activity_items}</ul>
              {degraded_line}
            </div>
            <div class="ln-dock-actions">
              <button type="button" class="ln-dock-map" data-command="open_map">View Map</button>
              {stop_button}
            </div>
          </div>
            """
        return f"""
        <div class="ln-overlay-shell" data-auto-collapse-rail="{str(auto_collapse_rail).lower()}" data-rail-collapsed="false">
          <div class="ln-stage-wash"></div>
          <div class="ln-agent-cursor"><span></span><em>{html.escape(current_step or status)}</em></div>
          <aside class="ln-rail{' ln-rail-setup' if view == 'setup' else ''}">
            <div class="ln-header">
              <div>
                <p class="ln-kicker">Agent Mode</p>
                <h2>{title}</h2>
              </div>
              {close_button_markup}
            </div>
            {body_markup}
          </aside>
          {rail_toggle_markup}
          {dock_markup}
        </div>
        """

    @staticmethod
    def _overlay_css() -> str:
        return (
            """
        :host { all: initial; }
        .ln-overlay-shell {
          all: initial;
          font-family: 'SF Pro Display', Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          --ln-rail-width: min(__RAIL_WIDTH__px, calc(100vw - 34px));
          --ln-dock-clearance: 0px;
          position: fixed;
          inset: 0;
          pointer-events: none;
          z-index: 2147483647;
        }
        [data-rail-collapsed="false"] {
          --ln-dock-clearance: calc(var(--ln-rail-width) + 80px);
        }
        .ln-stage-wash {
          position: fixed;
          inset: 0;
          background: radial-gradient(circle at 52% 28%, rgba(93, 168, 255, 0.16), transparent 35%), linear-gradient(180deg, rgba(14, 22, 40, 0.08), rgba(14, 22, 40, 0));
          opacity: 0;
          transition: opacity .18s ease;
        }
        [data-stage="indexing"] .ln-stage-wash, [data-stage="applying_batch"] .ln-stage-wash {
          opacity: 1;
        }
        .ln-rail {
          box-sizing: border-box;
          position: fixed;
          top: 20px;
          right: 20px;
          width: var(--ln-rail-width);
          max-height: calc(100dvh - 32px);
          overflow: auto;
          padding: 14px;
          border-radius: 18px;
          color: #f8fafc;
          background: linear-gradient(180deg, rgba(8, 12, 20, 0.97), rgba(10, 15, 26, 0.94));
          border: 1px solid rgba(122, 162, 255, 0.18);
          box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
          backdrop-filter: blur(20px);
          pointer-events: auto;
          transition: transform .22s ease, opacity .18s ease, box-shadow .18s ease;
        }
        .ln-rail-toggle {
          position: fixed;
          top: 120px;
          right: calc(20px + var(--ln-rail-width) - 4px);
          width: 42px;
          height: 64px;
          border: 1px solid rgba(122, 162, 255, 0.22);
          border-right: none;
          border-radius: 18px 0 0 18px;
          background: linear-gradient(180deg, rgba(10, 14, 23, 0.98), rgba(14, 20, 34, 0.96));
          color: #e2e8f0;
          font-size: 22px;
          line-height: 1;
          font-weight: 700;
          cursor: pointer;
          pointer-events: auto;
          box-shadow: 0 16px 44px rgba(0,0,0,.28);
          transition: right .22s ease, background .18s ease, transform .12s ease;
        }
        .ln-rail-toggle:hover {
          background: linear-gradient(180deg, rgba(30, 41, 59, 0.98), rgba(15, 23, 42, 0.98));
        }
        [data-rail-collapsed="true"] .ln-rail {
          transform: translateX(calc(100% + 28px));
          opacity: 0;
          pointer-events: none;
          box-shadow: none;
        }
        [data-rail-collapsed="true"] .ln-rail-toggle {
          right: 20px;
        }
        .ln-rail-setup {
          top: 14px;
          max-height: calc(100dvh - 24px);
          padding: 14px;
          display: flex;
          flex-direction: column;
          overflow: hidden;
        }
        .ln-agent-cursor {
          position: fixed;
          right: __CURSOR_RIGHT__px;
          top: 180px;
          width: max-content;
          max-width: 220px;
          display: flex;
          align-items: center;
          gap: 10px;
          opacity: 0;
          pointer-events: none;
          animation: ln-cursor-drift 2.4s ease-in-out infinite;
        }
        .ln-agent-cursor span {
          display: block;
          width: 18px;
          height: 18px;
          border-radius: 999px;
          background: radial-gradient(circle at 30% 30%, #fff, #60a5fa 60%, #1d4ed8 100%);
          box-shadow: 0 0 18px rgba(96,165,250,.55);
          flex: 0 0 auto;
        }
        .ln-agent-cursor em {
          font-style: normal;
          color: #fff;
          background: rgba(10, 14, 23, 0.94);
          border: 1px solid rgba(255,255,255,.08);
          border-radius: 999px;
          padding: 8px 12px;
          font-size: 13px;
          line-height: 1.2;
        }
        [data-stage="indexing"] .ln-agent-cursor, [data-stage="applying_batch"] .ln-agent-cursor {
          opacity: 1;
        }
        .ln-header {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 12px;
          margin-bottom: 10px;
        }
        .ln-kicker, .ln-label {
          margin: 0 0 6px;
          color: rgba(148, 163, 184, 0.88);
          text-transform: uppercase;
          letter-spacing: .12em;
          font-size: 11px;
          font-weight: 700;
        }
        .ln-header h2 {
          margin: 0;
          font-size: 24px;
          line-height: 1.05;
          font-weight: 700;
        }
        .ln-close, .ln-dock-actions button, .ln-primary, .ln-secondary, .ln-muted, .ln-session-item {
          border: 0;
          border-radius: 14px;
          padding: 11px 14px;
          cursor: pointer;
          font-size: 14px;
          font-weight: 700;
          transition: transform .12s ease, box-shadow .18s ease, opacity .18s ease, background .18s ease;
        }
        .ln-close:hover, .ln-dock-actions button:hover, .ln-primary:hover, .ln-secondary:hover, .ln-muted:hover, .ln-session-item:hover {
          box-shadow: 0 10px 24px rgba(15, 23, 42, 0.18);
        }
        .ln-close:active, .ln-dock-actions button:active, .ln-primary:active, .ln-secondary:active, .ln-muted:active, .ln-session-item:active,
        .ln-clicked {
          transform: translateY(1px) scale(.985);
          box-shadow: 0 10px 24px rgba(37, 99, 235, 0.28);
        }
        .ln-close { background: rgba(255,255,255,.08); color: #fff; width: 42px; height: 42px; }
        .ln-card {
          padding: 8px 10px;
          border-radius: 14px;
          background: rgba(255,255,255,.04);
          border: 1px solid rgba(255,255,255,.06);
          margin-bottom: 10px;
        }
        .ln-field { display: grid; gap: 5px; margin-bottom: 8px; }
        .ln-field span { color: rgba(191, 219, 254, 0.9); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
        .ln-field input, .ln-field textarea, .ln-field select {
          border: 1px solid rgba(255,255,255,.08);
          border-radius: 14px;
          background: rgba(255,255,255,.05);
          color: #fff;
          padding: 10px 12px;
          font: inherit;
        }
        .ln-setup-shell {
          display: flex;
          flex: 1 1 auto;
          min-height: 0;
          flex-direction: column;
          gap: 10px;
        }
        .ln-setup-scroll {
          display: grid;
          gap: 10px;
          flex: 1 1 auto;
          min-height: 0;
          overflow: auto;
          padding-right: 2px;
        }
        .ln-setup-form { display: grid; gap: 6px; }
        .ln-setup-footer {
          flex: 0 0 auto;
          display: grid;
          gap: 10px;
          margin-top: 2px;
          padding-top: 12px;
          border-top: 1px solid rgba(122, 162, 255, 0.12);
          background: linear-gradient(180deg, rgba(8,12,20,0.18), rgba(8,12,20,.94));
          backdrop-filter: blur(18px);
        }
        .ln-setup-actions {
          margin: 0;
        }
        .ln-command-grid, .ln-command-row { display: grid; gap: 10px; }
        .ln-command-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 10px; }
        .ln-primary { background: linear-gradient(180deg, #37d996, #12b981); color: #04111f; }
        .ln-secondary { background: linear-gradient(180deg, rgba(93,168,255,.18), rgba(93,168,255,.1)); color: #eff6ff; }
        .ln-muted, .ln-session-item { background: rgba(255,255,255,.08); color: #fff; width: 100%; text-align: left; }
        .ln-primary[disabled], .ln-secondary[disabled], .ln-muted[disabled], .ln-session-item[disabled] { opacity: .45; cursor: not-allowed; box-shadow: none; }
        .ln-session-list { display: grid; gap: 8px; }
        .ln-summary, .ln-goal, .ln-list li, .ln-action-list span { margin: 0; color: rgba(226,232,240,.9); font-size: 14px; line-height: 1.45; }
        .ln-summary.strong { color: #fff; font-weight: 700; margin-bottom: 8px; }
        .ln-progress-track { width: 100%; height: 10px; border-radius: 999px; overflow: hidden; background: rgba(255,255,255,.08); margin: 10px 0 12px; }
        .ln-progress-fill { height: 100%; border-radius: 999px; background: linear-gradient(90deg, #5da8ff, #34d399); transition: width .35s ease; }
        .ln-list, .ln-action-list { margin: 0; padding-left: 18px; display: grid; gap: 8px; }
        .ln-inline-list { margin: 0; padding-left: 18px; display: grid; gap: 6px; color: rgba(226,232,240,.92); }
        .ln-action-list li { display: grid; gap: 4px; }
        .ln-action-list strong { font-size: 15px; color: #fff; }
        .ln-action-kind { display: inline-flex; width: fit-content; padding: 4px 8px; border-radius: 999px; background: rgba(96,165,250,.14); color: #93c5fd; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; font-weight: 700; }
        .ln-stage-inline { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
        .ln-pulse { width: 12px; height: 12px; border-radius: 999px; background: #60a5fa; box-shadow: 0 0 0 rgba(96,165,250,.65); animation: ln-pulse 1.8s infinite; flex: 0 0 auto; }
        .ln-split { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
        .ln-dock {
          box-sizing: border-box;
          position: fixed;
          left: calc((100vw - var(--ln-dock-clearance)) / 2);
          right: auto;
          bottom: 24px;
          transform: translateX(-50%);
          width: min(__DOCK_WIDTH__px, calc(100vw - 48px - var(--ln-dock-clearance)));
          max-width: calc(100vw - 48px - var(--ln-dock-clearance));
          padding: 10px 14px;
          border-radius: 18px;
          color: #fff;
          background: linear-gradient(180deg, rgba(10, 14, 23, 0.98), rgba(14, 20, 34, 0.96));
          border: 1px solid rgba(96,165,250,.26);
          box-shadow: 0 18px 70px rgba(0,0,0,.4);
          backdrop-filter: blur(22px);
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 16px;
          pointer-events: auto;
        }
        .ln-dock-left { display: flex; flex-direction: column; gap: 4px; }
        .ln-dock-actions { display: flex; align-items: center; gap: 8px; }
        .ln-site-chip { display: inline-flex; width: fit-content; padding: 3px 8px; border-radius: 999px; background: rgba(96,165,250,.16); color: #bfdbfe; font-size: 11px; font-weight: 700; }
        .ln-site-status { color: rgba(226,232,240,.92); font-size: 12px; overflow-wrap: anywhere; }
        .ln-site-mode { color: rgba(148, 163, 184, 0.9); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
        .ln-beta { color: #fda4af; }
        .ln-review-item { display: grid; gap: 6px; }
        .ln-review-page { color: #93c5fd; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }
        .ln-review-rec { color: #f8fafc; font-weight: 600; }
        .ln-dock-actions button { color: #fff; }
        .ln-dock-map { background: linear-gradient(180deg, rgba(96,165,250,.94), rgba(14,165,233,.92)); }
        .ln-dock-stop { background: linear-gradient(180deg, #ff6b6b, #ef4444); }
        @keyframes ln-pulse {
          0% { box-shadow: 0 0 0 0 rgba(96,165,250,.55); }
          70% { box-shadow: 0 0 0 16px rgba(96,165,250,0); }
          100% { box-shadow: 0 0 0 0 rgba(96,165,250,0); }
        }
        @keyframes ln-cursor-drift {
          0% { transform: translate3d(0, 0, 0) scale(1); }
          30% { transform: translate3d(-70px, 34px, 0) scale(1.06); }
          60% { transform: translate3d(-24px, 120px, 0) scale(.96); }
          100% { transform: translate3d(0, 0, 0) scale(1); }
        }
        @media (max-width: 980px) {
          .ln-overlay-shell { --ln-dock-clearance: 0px; }
          .ln-rail { top: auto; right: 16px; left: 16px; bottom: 112px; width: auto; max-height: min(54dvh, 540px); }
          .ln-rail-toggle { top: auto; right: 18px; bottom: 196px; }
          [data-rail-collapsed="true"] .ln-rail { transform: translateY(calc(100% + 20px)); }
          [data-rail-collapsed="true"] .ln-rail-toggle { right: 18px; bottom: 128px; }
          .ln-dock { left: 12px; right: 12px; width: auto; bottom: 12px; transform: none; }
          .ln-agent-cursor { right: 24px; top: auto; bottom: 180px; }
          .ln-split, .ln-command-grid { grid-template-columns: 1fr; }
        }
        """
            .replace("__RAIL_WIDTH__", str(OVERLAY_RAIL_WIDTH_PX))
            .replace("__CURSOR_RIGHT__", str(OVERLAY_AGENT_CURSOR_RIGHT_PX))
            .replace("__DOCK_WIDTH__", str(OVERLAY_DOCK_WIDTH_PX))
        )
