import asyncio
import logging
from pathlib import Path

import pytest

from marketplace_bot.agents.crawler import Crawler
from marketplace_bot.bridge import LocalBrowserBridge
from marketplace_bot.navigator_models import ActionProposal, ObservationPacket
from marketplace_bot.navigator_models import SiteMemory
from marketplace_bot.site_memory_repository import LocalJsonSiteMemoryRepository
from marketplace_bot.state_store import StateStore


class _StaleCrawler:
    def __init__(self) -> None:
        self._browser = object()
        self.closed = False

    def _all_pages(self):
        return []

    async def close(self) -> None:
        self.closed = True
        self._browser = None


class _Page:
    def __init__(self, url: str = "", title: str = "Page") -> None:
        self.url = url
        self._title = title
        self.goto_calls = []
        self.screenshot_calls = 0

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url

    async def title(self) -> str:
        return self._title

    async def screenshot(self, type: str = "png", full_page: bool = False) -> bytes:
        self.screenshot_calls += 1
        assert type == "png"
        assert full_page is False
        return b"fake-png"

    async def evaluate(self, _script):
        return None


class _Locator:
    def __init__(self) -> None:
        self.click_calls = 0

    async def click(self, timeout: int | None = None) -> None:
        self.click_calls += 1

    async def fill(self, value: str) -> None:
        raise AssertionError("fill should not be called in this test")

    async def select_option(self, label: str) -> None:
        raise AssertionError("select_option should not be called in this test")

    async def inner_text(self) -> str:
        return "Continue"


class _DetachingPage(_Page):
    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        raise RuntimeError('Page.goto: Frame has been detached')


class _Context:
    def __init__(self, page: object) -> None:
        self._page = page
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        return self._page


class _Browser:
    def __init__(self, contexts: list[object]) -> None:
        self.contexts = contexts


class _FreshCrawler:
    def __init__(self, page: object | None = None, pages: list[object] | None = None, browser: object | None = None) -> None:
        self._browser = browser if browser is not None else object()
        self._page = page
        self._pages = pages or ([page] if page is not None else [])

    async def attach(self) -> None:
        return None

    def _all_pages(self):
        return list(self._pages)


class _RuntimePage(_Page):
    def __init__(self, url: str = "", title: str = "Page") -> None:
        super().__init__(url=url, title=title)
        self.exposed_bindings = []
        self.init_scripts = []
        self.events = []
        self.main_frame = object()

    async def expose_binding(self, name, handler) -> None:
        self.exposed_bindings.append((name, handler))

    async def add_init_script(self, script) -> None:
        self.init_scripts.append(script)

    def on(self, event_name, handler) -> None:
        self.events.append((event_name, handler))


def test_bridge_reconnects_when_cached_controller_has_no_pages(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / 'runtime')
    state_store.bootstrap()
    page = object()
    fresh = _FreshCrawler(page)

    def _crawler_factory(**_kwargs):
        return fresh

    monkeypatch.setattr('marketplace_bot.bridge.Crawler', _crawler_factory)

    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url='http://127.0.0.1:9222',
        target_domain='play.marketplace-simulation.com',
    )
    stale = _StaleCrawler()
    bridge._crawler = stale

    crawler = asyncio.run(bridge._ensure_controller())

    assert crawler is fresh
    assert stale.closed is True


def test_crawler_exposes_public_page_and_quarter_helpers() -> None:
    class _Context:
        def __init__(self, pages) -> None:
            self.pages = pages

    page_a = object()
    page_b = object()
    crawler = Crawler(cdp_url="http://127.0.0.1:9222")
    crawler._browser = _Browser([_Context([page_a, page_b])])

    assert crawler.has_usable_browser() is True
    assert crawler.list_pages() == [page_a, page_b]
    assert crawler.detect_quarter_number(
        "https://play.marketplace-simulation.com/mpl/web7/engine.php?quarter=4",
        "Quarter 4 decisions",
    ) == 4


