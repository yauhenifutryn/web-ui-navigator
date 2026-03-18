import json

from marketplace_bot.goal_compiler import GoalCompiler
from marketplace_bot.navigator_models import SiteMemory
from marketplace_bot.site_intelligence import (
    analyze_site_change,
    build_site_memory_key,
    build_structure_manifest,
    choose_index_strategy,
    compute_site_fingerprint,
    compute_structure_digest,
    merge_site_index,
)
from marketplace_bot.site_memory_repository import LocalJsonSiteMemoryRepository


def test_goal_compiler_assigns_default_index_modes() -> None:
    compiler = GoalCompiler()

    generic = compiler.compile("Compare iPhone prices on Apple and guide me quickly.")
    simulation = compiler.compile("Audit the current Marketplace Simulation quarter.")

    assert generic.index_mode == "advanced"
    assert simulation.index_mode == "advanced"


def test_local_site_memory_repository_round_trip(tmp_path) -> None:
    repo = LocalJsonSiteMemoryRepository(tmp_path / "runtime")
    memory = SiteMemory(
        memory_key="mem_test",
        site_origin="https://example.com",
        domain_pack="generic_web",
        index_mode="adaptive",
        site_fingerprint="fingerprint",
        structure_digest="digest",
        strategic_summary="Known site.",
        indexed_context={"site_index": {"navigation_items": ["Home"]}},
        last_checked_at="2026-03-09T00:00:00Z",
        last_indexed_at="2026-03-09T00:00:00Z",
        change_status="unchanged",
        change_summary="No changes detected.",
    )

    repo.save(memory)
    loaded = repo.get("mem_test")
    by_origin = repo.get_by_origin("generic_web", "https://example.com")

    assert loaded is not None
    assert loaded.site_origin == "https://example.com"
    assert by_origin is not None
    assert by_origin.memory_key == "mem_test"


def test_site_memory_key_uses_sha256_stable_prefix() -> None:
    assert build_site_memory_key("generic_web", "https://apple.com") == "mem_3057873bc1f5"


def test_analyze_site_change_detects_new_and_unchanged_nodes() -> None:
    previous_site_index = {
        "navigation_items": ["Home", "Store", "iPhone"],
        "site_map": [
            {"url": "https://apple.com", "title": "Home", "section_count": 3},
            {"url": "https://apple.com/iphone", "title": "iPhone", "section_count": 4},
        ],
    }
    previous = SiteMemory(
        memory_key="mem_existing",
        site_origin="https://apple.com",
        domain_pack="generic_web",
        index_mode="advanced",
        site_fingerprint=compute_site_fingerprint("https://apple.com", previous_site_index),
        structure_digest="digest_a",
        strategic_summary="Existing map.",
        indexed_context={"site_index": previous_site_index},
        last_checked_at="2026-03-09T00:00:00Z",
        last_indexed_at="2026-03-09T00:00:00Z",
        change_status="unchanged",
        change_summary="No changes detected.",
    )

    unchanged_probe = {
        "site_origin": "https://apple.com",
        "navigation_items": ["Home", "Store", "iPhone"],
        "site_map": [
            {"url": "https://apple.com/iphone", "title": "iPhone", "section_count": 4},
        ],
    }
    unchanged = analyze_site_change(previous, unchanged_probe)
    assert unchanged["change_status"] == "unchanged"
    assert unchanged["refresh_scope"] == "partial"

    changed_probe = {
        "site_origin": "https://apple.com",
        "navigation_items": ["Home", "Store", "iPhone", "Compare"],
        "site_map": [
            {"url": "https://apple.com/iphone/compare", "title": "Compare", "section_count": 2},
        ],
    }
    changed = analyze_site_change(previous, changed_probe)
    assert changed["change_status"] == "changed"
    assert changed["refresh_scope"] == "partial"
    assert changed["new_nodes"] == ["https://apple.com/iphone/compare"]


