from marketplace_bot.agents.crawler import Crawler
import asyncio


def test_extract_quarter_menu_items_from_text() -> None:
    crawler = Crawler(cdp_url="http://localhost:9222")
    semantic_text = """
    Quarter Menu - Quarter 3
    Quarter 3
    Enter the Market
    Accounting for Last Quarter
    Marketing
    Sales Channel
    Manufacturing
    Pro Forma Accounting
    Finance
    Summary of Decisions
    Final Check
    Submit
    Additional Decisions
    LECTURE
    EDGE
    """

    items = crawler._extract_quarter_menu_items_from_text(semantic_text)

    assert "Enter the Market" in items
    assert "Accounting for Last Quarter" in items
    assert "Submit" in items
    assert "Quarter 3" not in items
    assert "LECTURE" not in items


def test_bad_semantic_labels_are_filtered() -> None:
    crawler = Crawler(cdp_url="http://localhost:9222")

    assert crawler._looks_like_nav_label("Jump to:") is False
    assert crawler._looks_like_nav_label("Select an option") is False


def test_marketplace_subtabs_are_treated_as_actionable_menu_items() -> None:
    crawler = Crawler(cdp_url="http://localhost:9222")

    assert crawler._is_actionable_menu_item("Pricing") is True
    assert crawler._is_actionable_menu_item("Advertising") is True
    assert crawler._is_actionable_menu_item("Brand Management") is True
    assert crawler._is_actionable_menu_item("Buy Market Research") is True
    assert crawler._is_actionable_menu_item("Customer Needs") is True
    assert crawler._is_actionable_menu_item("Use Pattern") is True
    assert crawler._is_actionable_menu_item("Price Willing to Pay") is True
    assert crawler._is_actionable_menu_item("Design Ad") is True
    assert crawler._is_actionable_menu_item("Local Media Placement") is True
    assert crawler._is_actionable_menu_item("Regional Media") is True
    assert crawler._is_actionable_menu_item("Media Preferences") is True
    assert crawler._is_actionable_menu_item("Regional Media Placement") is True
    assert crawler._is_actionable_menu_item("Current Quarter") is True
    assert crawler._is_actionable_menu_item("Next Quarter") is True
    assert crawler._is_actionable_menu_item("Brand Production") is True
    assert crawler._is_actionable_menu_item("Fixed Capacity") is True
    assert crawler._is_actionable_menu_item("Test Market Results") is True
    assert crawler._is_actionable_menu_item("Top Concerns from Previous Quarter") is True
    assert crawler._is_actionable_menu_item("Competitors' Local Advertising") is True
    assert crawler._is_actionable_menu_item("Local Media Placement") is True
    assert crawler._is_actionable_menu_item("Competitors' Regional Advertising") is True
    assert crawler._is_actionable_menu_item("Demand Impact Factors") is True


def test_scrape_snapshot_merges_nested_section_navigation(monkeypatch) -> None:
    class FakePage:
        url = "https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=marketing&quarter=4"

        async def title(self):
            return "Marketing"

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    async def fake_extract_semantic_text(_page):
        return "Quarter 4 Marketing Pricing Advertising Buy Market Research"

    async def fake_discover_navigation_items(_page):
        return ["Marketing", "Sales Channel"]

    async def fake_crawl_navigation_sections(_page, limit=14):
        assert limit == 14
        return [
            {
                "menu_item": "Marketing",
                "semantic_text": "Pricing Advertising Buy Market Research",
                "navigation_items": ["Pricing", "Advertising", "Buy Market Research"],
            }
        ]

    monkeypatch.setattr(crawler, "extract_semantic_text", fake_extract_semantic_text)
    monkeypatch.setattr(crawler, "discover_navigation_items", fake_discover_navigation_items)
    monkeypatch.setattr(crawler, "crawl_navigation_sections", fake_crawl_navigation_sections)

    snapshot = asyncio.run(crawler._scrape_page_snapshot(page, quarter_number=4, editable=True))

    assert "Marketing" in snapshot["navigation_items"]
    assert "Pricing" in snapshot["navigation_items"]
    assert "Advertising" in snapshot["navigation_items"]
    assert "Buy Market Research" in snapshot["navigation_items"]