def test_bridge_assigns_stable_uuid_runtime_tokens_per_page(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "runtime")
    state_store.bootstrap()
    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url="http://127.0.0.1:9222",
        target_domain="play.marketplace-simulation.com",
    )
    bridge.register_command_handler(lambda payload: {"ok": True, **payload})
    page_a = _RuntimePage("https://example.com/a")
    page_b = _RuntimePage("https://example.com/b")

    asyncio.run(bridge._ensure_page_runtime(page_a))
    asyncio.run(bridge._ensure_page_runtime(page_a))
    asyncio.run(bridge._ensure_page_runtime(page_b))

    assert page_a.exposed_bindings and len(page_a.exposed_bindings) == 1
    assert len(page_a.init_scripts) == 1
    assert len(page_b.init_scripts) == 1
    assert bridge._page_runtime_tokens[page_a] != bridge._page_runtime_tokens[page_b]


def test_bridge_logs_warning_when_navigation_resync_fails(monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    state_store = StateStore(tmp_path / "runtime")
    state_store.bootstrap()
    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url="http://127.0.0.1:9222",
        target_domain="play.marketplace-simulation.com",
    )
    page = _RuntimePage("https://example.com/a")
    bridge._last_panel = {"stage": "live_advice"}

    async def _ensure_page_runtime(_page):
        return None

    async def _sync_agent_overlay(_panel):
        raise RuntimeError("overlay sync crashed")

    monkeypatch.setattr(bridge, "_ensure_page_runtime", _ensure_page_runtime)
    monkeypatch.setattr(bridge, "sync_agent_overlay", _sync_agent_overlay)
    caplog.set_level(logging.WARNING)

    asyncio.run(bridge._handle_navigation(page, page.main_frame))

    assert "Overlay resync failed after navigation" in caplog.text


def test_bridge_promotes_raw_cdp_target_when_playwright_pages_are_not_eligible(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / 'runtime')
    state_store.bootstrap()
    blank = _Page('')
    localhost = _Page('http://127.0.0.1:8002/')
    fresh = _FreshCrawler(pages=[blank, localhost])

    def _crawler_factory(**_kwargs):
        return fresh

    monkeypatch.setattr('marketplace_bot.bridge.Crawler', _crawler_factory)

    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url='http://127.0.0.1:9222',
        target_domain='play.marketplace-simulation.com',
    )
    monkeypatch.setattr(bridge, "_discover_target_url_from_cdp", lambda: 'https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student')

    page = asyncio.run(bridge._get_target_page())

    assert page is blank
    assert blank.goto_calls == [('https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student', 'domcontentloaded', 15000)]


def test_bridge_creates_fresh_page_when_promoted_candidate_is_detached(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / 'runtime')
    state_store.bootstrap()
    detached = _DetachingPage('')
    localhost = _Page('http://127.0.0.1:8002/')
    fresh_page = _Page('')
    context = _Context(fresh_page)
    browser = _Browser([context])
    fresh = _FreshCrawler(pages=[detached, localhost], browser=browser)

    def _crawler_factory(**_kwargs):
        return fresh

    monkeypatch.setattr('marketplace_bot.bridge.Crawler', _crawler_factory)

    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url='http://127.0.0.1:9222',
        target_domain='play.marketplace-simulation.com',
    )
    monkeypatch.setattr(bridge, '_discover_target_url_from_cdp', lambda: 'https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student')

    page = asyncio.run(bridge._get_target_page())

    assert page is fresh_page
    assert context.new_page_calls == 1
    assert fresh_page.goto_calls == [('https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student', 'domcontentloaded', 15000)]


