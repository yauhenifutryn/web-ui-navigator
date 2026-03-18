import asyncio

import pytest

from marketplace_bot.agents.crawler import Crawler


class FakePage:
    async def title(self) -> str:
        return "Marketplace - Workspace"


class FakeContext:
    def __init__(self) -> None:
        self.pages = [FakePage()]


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts = [FakeContext()]

    async def close(self) -> None:
        return None


class FakeChromium:
    async def connect_over_cdp(self, cdp_url: str) -> FakeBrowser:
        assert cdp_url == "http://localhost:9222"
        return FakeBrowser()


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()

    async def stop(self) -> None:
        return None


class FakeManager:
    async def start(self) -> FakePlaywright:
        return FakePlaywright()


def test_attach_and_read_page_title(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        monkeypatch.setattr("marketplace_bot.agents.crawler.async_playwright", lambda: FakeManager())

        crawler = Crawler(cdp_url="http://localhost:9222")
        await crawler.attach()

        title = await crawler.get_current_page_title()
        assert title == "Marketplace - Workspace"

        await crawler.close()

    asyncio.run(_run())