def test_analyze_site_change_reports_removed_nodes_and_checklist_counts() -> None:
    previous_site_index = {
        "navigation_items": ["Home", "Store", "iPhone", "Compare"],
        "site_map": [
            {"url": "https://apple.com", "title": "Home", "section_count": 3},
            {"url": "https://apple.com/iphone", "title": "iPhone", "section_count": 4},
            {"url": "https://apple.com/iphone/compare", "title": "Compare", "section_count": 2},
        ],
    }
    previous = SiteMemory(
        memory_key="mem_existing",
        site_origin="https://apple.com",
        domain_pack="generic_web",
        index_mode="advanced",
        site_fingerprint=compute_site_fingerprint("https://apple.com", previous_site_index),
        structure_digest=compute_structure_digest(previous_site_index),
        strategic_summary="Existing map.",
        indexed_context={"site_index": previous_site_index},
        last_checked_at="2026-03-09T00:00:00Z",
        last_indexed_at="2026-03-09T00:00:00Z",
        change_status="unchanged",
        change_summary="No changes detected.",
    )

    changed_probe = {
        "site_origin": "https://apple.com",
        "navigation_items": ["Home", "Store", "iPhone"],
        "site_map": [
            {"url": "https://apple.com", "title": "Home", "section_count": 3},
            {"url": "https://apple.com/iphone", "title": "iPhone", "section_count": 6},
        ],
    }

    changed = analyze_site_change(previous, changed_probe)

    assert changed["change_status"] == "changed"
    assert changed["changed_nodes"] == ["https://apple.com/iphone"]
    assert changed["removed_nodes"] == ["https://apple.com/iphone/compare"]
    assert changed["matched_nodes"] == 1
    assert changed["previous_node_count"] == 3
    assert changed["current_node_count"] == 2


def test_compute_structure_digest_uses_full_site_map_instead_of_truncating_tail_nodes() -> None:
    base_site_map = [
        {"url": f"https://example.com/node-{index}", "title": f"Node {index}", "section_count": index % 4}
        for index in range(85)
    ]
    baseline = {
        "navigation_items": [f"Node {index}" for index in range(85)],
        "site_map": base_site_map,
    }
    changed = {
        "navigation_items": [f"Node {index}" for index in range(85)],
        "site_map": [
            *base_site_map[:-1],
            {"url": "https://example.com/node-84", "title": "Node 84 Updated", "section_count": 3},
        ],
    }

    assert compute_structure_digest(baseline) != compute_structure_digest(changed)


def test_compute_structure_digest_uses_sha256_width() -> None:
    digest = compute_structure_digest(
        {
            "navigation_items": ["Home"],
            "site_map": [{"url": "https://apple.com", "title": "Home", "section_count": 1}],
        }
    )

    assert digest == "6e1f002601955fef65f67b8a12d8ab8e480ae6b1f2313a5f33778c9afc29b741"


def test_choose_index_strategy_respects_modes_and_change_scope() -> None:
    lightweight = choose_index_strategy("lightweight", None, {"change_status": "new", "refresh_scope": "lightweight"})
    adaptive_cold_start = choose_index_strategy("adaptive", None, {"change_status": "new", "refresh_scope": "full"})
    adaptive = choose_index_strategy("adaptive", {"memory_key": "m1"}, {"change_status": "unchanged", "refresh_scope": "partial"})
    advanced = choose_index_strategy("advanced", {"memory_key": "m1"}, {"change_status": "changed", "refresh_scope": "full"})
    advanced_reuse_case = choose_index_strategy("advanced", {"memory_key": "m1"}, {"change_status": "unchanged", "refresh_scope": "partial"})

    assert lightweight == "lightweight"
    assert adaptive_cold_start == "full"
    assert adaptive == "partial"
    assert advanced == "full"
    assert advanced_reuse_case == "full"


def test_merge_site_index_preserves_known_nodes_and_adds_new_page() -> None:
    previous = {
        "navigation_items": ["Marketing", "Sales Channel"],
        "site_map": [
            {"url": "https://example.com/q3/marketing", "title": "Marketing", "section_count": 5},
        ],
        "editable_quarter": 3,
    }
    current = {
        "navigation_items": ["Marketing", "Sales Channel", "Demand Projection"],
        "site_map": [
            {"url": "https://example.com/q4/demand", "title": "Demand Projection", "section_count": 3},
        ],
        "editable_quarter": 4,
    }

    merged = merge_site_index(previous, current)

    urls = [item["url"] for item in merged["site_map"]]
    assert "https://example.com/q3/marketing" in urls
    assert "https://example.com/q4/demand" in urls
    assert merged["editable_quarter"] == 4
    assert json.dumps(merged)