def test_capture_site_index_full_crawl_uses_selected_page_and_restores_starting_url(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / 'runtime')
    state_store.bootstrap()
    starting_url = 'https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=advertising&quarter=4'
    drifted_url = 'https://play.marketplace-simulation.com/mpl/web7/engine.php?resource=welcome-to-marketplace&quarter=1'
    page = _Page(starting_url)

    class _FullCrawler:
        async def discover_navigation_items(self, active_page):
            assert active_page is page
            return ['Advertising', 'Buy Market Research']

        async def extract_semantic_text(self, active_page):
            assert active_page is page
            return 'Quarter 4 Advertising Buy Market Research'

        async def _wait_after_navigation(self, active_page):
            assert active_page is page
            return None

        def _detect_quarter_number(self, url: str, semantic_text: str) -> int:
            assert url == starting_url
            assert 'Quarter 4' in semantic_text
            return 4

        async def scrape_completed_quarters_world_state(self, active_page):
            assert active_page is page
            active_page.url = drifted_url
            return {
                'captured_at': '2026-03-13T12:40:00Z',
                'title': 'Advertising',
                'url': drifted_url,
                'semantic_text': 'Quarter 4 Advertising',
                'navigation_items': ['Advertising', 'Buy Market Research'],
                'sections': [],
                'site_map': [{'title': 'Advertising', 'url': drifted_url, 'section_count': 0}],
                'completed_quarters': [],
                'quarter_range': {'start': 1, 'end': 4},
                'editable_quarter': 4,
            }

    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url='http://127.0.0.1:9222',
        target_domain='play.marketplace-simulation.com',
    )
    crawler = _FullCrawler()

    async def _ensure_controller():
        return crawler

    async def _get_target_page(prefer_url: str = ''):
        return page

    async def _build_observation(**kwargs):
        return ObservationPacket(
            session_id='sess_demo',
            page_url=page.url,
            page_title='Advertising',
            visible_text_summary='Quarter 4 Advertising',
            dom_summary='Quarter 4 Advertising',
            active_goal='Index the current Marketplace workspace.',
            domain_pack='marketplace_simulation',
            safety_mode='confirm_before_act',
            browser_metadata=kwargs.get('browser_metadata', {}),
            captured_at='2026-03-13T12:40:01Z',
        )

    monkeypatch.setattr(bridge, '_ensure_controller', _ensure_controller)
    monkeypatch.setattr(bridge, '_get_target_page', _get_target_page)
    monkeypatch.setattr(bridge, '_build_observation', _build_observation)

    observation = asyncio.run(
        bridge.capture_site_index(
            session_id='sess_demo',
            active_goal='Index the current Marketplace workspace.',
            domain_pack='marketplace_simulation',
            safety_mode='confirm_before_act',
            index_mode='advanced',
        )
    )

    assert page.goto_calls[-1] == (starting_url, 'domcontentloaded', 15000)
    assert observation.page_url == starting_url


def test_capture_site_index_reuses_inflight_request_for_same_session(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / 'runtime')
    state_store.bootstrap()
    page = _Page('https://example.com/workflow', title='Workflow')

    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url='http://127.0.0.1:9222',
        target_domain='example.com',
    )

    class _LightCrawler:
        async def _wait_after_navigation(self, active_page):
            assert active_page is page
            return None

    crawler = _LightCrawler()
    started = asyncio.Event()
    release = asyncio.Event()
    exploration_calls = 0

    async def _ensure_controller():
        return crawler

    async def _get_target_page(prefer_url: str = ''):
        assert prefer_url in ('', 'https://example.com/workflow')
        return page

    async def _build_site_probe(active_page, active_crawler, domain_pack):
        assert active_page is page
        assert active_crawler is crawler
        assert domain_pack == 'generic_web'
        return {
            'site_origin': 'https://example.com',
            'navigation_items': ['Workflow'],
            'site_map': [{'title': 'Workflow', 'url': page.url, 'section_count': 0}],
        }

    async def _explore_generic_site_index(active_page, active_crawler, active_goal, max_pages):
        nonlocal exploration_calls
        assert active_page is page
        assert active_crawler is crawler
        assert active_goal == 'Index the current workflow.'
        assert max_pages == 3
        exploration_calls += 1
        started.set()
        await release.wait()
        return {
            'captured_at': '2026-03-13T13:00:00Z',
            'title': 'Workflow',
            'url': page.url,
            'semantic_text': 'Current workflow',
            'navigation_items': ['Workflow'],
            'sections': [],
            'site_map': [{'title': 'Workflow', 'url': page.url, 'section_count': 0}],
            'completed_quarters': [],
            'quarter_range': {},
            'editable_quarter': None,
        }

    async def _build_observation(**kwargs):
        return ObservationPacket(
            session_id=kwargs['session_id'],
            page_url=page.url,
            page_title='Workflow',
            visible_text_summary='Current workflow',
            dom_summary='Current workflow',
            active_goal=kwargs['active_goal'],
            domain_pack=kwargs['domain_pack'],
            safety_mode=kwargs['safety_mode'],
            browser_metadata=kwargs.get('browser_metadata', {}),
            captured_at='2026-03-13T13:00:01Z',
        )

    monkeypatch.setattr(bridge, '_ensure_controller', _ensure_controller)
    monkeypatch.setattr(bridge, '_get_target_page', _get_target_page)
    monkeypatch.setattr(bridge, '_build_site_probe', _build_site_probe)
    monkeypatch.setattr(bridge, '_explore_generic_site_index', _explore_generic_site_index)
    monkeypatch.setattr(bridge, '_build_observation', _build_observation)

    async def _run() -> tuple[ObservationPacket, ObservationPacket]:
        first = asyncio.create_task(
            bridge.capture_site_index(
                session_id='sess_demo',
                active_goal='Index the current workflow.',
                domain_pack='generic_web',
                safety_mode='confirm_before_act',
                index_mode='lightweight',
            )
        )
        await started.wait()
        second = asyncio.create_task(
            bridge.capture_site_index(
                session_id='sess_demo',
                active_goal='Index the current workflow.',
                domain_pack='generic_web',
                safety_mode='confirm_before_act',
                index_mode='lightweight',
            )
        )
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    first_observation, second_observation = asyncio.run(_run())

    assert exploration_calls == 1
    assert first_observation.page_url == 'https://example.com/workflow'
    assert second_observation.page_url == 'https://example.com/workflow'


