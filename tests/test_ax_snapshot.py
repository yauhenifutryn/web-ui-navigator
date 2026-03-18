import asyncio

from marketplace_bot.ax_snapshot import CdpAxSnapshotProvider, McpAxSnapshotProvider, _normalize_input_node


def test_mcp_ax_snapshot_normalizes_rich_payload_into_summary_and_targets() -> None:
    async def _run() -> None:
        async def _fetcher(**_kwargs):
            return {
                "nodes": [
                    {
                        "role": "button",
                        "name": "Continue",
                        "focusable": True,
                        "disabled": False,
                        "bounds": {"x": 100, "y": 120, "width": 140, "height": 44},
                        "visibility": {"visible": True, "viewport_ratio": 1.0},
                        "styles": {"pointerEvents": "auto", "opacity": "1", "zIndex": "10"},
                    },
                    {
                        "role": "button",
                        "name": "Submit",
                        "focusable": True,
                        "disabled": True,
                        "bounds": {"x": 320, "y": 120, "width": 140, "height": 44},
                        "visibility": {"visible": True, "viewport_ratio": 1.0},
                        "styles": {"pointerEvents": "auto", "opacity": "1", "zIndex": "10"},
                        "occlusion": {"occluded": True},
                    },
                ]
            }

        provider = McpAxSnapshotProvider(fetcher=_fetcher, max_nodes=20)
        snapshot = await provider.capture(page=None, mode="live", target_scope=None, include_occlusion=True)

        assert snapshot.summary.startswith("AX:")
        assert snapshot.diagnostics["interactive_nodes"] == 2
        assert snapshot.diagnostics["disabled_nodes"] == 1
        assert snapshot.diagnostics["likely_occluded_nodes"] == 1
        assert snapshot.targets[0]["name"] == "Continue"
        assert snapshot.targets[1]["actionable"] is False
        assert snapshot.targets[1]["block_reason"] == "disabled"

    asyncio.run(_run())


def test_cdp_ax_snapshot_normalizes_ax_tree_and_runtime_geometry() -> None:
    async def _run() -> None:
        class _Session:
            async def send(self, method: str):
                assert method == "Accessibility.getFullAXTree"
                return {
                    "nodes": [
                        {
                            "backendDOMNodeId": 7,
                            "role": {"value": "button"},
                            "name": {"value": "Continue"},
                            "properties": [{"name": "focusable", "value": {"value": True}}],
                            "childIds": [],
                        }
                    ]
                }

        class _Context:
            async def new_cdp_session(self, page):
                assert page is fake_page
                return _Session()

        class _Page:
            def __init__(self) -> None:
                self.context = _Context()

            async def evaluate(self, script: str, options: dict):
                assert "querySelectorAll" in script
                assert options["includeOcclusion"] is False
                return [
                    {
                        "role": "button",
                        "name": "Continue",
                        "backendDOMNodeId": 7,
                        "focusable": True,
                        "disabled": False,
                        "visible": True,
                        "viewport_ratio": 1.0,
                        "pointer_events": "auto",
                        "opacity": 1,
                        "z_index": "10",
                        "bounds": {"x": 20, "y": 30, "width": 120, "height": 40},
                    }
                ]

        fake_page = _Page()
        provider = CdpAxSnapshotProvider(max_nodes=20)
        snapshot = await provider.capture(fake_page, mode="index", target_scope=None, include_occlusion=False)

        assert snapshot.source == "cdp"
        assert snapshot.targets[0]["name"] == "Continue"
        assert snapshot.targets[0]["bounds"]["width"] == 120
        assert snapshot.diagnostics["interactive_nodes"] == 1

    asyncio.run(_run())


def test_normalize_input_node_uses_sha256_based_ax_node_id() -> None:
    node = _normalize_input_node(
        {
            "backendDOMNodeId": 123,
            "role": "button",
            "name": "Continue",
            "bounds": {"x": 1},
            "visible": True,
            "viewport_ratio": 1.0,
            "pointer_events": "auto",
            "opacity": 1.0,
            "focusable": True,
        },
        source="cdp",
        index=0,
    )

    assert node["ax_node_id"] == "ax_83bc384d41e2"
