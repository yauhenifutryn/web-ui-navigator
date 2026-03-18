import asyncio

from marketplace_bot.agents.crawler import Crawler
from marketplace_bot.state_store import StateStore


class FakePage:
    def __init__(self, url: str, title: str) -> None:
        self.url = url
        self._title = title

    async def title(self) -> str:
        return self._title


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts



def test_get_active_page_prefers_marketplace_domain(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()

        crawler = Crawler(cdp_url="http://localhost:9222", state_store=store)
        crawler.target_domain = "play.marketplace-simulation.com"
        crawler._browser = FakeBrowser(
            [
                FakeContext([FakePage("about:blank", "Blank")]),
                FakeContext([FakePage("https://play.marketplace-simulation.com/workspace", "Workspace")]),
            ]
        )

        page = await crawler.get_active_page()
        assert page.url == "https://play.marketplace-simulation.com/workspace"

    asyncio.run(_run())


class SnapshotPage(FakePage):
    def __init__(self, url: str, title: str) -> None:
        super().__init__(url, title)


def test_scrape_page_snapshot_includes_excerpts(tmp_path) -> None:
    async def _run() -> None:
        store = StateStore(tmp_path / "runtime")
        store.bootstrap()
        crawler = Crawler(cdp_url="http://localhost:9222", state_store=store)
        page = SnapshotPage("https://example.com/stores", "Stores")

        async def fake_text(_page):
            return "Stores page text " * 200

        async def fake_nav(_page):
            return ["Stores", "Pricing"]

        async def fake_sections(_page):
            return [
                {"menu_item": "Stores", "semantic_text": "Stores section text " * 80},
                {"menu_item": "Pricing", "semantic_text": "Pricing section text " * 80},
            ]

        crawler.extract_semantic_text = fake_text
        crawler.discover_navigation_items = fake_nav
        crawler.crawl_navigation_sections = fake_sections

        snap = await crawler._scrape_page_snapshot(page, quarter_number=4, editable=True)

        assert snap["page_text_excerpt"].startswith("Stores page text")
        assert len(snap["section_previews"]) == 2
        assert snap["section_previews"][0]["menu_item"] == "Stores"

    asyncio.run(_run())