def test_capture_site_index_uses_generic_exploration_for_cold_start_adaptive_sessions(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "runtime")
    state_store.bootstrap()
    page = _Page("https://www.apple.com", title="Apple")

    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url="http://127.0.0.1:9222",
        target_domain="apple.com",
    )

    class _Crawler:
        async def _wait_after_navigation(self, active_page):
            assert active_page is page
            return None

    crawler = _Crawler()
    explore_calls = 0

    async def _ensure_controller():
        return crawler

    async def _get_target_page(prefer_url: str = ""):
        return page

    async def _build_site_probe(active_page, active_crawler, domain_pack):
        assert active_page is page
        assert active_crawler is crawler
        assert domain_pack == "generic_web"
        return {
            "site_origin": "https://www.apple.com",
            "navigation_items": ["Store", "Mac"],
            "site_map": [{"title": "Apple", "url": page.url, "section_count": 2}],
        }

    async def _explore_generic_site_index(active_page, active_crawler, active_goal, max_pages):
        nonlocal explore_calls
        assert active_page is page
        assert active_crawler is crawler
        assert "Mac mini" in active_goal
        assert max_pages >= 4
        explore_calls += 1
        return {
            "captured_at": "2026-03-14T23:00:00Z",
            "title": "Apple",
            "url": page.url,
            "semantic_text": "Mac mini starts at $599",
            "navigation_items": ["Store", "Mac", "Mac mini"],
            "sections": [],
            "site_map": [
                {"title": "Apple", "url": "https://www.apple.com", "section_count": 2},
                {"title": "Mac", "url": "https://www.apple.com/mac/", "section_count": 3},
                {"title": "Mac mini", "url": "https://www.apple.com/mac-mini/", "section_count": 0},
            ],
            "completed_quarters": [],
            "quarter_range": {},
            "editable_quarter": None,
        }

    async def _partial_site_index(*_args, **_kwargs):
        raise AssertionError("partial crawl should not be used for a cold-start adaptive generic session")

    async def _build_observation(**kwargs):
        return ObservationPacket(
            session_id=kwargs["session_id"],
            page_url=page.url,
            page_title="Apple",
            visible_text_summary="Mac mini starts at $599",
            dom_summary="Mac mini starts at $599",
            active_goal=kwargs["active_goal"],
            domain_pack=kwargs["domain_pack"],
            safety_mode=kwargs["safety_mode"],
            browser_metadata=kwargs.get("browser_metadata", {}),
            captured_at="2026-03-14T23:00:01Z",
        )

    monkeypatch.setattr(bridge, "_ensure_controller", _ensure_controller)
    monkeypatch.setattr(bridge, "_get_target_page", _get_target_page)
    monkeypatch.setattr(bridge, "_build_site_probe", _build_site_probe)
    monkeypatch.setattr(bridge, "_explore_generic_site_index", _explore_generic_site_index)
    monkeypatch.setattr(bridge, "_partial_site_index", _partial_site_index)
    monkeypatch.setattr(bridge, "_build_observation", _build_observation)

    observation = asyncio.run(
        bridge.capture_site_index(
            session_id="sess_demo",
            active_goal="Find Mac mini pricing on Apple.",
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
            index_mode="adaptive",
        )
    )

    assert explore_calls == 1
    assert observation.browser_metadata["site_check"]["strategy"] == "full"