def test_build_structure_manifest_uses_completed_quarters_and_sections_when_site_map_is_empty() -> None:
    site_index = {
        "title": "Sales Channel",
        "url": "https://play.marketplace-simulation.com/q4/sales-channel",
        "editable_quarter": 4,
        "navigation_items": ["Performance Report", "Marketing", "Sales Channel", "Finance"],
        "site_map": [],
        "completed_quarters": [
            {
                "title": "Sales Channel",
                "url": "https://play.marketplace-simulation.com/q4/sales-channel",
                "quarter_number": 4,
                "editable": True,
                "sections": [
                    {"menu_item": "Performance Report", "navigation_items": ["Market Share", "Cash Flow"]},
                    {"menu_item": "Marketing", "navigation_items": ["Pricing", "Advertising"]},
                ],
            },
            {
                "title": "Summary of Decisions",
                "url": "https://play.marketplace-simulation.com/q3/summary",
                "quarter_number": 3,
                "editable": False,
                "sections": [],
            },
        ],
        "sections": [
            {"menu_item": "Performance Report", "navigation_items": ["Market Share", "Cash Flow"]},
            {"menu_item": "Marketing", "navigation_items": ["Pricing", "Advertising"]},
            {"menu_item": "Finance", "navigation_items": ["Cash Flow"]},
        ],
    }

    manifest = build_structure_manifest(site_index)

    titles = [item["title"] for item in manifest["nodes"]]
    assert manifest["node_count"] >= 5
    assert "Sales Channel" in titles
    assert "Summary of Decisions" in titles
    assert "Performance Report" in titles
    assert "Marketing" in titles
    assert "Finance" in titles


def test_build_structure_manifest_preserves_parent_relationships_for_nested_nodes() -> None:
    site_index = {
        "title": "Mac",
        "url": "https://www.apple.com/mac/",
        "navigation_items": ["Mac", "Mac mini", "Compare", "Buy"],
        "site_map": [
            {
                "key": "root::mac",
                "url": "https://www.apple.com/mac/",
                "title": "Mac",
                "section_count": 3,
            },
            {
                "key": "family::mac-mini",
                "url": "https://www.apple.com/mac-mini/",
                "title": "Mac mini",
                "section_count": 2,
                "parent_key": "root::mac",
            },
            {
                "key": "leaf::compare",
                "url": "https://www.apple.com/mac-mini/compare/",
                "title": "Compare Mac mini Models",
                "section_count": 0,
                "parent_key": "family::mac-mini",
            },
        ],
        "sections": [
            {
                "menu_item": "Buy Mac mini",
                "url": "https://www.apple.com/shop/buy-mac/mac-mini",
                "navigation_items": ["Configure"],
                "parent_key": "family::mac-mini",
            }
        ],
    }

    manifest = build_structure_manifest(site_index)
    nodes = {item["key"]: item for item in manifest["nodes"]}

    assert nodes["family::mac-mini"]["parent_key"] == "root::mac"
    assert nodes["leaf::compare"]["parent_key"] == "family::mac-mini"
    section_node = next(item for item in manifest["nodes"] if item["title"] == "Buy Mac mini")
    assert section_node["parent_key"] == "family::mac-mini"


def test_build_structure_manifest_derives_marketplace_hierarchy_from_legacy_urls() -> None:
    site_index = {
        "title": "Price and Priority",
        "url": "https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=pricing&parentResource=pricing_&tab=workspace&quarter=4&language=en-us",
        "site_map": [
            {
                "url": "https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=pricing&parentResource=pricing_&tab=workspace&quarter=4&language=en-us",
                "title": "Price and Priority",
                "section_count": 4,
            },
            {
                "url": "https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=performance-report&parentResource=pricing_&tab=workspace&quarter=4&language=en-us",
                "title": "Performance Report",
                "section_count": 3,
            },
        ],
    }

    manifest = build_structure_manifest(site_index)
    nodes = {item["key"]: item for item in manifest["nodes"]}

    assert "marketplace::quarter::4" in nodes
    assert "marketplace::quarter::4::tab::workspace" in nodes
    assert "marketplace::quarter::4::tab::workspace::parent::pricing" in nodes
    assert nodes["marketplace::quarter::4::tab::workspace"]["parent_key"] == "marketplace::quarter::4"
    assert nodes["marketplace::quarter::4::tab::workspace::parent::pricing"]["parent_key"] == "marketplace::quarter::4::tab::workspace"
    page_node = nodes["https://play.marketplace-simulation.com/mpl/web7/engine.php?tpl=student&resource=performance-report&parentResource=pricing_&tab=workspace&quarter=4&language=en-us"]
    assert page_node["parent_key"] == "marketplace::quarter::4::tab::workspace::parent::pricing"