def test_set_agent_overlay_interactive_updates_overlay_pointer_events() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.evaluate_calls = []

        async def evaluate(self, script, interactive):
            self.evaluate_calls.append((script, interactive))
            return None

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    asyncio.run(crawler._set_agent_overlay_interactive(page, interactive=False))
    asyncio.run(crawler._set_agent_overlay_interactive(page, interactive=True))

    assert len(page.evaluate_calls) == 2
    assert page.evaluate_calls[0][1] is False
    assert page.evaluate_calls[1][1] is True


def test_extract_semantic_text_retries_after_navigation_context_reset() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.evaluate_calls = 0
            self.wait_calls = []

        async def evaluate(self, script):
            self.evaluate_calls += 1
            if self.evaluate_calls == 1:
                raise RuntimeError("Page.evaluate: Execution context was destroyed, most likely because of a navigation")
            return "Quarter 4 Buy Market Research"

        async def wait_for_load_state(self, state, timeout=None):
            self.wait_calls.append((state, timeout))
            return None

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    text = asyncio.run(crawler.extract_semantic_text(page))

    assert text == "Quarter 4 Buy Market Research"
    assert page.evaluate_calls == 2
    assert page.wait_calls


def test_marketplace_full_crawl_keeps_live_index_on_the_current_editable_quarter(monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=buymarketresearch&quarter=4"
            self.goto_calls = []

        async def goto(self, url, wait_until=None, timeout=None):
            self.goto_calls.append((url, wait_until, timeout))
            self.url = url

        async def title(self):
            return "Buy Market Research"

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()
    full_calls = []
    summary_calls = []

    async def fake_extract_semantic_text(active_page):
        return f"Quarter {crawler._detect_quarter_number(active_page.url, '')}"

    async def fake_wait_after_navigation(_page):
        return None

    async def fake_scrape_page_snapshot(active_page, quarter_number, editable=False, section_limit=14):
        full_calls.append((quarter_number, editable, section_limit, active_page.url))
        return {
            "captured_at": "2026-03-13T12:00:00Z",
            "quarter_number": quarter_number,
            "editable": editable,
            "title": f"Quarter {quarter_number} Editable",
            "url": active_page.url,
            "semantic_text": f"Quarter {quarter_number} full detail",
            "navigation_items": ["Buy Market Research", "Pricing", "Advertising", "Open Stores"],
            "sections": [{"menu_item": "Buy Market Research", "semantic_text": "full"}],
            "section_previews": [{"menu_item": "Buy Market Research", "semantic_text_excerpt": "full"}],
        }

    async def fake_scrape_quarter_summary_snapshot(active_page, quarter_number):
        summary_calls.append((quarter_number, active_page.url))
        return {
            "captured_at": "2026-03-13T12:00:00Z",
            "quarter_number": quarter_number,
            "editable": False,
            "title": f"Quarter {quarter_number} Summary of Decisions",
            "url": active_page.url,
            "semantic_text": f"Quarter {quarter_number} summary only",
            "navigation_items": ["Summary of Decisions"],
            "sections": [],
            "section_previews": [],
        }

    monkeypatch.setattr(crawler, "extract_semantic_text", fake_extract_semantic_text)
    monkeypatch.setattr(crawler, "_wait_after_navigation", fake_wait_after_navigation)
    monkeypatch.setattr(crawler, "_scrape_page_snapshot", fake_scrape_page_snapshot)
    monkeypatch.setattr(crawler, "_scrape_quarter_summary_snapshot", fake_scrape_quarter_summary_snapshot)

    payload = asyncio.run(crawler.scrape_completed_quarters_world_state(page))

    assert full_calls == [(4, True, 24, "https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=buymarketresearch&quarter=4")]
    assert summary_calls == []
    assert payload["editable_quarter"] == 4
    assert payload["quarter_range"] == {"start": 4, "end": 4}
    assert payload["navigation_items"] == ["Buy Market Research", "Pricing", "Advertising", "Open Stores"]
    assert payload["completed_quarters"][-1]["title"] == "Quarter 4 Editable"
    assert len(payload["sections"]) == 1


def test_discover_navigation_items_includes_raw_dom_task_labels_when_roles_are_incomplete() -> None:
    class FailingLocator:
        async def count(self):
            raise RuntimeError("role lookup unavailable")

    class FakeNav:
        def get_by_role(self, role):
            return FailingLocator()

    class FakePage:
        def get_by_role(self, role):
            if role == "navigation":
                return FakeNav()
            return FailingLocator()

        async def evaluate(self, script):
            return [
                "Competitors' Local Advertising",
                "Local Media Placement",
                "Demand Impact Factors",
                "Current Quarter",
                "Next Quarter",
            ]

    crawler = Crawler(cdp_url="http://localhost:9222")

    items = asyncio.run(crawler.discover_navigation_items(FakePage()))

    assert "Competitors' Local Advertising" in items
    assert "Local Media Placement" in items
    assert "Demand Impact Factors" in items
    assert "Current Quarter" in items
    assert "Next Quarter" in items


def test_discover_navigation_items_skips_slow_navigation_entries_instead_of_failing_the_whole_scan() -> None:
    class FakeLink:
        def __init__(self, label: str, should_fail: bool = False) -> None:
            self.label = label
            self.should_fail = should_fail

        async def inner_text(self) -> str:
            if self.should_fail:
                raise RuntimeError("Locator.inner_text: Timeout 30000ms exceeded.")
            return self.label

    class FakeLinks:
        def __init__(self, labels) -> None:
            self.labels = labels

        async def count(self):
            return len(self.labels)

        def nth(self, index):
            label = self.labels[index]
            return FakeLink(label, should_fail=(index == 2))

    class FakeNav:
        def get_by_role(self, role):
            assert role == "link"
            return FakeLinks(["Mac", "iPhone", "Slow Item", "Mac mini"])

    class FakePage:
        def get_by_role(self, role):
            if role == "navigation":
                return FakeNav()
            raise RuntimeError("unexpected role lookup")

        async def evaluate(self, script):
            return []

    crawler = Crawler(cdp_url="http://localhost:9222")

    items = asyncio.run(crawler.discover_navigation_items(FakePage()))

    assert "Mac" in items
    assert "iPhone" in items
    assert "Mac mini" in items


def test_scrape_completed_quarters_world_state_keeps_the_current_marketplace_tab_for_live_stability(monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=marketing&tab=task&quarter=4&language=en-us"
            self.goto_calls = []

        async def goto(self, url, wait_until=None, timeout=None):
            self.goto_calls.append((url, wait_until, timeout))
            self.url = url

        async def title(self):
            return "Marketing"

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    async def fake_extract_semantic_text(_page):
        return "Quarter 4 Marketing"

    async def fake_wait_after_navigation(_page):
        return None

    async def fake_scrape_page_snapshot(active_page, quarter_number, editable=False, section_limit=14):
        return {
            "captured_at": "2026-03-14T10:00:00Z",
            "quarter_number": quarter_number,
            "editable": editable,
            "title": "Quarter 4 Editable",
            "url": active_page.url,
            "semantic_text": "Quarter 4 full detail",
            "navigation_items": ["Marketing"],
            "sections": [],
            "section_previews": [],
        }

    async def fake_scrape_quarter_summary_snapshot(active_page, quarter_number):
        return {
            "captured_at": "2026-03-14T10:00:00Z",
            "quarter_number": quarter_number,
            "editable": False,
            "title": f"Quarter {quarter_number} Summary",
            "url": active_page.url,
            "semantic_text": f"Quarter {quarter_number} summary",
            "navigation_items": ["Summary of Decisions"],
            "sections": [],
            "section_previews": [],
        }

    monkeypatch.setattr(crawler, "extract_semantic_text", fake_extract_semantic_text)
    monkeypatch.setattr(crawler, "_wait_after_navigation", fake_wait_after_navigation)
    monkeypatch.setattr(crawler, "_scrape_page_snapshot", fake_scrape_page_snapshot)
    monkeypatch.setattr(crawler, "_scrape_quarter_summary_snapshot", fake_scrape_quarter_summary_snapshot)

    asyncio.run(crawler.scrape_completed_quarters_world_state(page))

    assert page.goto_calls == []


def test_crawl_navigation_sections_recurses_into_newly_visible_nested_subsections(monkeypatch) -> None:
    class FakeLocator:
        def __init__(self, page, name):
            self.page = page
            self.name = name

        @property
        def first(self):
            return self

        async def click(self, timeout=None):
            self.page.clicked.append((self.name, timeout))
            self.page.current_node = self.name

    class FakePage:
        def __init__(self) -> None:
            self.current_node = "root"
            self.clicked = []

        def get_by_role(self, role, name=None):
            return FakeLocator(self, name)

        def get_by_text(self, name, exact=False):
            return FakeLocator(self, name)

    visible_by_node = {
        "root": ["Test Market Results", "Marketing", "Sales Channel"],
        "Test Market Results": ["Test Market Results", "Marketing", "Sales Channel"],
        "Sales Channel": ["Test Market Results", "Marketing", "Sales Channel"],
        "Marketing": ["Test Market Results", "Marketing", "Sales Channel", "Brand Management", "Pricing", "Advertising"],
        "Brand Management": ["Test Market Results", "Marketing", "Sales Channel", "Brand Management", "Pricing", "Advertising"],
        "Pricing": ["Test Market Results", "Marketing", "Sales Channel", "Brand Management", "Pricing", "Advertising", "Price and Priority", "Competitors' Prices"],
        "Price and Priority": ["Test Market Results", "Marketing", "Sales Channel", "Brand Management", "Pricing", "Advertising", "Price and Priority"],
        "Competitors' Prices": ["Test Market Results", "Marketing", "Sales Channel", "Brand Management", "Pricing", "Advertising", "Competitors' Prices"],
        "Advertising": ["Test Market Results", "Marketing", "Sales Channel", "Brand Management", "Pricing", "Advertising", "Design Ad"],
        "Design Ad": ["Test Market Results", "Marketing", "Sales Channel", "Brand Management", "Pricing", "Advertising", "Design Ad"],
    }

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    async def fake_discover_navigation_items(_page):
        return list(visible_by_node[page.current_node])

    async def fake_extract_semantic_text(_page):
        return f"Visible now: {' | '.join(visible_by_node[page.current_node])}"

    async def fake_wait_after_navigation(_page):
        return None

    async def fake_dismiss_known_overlays(_page):
        return None

    async def fake_set_agent_overlay_interactive(_page, interactive: bool):
        return None

    monkeypatch.setattr(crawler, "discover_navigation_items", fake_discover_navigation_items)
    monkeypatch.setattr(crawler, "extract_semantic_text", fake_extract_semantic_text)
    monkeypatch.setattr(crawler, "_wait_after_navigation", fake_wait_after_navigation)
    monkeypatch.setattr(crawler, "_dismiss_known_overlays", fake_dismiss_known_overlays)
    monkeypatch.setattr(crawler, "_set_agent_overlay_interactive", fake_set_agent_overlay_interactive)

    sections = asyncio.run(crawler.crawl_navigation_sections(page, limit=10))

    names = [section["menu_item"] for section in sections]
    assert "Marketing" in names
    assert "Brand Management" in names
    assert "Pricing" in names
    assert "Price and Priority" in names
    assert "Advertising" in names
    assert "Design Ad" in names
    clicked_names = [name for name, _ in page.clicked]
    assert clicked_names[:4] == ["Test Market Results", "Marketing", "Brand Management", "Pricing"]


def test_crawl_navigation_sections_records_parent_urls_for_nested_sections(monkeypatch) -> None:
    class FakeLocator:
        def __init__(self, page, name):
            self.page = page
            self.name = name

        @property
        def first(self):
            return self

        async def click(self, timeout=None):
            self.page.current_node = self.name

    class FakePage:
        urls = {
            "root": "https://example.com/workspace",
            "Marketing": "https://example.com/workspace/marketing",
            "Pricing": "https://example.com/workspace/marketing/pricing",
            "Price and Priority": "https://example.com/workspace/marketing/pricing/price-priority",
        }

        def __init__(self) -> None:
            self.current_node = "root"

        @property
        def url(self):
            return self.urls[self.current_node]

        def get_by_role(self, role, name=None):
            return FakeLocator(self, name)

        def get_by_text(self, name, exact=False):
            return FakeLocator(self, name)

    visible_by_node = {
        "root": ["Marketing"],
        "Marketing": ["Marketing", "Pricing"],
        "Pricing": ["Pricing", "Price and Priority"],
        "Price and Priority": ["Price and Priority"],
    }

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    async def fake_discover_navigation_items(_page):
        return list(visible_by_node[page.current_node])

    async def fake_extract_semantic_text(_page):
        return f"Visible now: {' | '.join(visible_by_node[page.current_node])}"

    async def fake_wait_after_navigation(_page):
        return None

    async def fake_dismiss_known_overlays(_page):
        return None

    async def fake_set_agent_overlay_interactive(_page, interactive: bool):
        return None

    monkeypatch.setattr(crawler, "discover_navigation_items", fake_discover_navigation_items)
    monkeypatch.setattr(crawler, "extract_semantic_text", fake_extract_semantic_text)
    monkeypatch.setattr(crawler, "_wait_after_navigation", fake_wait_after_navigation)
    monkeypatch.setattr(crawler, "_dismiss_known_overlays", fake_dismiss_known_overlays)
    monkeypatch.setattr(crawler, "_set_agent_overlay_interactive", fake_set_agent_overlay_interactive)

    sections = asyncio.run(crawler.crawl_navigation_sections(page, limit=10))
    by_name = {section["menu_item"]: section for section in sections}

    assert by_name["Marketing"]["parent_url"] == "https://example.com/workspace"
    assert by_name["Pricing"]["parent_url"] == "https://example.com/workspace/marketing"
    assert by_name["Price and Priority"]["parent_url"] == "https://example.com/workspace/marketing/pricing"


def test_click_navigation_item_skips_duplicate_active_match_before_clicking_real_target() -> None:
    class FakeCandidate:
        def __init__(self, page, name: str, becomes_current: bool) -> None:
            self.page = page
            self.name = name
            self.becomes_current = becomes_current

        async def click(self, timeout=None):
            self.page.click_attempts.append((self.name, self.becomes_current, timeout))
            if self.becomes_current:
                self.page.current_node = self.name

    class FakeLocatorList:
        def __init__(self, candidates):
            self._candidates = list(candidates)

        async def count(self):
            return len(self._candidates)

        def nth(self, index: int):
            return self._candidates[index]

        @property
        def first(self):
            return self._candidates[0]

    class FakePage:
        def __init__(self) -> None:
            self.current_node = "Finance"
            self.click_attempts = []

        def get_by_role(self, role, name=None):
            if role == "tab" and name == "Cash Flow":
                return FakeLocatorList(
                    [
                        FakeCandidate(self, "Cash Flow", becomes_current=False),
                        FakeCandidate(self, "Cash Flow", becomes_current=True),
                    ]
                )
            return FakeLocatorList([])

        def get_by_text(self, name, exact=False):
            return FakeLocatorList([])

        async def evaluate(self, script):
            if "location.href" not in script:
                raise AssertionError("unexpected script")
            return self.current_node

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    asyncio.run(crawler._click_navigation_item(page, "Cash Flow"))

    assert page.current_node == "Cash Flow"
    assert page.click_attempts == [
        ("Cash Flow", False, crawler.click_timeout_ms),
        ("Cash Flow", True, crawler.click_timeout_ms),
    ]


def test_crawl_navigation_sections_skips_clicking_the_current_active_marketplace_resource(monkeypatch) -> None:
    class FakeLocator:
        def __init__(self, page, name):
            self.page = page
            self.name = name

        @property
        def first(self):
            return self

        async def click(self, timeout=None):
            self.page.clicked.append((self.name, timeout))
            resource = self.name.lower().replace(" ", "").replace("-", "")
            self.page.url = (
                "https://play.marketplace-simulation.com/mpl/web7/engine.php?"
                f"tpl=student&resource={resource}&tab=workspace&quarter=4&language=en-us"
            )

    class FakePage:
        def __init__(self) -> None:
            self.url = (
                "https://play.marketplace-simulation.com/mpl/web7/engine.php?"
                "tpl=student&resource=performance-report&tab=task&quarter=4&language=en-us"
            )
            self.clicked = []

        def get_by_role(self, role, name=None):
            return FakeLocator(self, name)

        def get_by_text(self, name, exact=False):
            return FakeLocator(self, name)

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    async def fake_discover_navigation_items(_page):
        return ["Performance Report", "Marketing", "Sales Channel"]

    async def fake_extract_semantic_text(_page):
        return "Quarter 4 Performance Report Marketing Sales Channel"

    async def fake_wait_after_navigation(_page):
        return None

    async def fake_dismiss_known_overlays(_page):
        return None

    async def fake_set_agent_overlay_interactive(_page, interactive: bool):
        return None

    monkeypatch.setattr(crawler, "discover_navigation_items", fake_discover_navigation_items)
    monkeypatch.setattr(crawler, "extract_semantic_text", fake_extract_semantic_text)
    monkeypatch.setattr(crawler, "_wait_after_navigation", fake_wait_after_navigation)
    monkeypatch.setattr(crawler, "_dismiss_known_overlays", fake_dismiss_known_overlays)
    monkeypatch.setattr(crawler, "_set_agent_overlay_interactive", fake_set_agent_overlay_interactive)

    sections = asyncio.run(crawler.crawl_navigation_sections(page, limit=1))

    assert sections
    assert page.clicked[0][0] == "Marketing"


def test_crawl_navigation_sections_skips_clicking_the_current_active_marketplace_title_alias(monkeypatch) -> None:
    class FakeLocator:
        def __init__(self, page, name):
            self.page = page
            self.name = name

        @property
        def first(self):
            return self

        async def click(self, timeout=None):
            self.page.clicked.append((self.name, timeout))

    class FakePage:
        def __init__(self) -> None:
            self.url = (
                "https://play.marketplace-simulation.com/mpl/web7/engine.php?"
                "tpl=student&resource=welcome-to-marketplace&tab=task&quarter=4&language=en-us"
            )
            self.clicked = []

        async def title(self):
            return "Test Market Results"

        def get_by_role(self, role, name=None):
            return FakeLocator(self, name)

        def get_by_text(self, name, exact=False):
            return FakeLocator(self, name)

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    async def fake_discover_navigation_items(_page):
        return ["Test Market Results", "Top Concerns from Previous Quarter", "Performance Report"]

    async def fake_extract_semantic_text(_page):
        return "Quarter 4 Test Market Results Top Concerns from Previous Quarter Performance Report"

    async def fake_wait_after_navigation(_page):
        return None

    async def fake_dismiss_known_overlays(_page):
        return None

    async def fake_set_agent_overlay_interactive(_page, interactive: bool):
        return None

    monkeypatch.setattr(crawler, "discover_navigation_items", fake_discover_navigation_items)
    monkeypatch.setattr(crawler, "extract_semantic_text", fake_extract_semantic_text)
    monkeypatch.setattr(crawler, "_wait_after_navigation", fake_wait_after_navigation)
    monkeypatch.setattr(crawler, "_dismiss_known_overlays", fake_dismiss_known_overlays)
    monkeypatch.setattr(crawler, "_set_agent_overlay_interactive", fake_set_agent_overlay_interactive)

    sections = asyncio.run(crawler.crawl_navigation_sections(page, limit=1))

    assert sections
    assert page.clicked[0][0] == "Top Concerns from Previous Quarter"


def test_click_navigation_item_skips_hidden_text_matches_before_using_visible_fallback(monkeypatch) -> None:
    class FakeCandidate:
        def __init__(self, page, name: str, visible: bool) -> None:
            self.page = page
            self.name = name
            self.visible = visible

        async def is_visible(self) -> bool:
            return self.visible

        async def click(self, timeout=None):
            self.page.clicked.append((self.name, self.visible, timeout))
            if not self.visible:
                raise RuntimeError("hidden target")
            self.page.signature = f"after:{self.name}"

    class FakeLocator:
        def __init__(self, candidates):
            self.candidates = list(candidates)

        async def count(self):
            return len(self.candidates)

        def nth(self, index):
            return self.candidates[index]

        @property
        def first(self):
            return self.candidates[0]

    class EmptyLocator:
        async def count(self):
            return 0

        def nth(self, index):
            raise IndexError(index)

        @property
        def first(self):
            raise RuntimeError("no matches")

    class FakePage:
        def __init__(self) -> None:
            self.signature = "before"
            self.clicked = []
            self.text_locator = FakeLocator(
                [
                    FakeCandidate(self, "Market Share", visible=False),
                    FakeCandidate(self, "Market Share", visible=True),
                ]
            )

        def get_by_role(self, role, name=None):
            return EmptyLocator()

        def get_by_text(self, name, exact=False):
            return self.text_locator

        async def evaluate(self, script):
            return self.signature

    crawler = Crawler(cdp_url="http://localhost:9222")
    page = FakePage()

    async def fake_wait_after_navigation(_page):
        return None

    monkeypatch.setattr(crawler, "_wait_after_navigation", fake_wait_after_navigation)

    asyncio.run(crawler._click_navigation_item(page, "Market Share"))

    assert page.clicked == [
        ("Market Share", True, crawler.click_timeout_ms),
    ]