def test_score_generic_link_prefers_relevant_parent_branches_over_leaf_actions() -> None:
    goal_terms = ["workstation", "memory"]

    parent_score = LocalBrowserBridge._score_generic_link_details(
        "Workstations",
        "https://example.com/workstations/",
        goal_terms,
    )["score"]
    compare_score = LocalBrowserBridge._score_generic_link_details(
        "Compare Memory Workstations",
        "https://example.com/workstations/compare/memory-models",
        goal_terms,
    )["score"]
    buy_score = LocalBrowserBridge._score_generic_link_details(
        "Buy Memory Workstation",
        "https://example.com/shop/buy-workstations/memory-pro",
        goal_terms,
    )["score"]

    assert parent_score > compare_score
    assert parent_score > buy_score


def test_discover_generic_candidate_links_filters_low_relevance_leaf_noise_when_parent_branch_exists(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "runtime")
    state_store.bootstrap()
    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url="http://127.0.0.1:9222",
        target_domain="apple.com",
    )

    class _Page:
        url = "https://www.apple.com/"

        async def evaluate(self, _script: str):
            return [
                {"label": "Mac", "url": "https://www.apple.com/mac/", "present": True},
                {
                    "label": "Compare MacBook Air and MacBook Pro",
                    "url": "https://www.apple.com/mac/compare/?modelList=MacBook-Air-M4,MacBook-Pro-14-M4",
                    "present": True,
                },
                {"label": "Buy MacBook Air", "url": "https://www.apple.com/shop/buy-mac/macbook-air", "present": True},
                {"label": "Support", "url": "https://support.apple.com/mac", "present": True},
                {"label": "Mac mini PDF", "url": "https://www.apple.com/mac-mini/specs.pdf", "present": True},
            ]

    page = _Page()

    candidates = asyncio.run(
        bridge._discover_generic_candidate_links(page, ["mac", "mini"])
    )

    labels = [str(item.get("label")) for item in candidates]
    assert labels[0] == "Mac"
    assert "Compare MacBook Air and MacBook Pro" not in labels
    assert "Buy MacBook Air" not in labels
    assert "Mac mini PDF" not in labels


