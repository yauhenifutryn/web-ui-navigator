from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from marketplace_bot.logging_json import log_event
from marketplace_bot.state_store import StateStore, utc_now_iso

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    async_playwright = None


@dataclass
class Crawler:
    cdp_url: str
    state_store: StateStore | None = None
    target_domain: str = "play.marketplace-simulation.com"
    _playwright: Any = None
    _browser: Any = None
    click_timeout_ms: int = 2500
    nav_wait_timeout_ms: int = 4000

    async def attach(self) -> None:
        if async_playwright is None:
            raise RuntimeError("playwright is not installed")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
        log_event("crawler", "attached", cdp_url=self.cdp_url)

    def list_pages(self) -> list[Any]:
        if self._browser is None:
            return []
        pages: list[Any] = []
        for context in self._browser.contexts:
            pages.extend(context.pages)
        return pages

    def _all_pages(self) -> list[Any]:
        return self.list_pages()

    def has_usable_browser(self) -> bool:
        return self._browser is not None and bool(self.list_pages())

    def browser_contexts(self) -> list[Any]:
        return list(getattr(self._browser, "contexts", []) or [])

    def detect_quarter_number(self, url: str, semantic_text: str) -> int:
        return self._detect_quarter_number(url, semantic_text)

    async def wait_after_navigation(self, page: Any) -> None:
        await self._wait_after_navigation(page)

    async def scrape_page_snapshot(
        self,
        page: Any,
        *,
        quarter_number: int,
        editable: bool = False,
        section_limit: int = 14,
    ) -> dict[str, Any]:
        return await self._scrape_page_snapshot(
            page,
            quarter_number=quarter_number,
            editable=editable,
            section_limit=section_limit,
        )

    async def get_active_page(self) -> Any:
        pages = self.list_pages()
        if not pages:
            raise RuntimeError("No open pages found in attached Chrome instance")

        for page in pages:
            url = getattr(page, "url", "") or ""
            if self.target_domain and self.target_domain in url:
                log_event("crawler", "active_page_selected", strategy="target_domain", url=url)
                return page

        for page in pages:
            url = getattr(page, "url", "") or ""
            if url.startswith("http"):
                log_event("crawler", "active_page_selected", strategy="first_http", url=url)
                return page

        selected = pages[0]
        log_event("crawler", "active_page_selected", strategy="first_page", url=getattr(selected, "url", ""))
        return selected

    async def get_current_page_title(self) -> str:
        page = await self.get_active_page()
        return await page.title()

    async def extract_semantic_text(self, page: Any) -> str:
        for attempt in range(2):
            try:
                text = await page.evaluate("document.body.innerText")
                if not isinstance(text, str):
                    return ""
                return text.strip()
            except Exception as exc:
                if attempt == 1 or "Execution context was destroyed" not in str(exc):
                    raise
                await self._wait_after_navigation(page)
        return ""

    async def discover_navigation_items(self, page: Any) -> list[str]:
        menu_items: list[str] = []
        await self._ensure_marketplace_workspace_tab(page)
        try:
            nav = page.get_by_role("navigation")
            links = nav.get_by_role("link")
            count = await links.count()
            for idx in range(min(count, 40)):
                try:
                    label = (await links.nth(idx).inner_text()).strip()
                except Exception as item_exc:
                    log_event("crawler", "navigation_item_text_failed", index=idx, error=str(item_exc))
                    continue
                if self._looks_like_nav_label(label):
                    menu_items.append(label)
        except Exception as exc:
            log_event("crawler", "navigation_discovery_failed", error=str(exc))

        if not menu_items:
            try:
                links = page.get_by_role("link")
                count = await links.count()
                for idx in range(min(count, 120)):
                    try:
                        label = (await links.nth(idx).inner_text()).strip()
                    except Exception as item_exc:
                        log_event("crawler", "global_link_text_failed", index=idx, error=str(item_exc))
                        continue
                    if self._looks_like_nav_label(label):
                        menu_items.append(label)
            except Exception as exc:
                log_event("crawler", "global_link_discovery_failed", error=str(exc))

        try:
            raw_labels = await page.evaluate(
                """
                () => Array.from(
                  document.querySelectorAll(
                    "span.taskListItemTitle, [data-test^='tli'], .taskListItemTitle, .workspaceListItemTitle"
                  )
                )
                  .map((el) => {
                    const text = (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const visible = !!text && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                    return { text, visible };
                  })
                  .filter((item) => item.text)
                """
            )
            if isinstance(raw_labels, list):
                for item in raw_labels:
                    if isinstance(item, dict):
                        if not bool(item.get("visible")):
                            continue
                        label = str(item.get("text", "")).strip()
                    else:
                        label = str(item).strip()
                    if label:
                        menu_items.append(label)
        except Exception as exc:
            log_event("crawler", "raw_dom_navigation_discovery_failed", error=str(exc))

        if not menu_items:
            semantic_text = await self.extract_semantic_text(page)
            for line in semantic_text.splitlines():
                label = line.strip()
                if self._looks_like_nav_label(label):
                    menu_items.append(label)

        # Explicitly mine the left "Quarter Menu" block from semantic text.
        # This prevents quick-links from dominating discovery when ARIA roles are weak.
        semantic_text = await self.extract_semantic_text(page)
        quarter_menu_items = self._extract_quarter_menu_items_from_text(semantic_text)
        if quarter_menu_items:
            menu_items = [*quarter_menu_items, *menu_items]

        deduped: list[str] = []
        seen = set()
        is_marketplace = "marketplace-simulation.com" in (getattr(page, "url", "") or "")
        for item in menu_items:
            is_allowed = self._is_actionable_menu_item(item) if is_marketplace else self._looks_like_nav_label(item)
            if item not in seen and is_allowed:
                seen.add(item)
                deduped.append(item)
        return deduped

    async def crawl_navigation_sections(self, page: Any, limit: int = 14) -> list[dict[str, str]]:
        sections: list[dict[str, str]] = []
        visited: set[str] = set()
        await self._set_agent_overlay_interactive(page, interactive=False)
        try:
            await self._ensure_marketplace_workspace_tab(page)

            async def _crawl_visible_items(depth: int = 0, baseline_items: set[str] | None = None) -> None:
                if depth > 3 or len(sections) >= limit:
                    return
                visible_items = await self.discover_navigation_items(page)
                active_keys = await self._current_marketplace_active_keys(page)
                candidate_items = [
                    item
                    for item in visible_items
                    if (
                        item not in visited
                        and (baseline_items is None or item not in baseline_items)
                        and self._normalize_marketplace_key(item) not in active_keys
                    )
                ]
                for name in candidate_items:
                    if len(sections) >= limit:
                        return
                    visited.add(name)
                    parent_url = str(getattr(page, "url", "") or "")
                    try:
                        await self._dismiss_known_overlays(page)
                        await self._click_navigation_item(page, name)
                        await self._wait_after_navigation(page)
                        section_text = await self.extract_semantic_text(page)
                        section_navigation = await self.discover_navigation_items(page)
                        sections.append(
                            {
                                "menu_item": name,
                                "url": str(getattr(page, "url", "") or ""),
                                "parent_url": parent_url,
                                "semantic_text": section_text,
                                "navigation_items": section_navigation,
                            }
                        )
                        await _crawl_visible_items(depth + 1, baseline_items=set(visible_items))
                    except Exception as item_exc:
                        log_event(
                            "crawler",
                            "navigation_item_failed",
                            menu_item=name,
                            error=str(item_exc),
                        )

            await _crawl_visible_items()
        finally:
            await self._set_agent_overlay_interactive(page, interactive=True)
        return sections

    async def scrape_current_world_state(self) -> dict[str, Any]:
        page = await self.get_active_page()
        await self._ensure_marketplace_workspace_tab(page)
        semantic_text = await self.extract_semantic_text(page)
        quarter_num = self.detect_quarter_number(page.url, semantic_text)
        payload = await self._scrape_page_snapshot(page, quarter_number=quarter_num, editable=True)

        if self.state_store is not None:
            self.state_store.write_latest_scrape(self._render_scrape_text([payload]))
        return payload

    async def scrape_completed_quarters_world_state(self, page: Any | None = None) -> dict[str, Any]:
        page = page or await self.get_active_page()
        await self._ensure_marketplace_workspace_tab(page)
        current_text = await self.extract_semantic_text(page)
        current_quarter = self.detect_quarter_number(page.url, current_text)
        if current_quarter < 1:
            current_quarter = 1

        current_snapshot = await self._scrape_page_snapshot(
            page,
            quarter_number=current_quarter,
            editable=True,
            section_limit=24,
        )
        snapshots = [current_snapshot]

        all_navigation: list[str] = []
        semantic_parts: list[str] = []
        total_sections = 0
        for snap in snapshots:
            all_navigation.extend(snap.get("navigation_items", []))
            semantic_parts.append(
                f"QUARTER {snap.get('quarter_number', '?')} | TITLE: {snap.get('title', '')}\n"
                f"{snap.get('semantic_text', '')}"
            )
            for section in snap.get("sections", []):
                semantic_parts.append(
                    f"QUARTER {snap.get('quarter_number', '?')} | SECTION: {section.get('menu_item', '')}\n"
                    f"{section.get('semantic_text', '')}"
                )
            total_sections += len(snap.get("sections", []))

        dedup_nav: list[str] = []
        seen = set()
        for item in all_navigation:
            if item not in seen:
                seen.add(item)
                dedup_nav.append(item)

        aggregate_payload = {
            "captured_at": utc_now_iso(),
            "title": snapshots[-1]["title"] if snapshots else "",
            "url": snapshots[-1]["url"] if snapshots else page.url,
            "semantic_text": "\n\n".join(semantic_parts),
            "navigation_items": dedup_nav,
            "sections": [sec for snap in snapshots for sec in snap.get("sections", [])],
            "completed_quarters": snapshots,
            "quarter_range": {"start": current_quarter, "end": current_quarter},
            "editable_quarter": current_quarter,
            "history_scope": "current_quarter_only",
        }

        if self.state_store is not None:
            self.state_store.write_latest_scrape(self._render_scrape_text(snapshots))

        log_event(
            "crawler",
            "completed_quarters_scraped",
            quarters=len(snapshots),
            navigation_count=len(dedup_nav),
            sections=total_sections,
        )
        return aggregate_payload

    async def _scrape_quarter_summary_snapshot(self, page: Any, quarter_number: int) -> dict[str, Any]:
        await self._ensure_marketplace_workspace_tab(page)
        await self._dismiss_known_overlays(page)
        summary_targets = ("Summary of Decisions", "Final Check", "Submit")
        await self._set_agent_overlay_interactive(page, interactive=False)
        try:
            for name in summary_targets:
                clicked = False
                for role in ("link", "button", "tab", "menuitem"):
                    try:
                        await page.get_by_role(role, name=name).first.click(timeout=self.click_timeout_ms)
                        clicked = True
                        break
                    except Exception:
                        continue
                if not clicked:
                    try:
                        await page.get_by_text(name, exact=False).first.click(timeout=self.click_timeout_ms)
                        clicked = True
                    except Exception:
                        clicked = False
                if clicked:
                    await self._wait_after_navigation(page)
                    break
        finally:
            await self._set_agent_overlay_interactive(page, interactive=True)

        title = await page.title()
        url = page.url
        semantic_text = await self.extract_semantic_text(page)
        summary_label = next((label for label in summary_targets if label.lower() in title.lower() or label.lower() in semantic_text.lower()), "Summary of Decisions")
        return {
            "captured_at": utc_now_iso(),
            "quarter_number": quarter_number,
            "editable": False,
            "title": title or f"Quarter {quarter_number} Summary",
            "url": url,
            "semantic_text": semantic_text,
            "page_text_excerpt": semantic_text[:1800],
            "navigation_items": [summary_label],
            "sections": [],
            "section_previews": [],
            "summary_only": True,
        }

    async def close(self) -> None:
        # Do not close the user's Chrome instance attached via CDP.
        # Only drop local references and stop playwright transport.
        if self._browser is not None:
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        log_event("crawler", "closed")

    async def _scrape_page_snapshot(self, page: Any, quarter_number: int, editable: bool = False, section_limit: int = 14) -> dict[str, Any]:
        await self._ensure_marketplace_workspace_tab(page)
        title = await page.title()
        url = page.url
        semantic_text = await self.extract_semantic_text(page)
        nav_items = await self.discover_navigation_items(page)
        try:
            section_texts = await self.crawl_navigation_sections(page, limit=section_limit)
        except TypeError:
            section_texts = await self.crawl_navigation_sections(page)
        for section in section_texts:
            for item in section.get("navigation_items", []) or []:
                if item not in nav_items:
                    nav_items.append(item)
        page_text_excerpt = semantic_text[:2400]
        section_previews = [
            {
                "menu_item": section.get("menu_item", ""),
                "semantic_text_excerpt": str(section.get("semantic_text", ""))[:1200],
            }
            for section in section_texts[:12]
        ]
        log_event(
            "crawler",
            "world_state_scraped",
            quarter=quarter_number,
            navigation_count=len(nav_items),
            sections=len(section_texts),
        )
        return {
            "captured_at": utc_now_iso(),
            "quarter_number": quarter_number,
            "editable": editable,
            "title": title,
            "url": url,
            "semantic_text": semantic_text,
            "page_text_excerpt": page_text_excerpt,
            "navigation_items": nav_items,
            "sections": section_texts,
            "section_previews": section_previews,
        }

    def _render_scrape_text(self, snapshots: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for snap in snapshots:
            lines.extend(
                [
                    f"QUARTER: {snap.get('quarter_number')}",
                    f"TITLE: {snap.get('title')}",
                    f"URL: {snap.get('url')}",
                    "",
                    "PAGE_TEXT:",
                    snap.get("semantic_text", ""),
                    "",
                    "NAVIGATION_ITEMS:",
                    "\n".join(snap.get("navigation_items", [])),
                ]
            )
            for section in snap.get("sections", []):
                lines.extend(["", f"SECTION: {section.get('menu_item', '')}", section.get("semantic_text", "")])
            lines.extend(["", "-" * 80, ""])
        return "\n".join(lines)

    def _detect_quarter_number(self, url: str, semantic_text: str) -> int:
        try:
            parsed = urlparse(url)
            quarter_values = parse_qs(parsed.query).get("quarter", [])
            if quarter_values:
                return int(quarter_values[0])
        except Exception:
            pass

        match = re.search(r"\bQuarter\s+(\d+)\b", semantic_text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
        return 1

    def _build_quarter_url(self, url: str, quarter: int) -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        query["quarter"] = [str(quarter)]
        new_query = urlencode(query, doseq=True)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

    def _normalize_marketplace_key(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())

    def _current_marketplace_resource_keys(self, url: str) -> set[str]:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        keys: set[str] = set()
        for raw in [*query.get("resource", []), *query.get("parentResource", [])]:
            normalized = self._normalize_marketplace_key(raw)
            if normalized:
                keys.add(normalized)
        return keys

    async def _current_marketplace_active_keys(self, page: Any) -> set[str]:
        keys = self._current_marketplace_resource_keys(getattr(page, "url", "") or "")
        try:
            title = await page.title()
        except Exception:
            title = ""
        normalized_title = self._normalize_marketplace_key(title)
        if normalized_title:
            keys.add(normalized_title)
        return keys

    def _looks_like_nav_label(self, label: str) -> bool:
        text = label.strip()
        if len(text) < 3 or len(text) > 55:
            return False
        if text.endswith(":"):
            return False
        if text.startswith("http"):
            return False
        if text.count(" ") > 8:
            return False
        if any(token in text for token in ("$", "%", "http", "www.", "@")):
            return False

        lowered = text.lower()
        blocked = (
            "jump to",
            "select an option",
            "welcome to marketplace",
            "virtual business world",
            "overview of activities",
            "how to succeed",
            "getting started",
            "responsibilities",
            "team norms",
            "marketplace live",
            "company name",
            "microsimulations",
            "trophy room",
            "chat",
            "email",
            "help",
            "quick links",
            "ai coach",
            "view full report",
            "ending cash",
            "game time",
            "gmt",
        )
        if any(token in lowered for token in blocked):
            return False
        if lowered == "account":
            return False
        return True

    def _is_actionable_menu_item(self, item: str) -> bool:
        lowered = item.strip().lower()
        if not lowered:
            return False
        decision_keywords = (
            "test market results",
            "top concerns from previous quarter",
            "enter the market",
            "accounting",
            "performance report",
            "market share",
            "sales",
            "income statement",
            "cash flow",
            "balance sheet",
            "balanced scorecard",
            "detailed brand demand report",
            "strategic graphs",
            "competitors' profiles",
            "marketing",
            "brand management",
            "customer needs",
            "use pattern",
            "brand profitability",
            "product review",
            "brand judgment",
            "competitors' brands",
            "design brand",
            "pricing",
            "price willing to pay",
            "price judgment",
            "competitors' prices",
            "cost of production",
            "price and priority",
            "advertising",
            "ad copy review",
            "ad copy judgment",
            "competitors' ads",
            "design ad",
            "local media",
            "competitors' local advertising",
            "local media placement",
            "regional media",
            "competitors' regional advertising",
            "regional media placement",
            "media preferences",
            "internet marketing",
            "number of searches",
            "organic sem results",
            "manage web pages",
            "check ad claims",
            "buy market research",
            "tactical summary",
            "sales channel",
            "regional profitability",
            "competitors in city",
            "competitors' sales force",
            "hire sales people",
            "market potential",
            "open stores",
            "demand projection",
            "demand impact factors",
            "current quarter",
            "next quarter",
            "manufacturing",
            "results of previous quarter",
            "competitive capacities",
            "brand production",
            "operating capacity",
            "overtime",
            "production simulation",
            "fixed capacity",
            "pro forma",
            "finance",
            "financial ratios",
            "closely held stock",
            "certificate of deposit",
            "payment to business partners",
            "summary of decisions",
            "final check",
            "submit",
            "additional decisions",
        )
        return any(keyword in lowered for keyword in decision_keywords)

    def _extract_quarter_menu_items_from_text(self, semantic_text: str) -> list[str]:
        lines = [line.strip() for line in semantic_text.splitlines() if line.strip()]
        if not lines:
            return []

        items: list[str] = []
        in_menu = False
        for line in lines:
            lowered = line.lower()
            if lowered.startswith("quarter menu"):
                in_menu = True
                continue

            if in_menu and lowered in ("lecture", "workspace", "game gadgets", "quick links"):
                break

            if not in_menu:
                continue

            if lowered.startswith("quarter "):
                continue
            if lowered in ("menu search", "edge"):
                continue
            if self._looks_like_nav_label(line):
                items.append(line)
        return items

    async def _wait_after_navigation(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=self.nav_wait_timeout_ms)
            return
        except Exception:
            pass
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=self.nav_wait_timeout_ms)
        except Exception:
            # Continue with best-effort capture even when no formal load-state transition is detected.
            pass

    async def _dismiss_known_overlays(self, page: Any) -> None:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        for label in ("Close", "OK", "Dismiss"):
            try:
                await page.get_by_role("button", name=label).first.click(timeout=400)
                return
            except Exception:
                continue

    async def _navigation_signature(self, page: Any) -> str:
        try:
            signature = await page.evaluate(
                """
                () => JSON.stringify({
                  href: location.href,
                  title: document.title,
                  heading: Array.from(document.querySelectorAll('h1, h2, h3, [role="tab"][aria-selected="true"]'))
                    .slice(0, 6)
                    .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean),
                })
                """
            )
            return str(signature or "")
        except Exception:
            return ""

    async def _click_navigation_item(self, page: Any, name: str) -> None:
        before_signature = await self._navigation_signature(page)

        async def _click_candidate(candidate: Any) -> bool:
            try:
                if hasattr(candidate, "is_visible") and not await candidate.is_visible():
                    return False
            except Exception:
                pass
            await candidate.click(timeout=self.click_timeout_ms)
            await self._wait_after_navigation(page)
            after_signature = await self._navigation_signature(page)
            return not before_signature or after_signature != before_signature

        async def _click_locator_candidates(locator: Any, count: int) -> bool:
            for index in range(count):
                try:
                    candidate = locator.nth(index)
                    if await _click_candidate(candidate):
                        return True
                except Exception:
                    continue
            return False

        for role in ("link", "button", "tab", "menuitem"):
            try:
                locator = page.get_by_role(role, name=name)
                try:
                    count = await locator.count()
                except Exception:
                    if await _click_candidate(locator.first):
                        return
                    continue
                if await _click_locator_candidates(locator, count):
                    return
            except Exception:
                continue
        try:
            locator = page.get_by_text(name, exact=False)
            try:
                count = await locator.count()
            except Exception:
                if await _click_candidate(locator.first):
                    return
                count = 0
        except Exception:
            count = 0
        if count and await _click_locator_candidates(locator, count):
            return
        raise RuntimeError(f"Could not click a visible navigation target for '{name}'.")

    async def _ensure_marketplace_workspace_tab(self, page: Any) -> None:
        current_url = getattr(page, "url", "") or ""
        if "marketplace-simulation.com" not in current_url:
            return
        # Live Marketplace tabs frequently bounce between `task` and `workspace`.
        # Forcing a tab rewrite causes visible churn and can trap the crawler in
        # repeated self-induced reloads, so the indexer now respects the user's
        # current Marketplace tab instead of rewriting it mid-crawl.
        return

    async def _set_agent_overlay_interactive(self, page: Any, interactive: bool) -> None:
        try:
            await page.evaluate(
                """
                (interactive) => {
                  const root = document.getElementById("__live_navigator_overlay_root__");
                  if (!root) return;
                  root.style.pointerEvents = interactive ? "auto" : "none";
                }
                """,
                interactive,
            )
        except Exception:
            pass