def test_capture_site_index_full_generic_refresh_replaces_stale_site_memory(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "runtime")
    state_store.bootstrap()
    site_memory_repository = LocalJsonSiteMemoryRepository(tmp_path / "runtime")
    site_memory_repository.save(
        SiteMemory(
            memory_key="mem_apple",
            site_origin="https://www.apple.com",
            domain_pack="generic_web",
            index_mode="advanced",
            site_fingerprint="stale",
            structure_digest="stale",
            strategic_summary="Old crawl.",
            indexed_context={
                "site_index": {
                    "site_map": [
                        {"title": "Old page", "url": "https://www.apple.com/old-page", "section_count": 0},
                    ],
                    "navigation_items": ["Old page"],
                }
            },
            last_checked_at="2026-03-01T00:00:00Z",
            last_indexed_at="2026-03-01T00:00:00Z",
            change_status="changed",
            change_summary="Old crawl.",
        )
    )
    page = _Page("https://www.apple.com", title="Apple")

    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url="http://127.0.0.1:9222",
        target_domain="apple.com",
        site_memory_repository=site_memory_repository,
    )

    class _Crawler:
        async def _wait_after_navigation(self, active_page):
            assert active_page is page
            return None

    crawler = _Crawler()

    async def _ensure_controller():
        return crawler

    async def _get_target_page(prefer_url: str = ""):
        return page

    async def _build_site_probe(active_page, active_crawler, domain_pack):
        assert active_page is page
        assert active_crawler is crawler
        assert domain_pack == "generic_web"
        return {
            "site_origin": "https://www.apple.com",
            "navigation_items": ["Mac", "Mac mini"],
            "site_map": [{"title": "Apple", "url": page.url, "section_count": 2}],
        }

    async def _explore_generic_site_index(active_page, active_crawler, active_goal, max_pages):
        assert active_page is page
        assert active_crawler is crawler
        assert max_pages >= 5
        return {
            "captured_at": "2026-03-14T23:00:00Z",
            "title": "Apple",
            "url": page.url,
            "semantic_text": "Mac mini starts at $599",
            "navigation_items": ["Mac", "Mac mini"],
            "sections": [],
            "site_map": [
                {"title": "Apple", "url": "https://www.apple.com", "section_count": 2},
                {"title": "Mac mini", "url": "https://www.apple.com/mac-mini/", "section_count": 0},
            ],
            "completed_quarters": [],
            "quarter_range": {},
            "editable_quarter": None,
        }

    async def _build_observation(**kwargs):
        return ObservationPacket(
            session_id=kwargs["session_id"],
            page_url=page.url,
            page_title="Apple",
            visible_text_summary="Mac mini starts at $599",
            dom_summary="Mac mini starts at $599",
            active_goal=kwargs["active_goal"],
            domain_pack=kwargs["domain_pack"],
            safety_mode=kwargs["safety_mode"],
            browser_metadata=kwargs.get("browser_metadata", {}),
            captured_at="2026-03-14T23:00:01Z",
        )

    monkeypatch.setattr(bridge, "_ensure_controller", _ensure_controller)
    monkeypatch.setattr(bridge, "_get_target_page", _get_target_page)
    monkeypatch.setattr(bridge, "_build_site_probe", _build_site_probe)
    monkeypatch.setattr(bridge, "_explore_generic_site_index", _explore_generic_site_index)
    monkeypatch.setattr(bridge, "_build_observation", _build_observation)

    observation = asyncio.run(
        bridge.capture_site_index(
            session_id="sess_demo",
            active_goal="Find Mac mini pricing on Apple.",
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
            index_mode="advanced",
        )
    )

    urls = [item["url"] for item in observation.browser_metadata["site_index"]["site_map"]]
    assert "https://www.apple.com/mac-mini/" in urls
    assert "https://www.apple.com/old-page" not in urls


def test_build_observation_includes_ax_metadata_when_provider_succeeds(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "runtime")
    state_store.bootstrap()
    page = _Page("https://example.com/workflow", title="Workflow")

    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url="http://127.0.0.1:9222",
        target_domain="example.com",
    )

    class _Crawler:
        async def extract_semantic_text(self, active_page):
            assert active_page is page
            return "Workflow Continue"

    class _AxProvider:
        async def capture(self, active_page, mode: str, target_scope=None, include_occlusion: bool = False):
            assert active_page is page
            assert mode == "live"
            assert include_occlusion is False
            return type(
                "AxSnapshot",
                (),
                {
                    "source": "cdp",
                    "mode": "live",
                    "summary": "AX: 1 interactive node, 0 blocked, 0 likely occluded",
                    "targets": [{"ax_node_id": "ax_1", "role": "button", "name": "Continue", "actionable": True}],
                    "diagnostics": {"interactive_nodes": 1, "blocked_nodes": 0, "likely_occluded_nodes": 0},
                    "raw": {"nodes": [{"role": "button", "name": "Continue"}]},
                },
            )()

    async def _extract_browser_metadata(active_page):
        assert active_page is page
        return {"page_signature": "sig"}

    monkeypatch.setattr(bridge, "_extract_browser_metadata", _extract_browser_metadata)
    bridge.ax_snapshot_provider = _AxProvider()

    observation = asyncio.run(
        bridge._build_observation(
            page=page,
            crawler=_Crawler(),
            session_id="sess_ax",
            active_goal="Continue the flow.",
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
        )
    )

    assert observation.browser_metadata["ax_summary"].startswith("AX:")
    assert observation.browser_metadata["ax_targets"][0]["name"] == "Continue"
    assert observation.browser_metadata["ax_capture_mode"] == "live"
    assert observation.browser_metadata["ax_diagnostics"]["interactive_nodes"] == 1


def test_build_observation_captures_supplementary_table_slices(monkeypatch, tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "runtime")
    state_store.bootstrap()

    class _TablePage(_Page):
        def __init__(self) -> None:
            super().__init__("https://example.com/table", title="Table View")
            self.screenshot_args = []

        async def screenshot(self, type: str = "png", full_page: bool = False, clip=None) -> bytes:
            self.screenshot_args.append({"type": type, "full_page": full_page, "clip": clip})
            if clip is None:
                return b"page-png"
            return f"slice-{int(clip['y'])}".encode("utf-8")

        async def evaluate(self, _script):
            return {
                "kind": "table",
                "label": "Regional pricing table",
                "selector": "table",
                "row_count": 18,
                "headers": ["Region", "Price", "Volume"],
                "sample_rows": [
                    ["NORAM", "1200", "88"],
                    ["EUROPE", "1150", "77"],
                ],
                "captures": [
                    {"label": "table_slice_1", "clip": {"x": 20, "y": 120, "width": 800, "height": 420}},
                    {"label": "table_slice_2", "clip": {"x": 20, "y": 500, "width": 800, "height": 420}},
                ],
            }

    page = _TablePage()
    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url="http://127.0.0.1:9222",
        target_domain="example.com",
    )

    class _Crawler:
        async def extract_semantic_text(self, active_page):
            assert active_page is page
            return "Regional pricing table"

    async def _extract_browser_metadata(active_page):
        assert active_page is page
        return {"page_signature": "sig-table"}

    async def _capture_ax_metadata(active_page, *, session_id: str, mode: str, target_scope=None):
        assert active_page is page
        return {}

    monkeypatch.setattr(bridge, "_extract_browser_metadata", _extract_browser_metadata)
    monkeypatch.setattr(bridge, "_capture_ax_metadata", _capture_ax_metadata)

    observation = asyncio.run(
        bridge._build_observation(
            page=page,
            crawler=_Crawler(),
            session_id="sess_table",
            active_goal="Review the table.",
            domain_pack="generic_web",
            safety_mode="confirm_before_act",
        )
    )

    supplementary = observation.supplementary_screenshots
    assert len(supplementary) == 2
    assert supplementary[0].label == "table_slice_1"
    assert observation.browser_metadata["table_region"]["row_count"] == 18
    assert observation.browser_metadata["table_region"]["headers"] == ["Region", "Price", "Volume"]
    assert len(page.screenshot_args) == 3
    assert page.screenshot_args[0]["clip"] is None
    assert page.screenshot_args[1]["clip"]["y"] == 120


def test_execute_single_blocks_click_when_ax_target_is_not_actionable(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / "runtime")
    state_store.bootstrap()
    page = _Page("https://example.com/workflow", title="Workflow")
    locator = _Locator()

    class _ActionPage(_Page):
        def get_by_text(self, text: str, exact: bool = False):
            assert text == "Continue"
            return type("LocatorChain", (), {"first": locator})()

    action_page = _ActionPage("https://example.com/workflow", title="Workflow")

    bridge = LocalBrowserBridge(
        state_store=state_store,
        cdp_url="http://127.0.0.1:9222",
        target_domain="example.com",
    )

    class _AxProvider:
        async def capture(self, active_page, mode: str, target_scope=None, include_occlusion: bool = False):
            assert active_page is action_page
            assert mode == "verify"
            assert include_occlusion is True
            assert target_scope["target_text"] == "Continue"
            return type(
                "AxSnapshot",
                (),
                {
                    "source": "cdp",
                    "mode": "verify",
                    "summary": "AX: 1 interactive node, 1 blocked, 1 likely occluded",
                    "targets": [
                        {
                            "ax_node_id": "ax_continue",
                            "role": "button",
                            "name": "Continue",
                            "actionable": False,
                            "block_reason": "occluded",
                            "bounds": {"x": 20, "y": 30, "width": 120, "height": 40},
                        }
                    ],
                    "diagnostics": {"interactive_nodes": 1, "blocked_nodes": 1, "likely_occluded_nodes": 1},
                    "raw": {},
                },
            )()

    bridge.ax_snapshot_provider = _AxProvider()
    action = ActionProposal(action_id="act_continue", action="click", reasoning="Continue the flow", target_text="Continue")

    result = asyncio.run(bridge._execute_single(action_page, action))

    assert result["status"] == "skipped"
    assert result["detail"] == "ax_blocked:occluded"
    assert locator.click_calls == 0
    assert action.metadata["ax_block_reason"] == "occluded"
