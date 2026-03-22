from pathlib import Path

from marketplace_bot.bridge import LocalBrowserBridge


def test_overlay_html_contains_stage_advice_and_actions():
    html = LocalBrowserBridge._overlay_html(
        {
            "title": "Live Navigator",
            "stage": "planning",
            "status": "Planning the current screen",
            "goal": "Compare iPhone prices",
            "strategic_summary": "This site uses a top navigation and product tiles.",
            "current_step": "Checking the current page fingerprint.",
            "progress": 48,
            "live_advice": ["Open the iPhone section first.", "Avoid dismissing the locale banner too early."],
            "actions": [
                {"action": "click", "target_text": "iPhone", "reasoning": "Top navigation is visible."},
                {"action": "click", "target_text": "Continue", "reasoning": "Dismiss the locale modal."},
            ],
        }
    )

    assert "Planning the current screen" in html
    assert "Compare iPhone prices" in html
    assert "Open the iPhone section first." in html
    assert "iPhone" in html
    assert "How This Works" in html
    assert "Index Progress" in html
    assert "Checking the current page fingerprint." in html
    assert "Live Notes" in html


def test_overlay_html_disables_apply_when_review_batch_is_not_ready():
    html = LocalBrowserBridge._overlay_html(
        {
            "title": "Live Navigator",
            "stage": "review_batch_ready",
            "status": "Review batch ready.",
            "goal": "Optimize the current quarter",
            "review_batch": {
                "summary": "Prepared 0 grouped changes.",
                "current_focus": "Quarter 4",
                "rationale": ["No actionable edits on this page."],
                "apply_ready": False,
            },
        }
    )

    assert "Apply Review (Beta)" in html
    assert "Manual application is safer" in html
    assert '<button type="button" class="ln-primary" data-command="apply_review_batch" disabled>Apply Review (Beta)</button>' in html


def test_overlay_html_uses_clearer_preset_and_scan_labels():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "setup",
            "title": "Live Navigator",
            "project_name": "Navigator Session",
            "goal": "Help me navigate this website.",
            "smart_scan_available": False,
        }
    )

    assert "Workspace Type" in html
    assert "How Thorough" in html
    assert "General Web" in html
    assert "Complex Workspace" in html
    assert "Quick Scan" in html
    assert "Smart Scan" in html
    assert "Deep Scan" in html
    assert "General Web is for simpler sites and short workflows." in html
    assert "Complex Workspace is for nested, recurring, data-heavy systems such as legacy business apps, internal tools, and Marketplace." in html
    assert "Deep Scan is recommended for complex workspaces because it builds reusable structure memory." in html
    assert 'class="ln-rail ln-rail-setup"' in html
    assert 'class="ln-setup-shell"' in html
    assert 'class="ln-setup-scroll"' in html
    assert 'class="ln-setup-footer"' in html
    assert 'class="ln-command-row ln-setup-actions"' in html
    assert 'form="ln-setup-form"' in html


def test_overlay_html_disables_smart_scan_when_no_prior_memory_exists():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "setup",
            "title": "Live Navigator",
            "project_name": "Navigator Session",
            "goal": "Help me navigate this website.",
            "smart_scan_available": False,
            "index_mode": "advanced",
        }
    )

    assert '<option value="adaptive" disabled>Smart Scan (requires prior memory)</option>' in html
    assert "Smart Scan turns on after the first indexed session" in html


def test_overlay_html_uses_complex_workspace_labels_in_active_session_dock():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "indexing",
            "status": "Indexing the current workspace.",
            "goal": "Review the current site.",
            "domain_pack": "marketplace_simulation",
            "mode": "complex_workspace",
            "index_mode": "advanced",
        }
    )

    assert "Complex Workspace" in html
    assert "Simulation Workspace" not in html


def test_overlay_html_uses_mode_specific_command_row_and_activity_strip_for_complex_workspace():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "review_batch_ready",
            "status": "Review ready.",
            "goal": "Review the site.",
            "mode": "complex_workspace",
            "activity_log_tail": ["Indexed 14 visible workflow nodes.", "Prepared 3 anchored notes."],
            "coverage_summary": {
                "discovered_nodes": 14,
                "indexed_nodes": 12,
                "skipped_nodes": 2,
                "blocked_nodes": 0,
                "alias_collapsed_nodes": 1,
            },
        }
    )

    assert 'data-command="enter_live_advice"' in html
    assert 'data-command="prepare_review_batch"' in html
    assert 'data-command="open_review"' in html
    assert 'data-command="show_setup"' in html
    assert 'data-command="open_sessions"' in html
    assert 'data-command="open_logs"' not in html
    assert "View Map" in html
    assert "Current phase" in html
    assert "Indexed 14 visible workflow nodes." in html
    assert "Prepared 3 anchored notes." in html
    assert 'data-rail-toggle' in html


def test_overlay_html_hides_live_notes_and_executable_batch_in_review_only_mode():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "review_batch_ready",
            "status": "Review ready.",
            "goal": "Review the site.",
            "mode": "review_only",
            "activity_log_tail": ["Extracted 6 MacBook offers.", "Cheapest model identified."],
        }
    )

    assert 'data-command="enter_live_advice"' not in html
    assert "Executable Batch" not in html
    assert 'data-command="prepare_review_batch"' in html
    assert 'data-command="open_review"' in html
    assert "View Map" in html


def test_overlay_html_hides_dock_during_setup_view():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "setup",
            "title": "Live Navigator",
            "project_name": "Navigator Session",
            "goal": "Help me navigate this website.",
        }
    )

    assert 'class="ln-dock"' not in html
    assert '>Stop<' not in html


def test_overlay_html_only_shows_stop_controls_when_agent_is_actively_working():
    idle_html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "index_summary_ready",
            "status": "Index complete.",
            "goal": "Review the site.",
        }
    )
    active_html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "indexing",
            "status": "Indexing visible workflow areas.",
            "goal": "Review the site.",
        }
    )
    live_advice_html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "live_advice",
            "status": "Live notes are following the current page.",
            "goal": "Review the site.",
        }
    )

    assert 'class="ln-dock"' in idle_html
    assert 'class="ln-dock"' in live_advice_html
    assert 'Current phase' in idle_html
    assert 'Current phase' in live_advice_html
    assert 'data-command="stop_session"' not in idle_html
    assert 'data-command="stop_session"' not in live_advice_html
    assert 'data-command="stop_session"' in active_html
    assert 'class="ln-dock"' in active_html
    assert 'data-auto-collapse-rail="true"' in active_html
    assert 'data-auto-collapse-rail="false"' in idle_html


def test_overlay_html_surfaces_last_capture_metadata():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "indexing",
            "status": "Indexing visible workflow areas.",
            "goal": "Review the site.",
            "last_capture_at": "2026-03-16T00:54:00Z",
            "last_capture_path": "runtime/artifacts/sess_test/2026-03-16T00-54-00Z.png",
            "last_capture_page": "Strategic Graphs",
        }
    )

    assert "Last Capture" in html
    assert "2026-03-16T00:54:00Z" in html
    assert "2026-03-16T00-54-00Z.png" in html
    assert "Strategic Graphs" in html


def test_overlay_html_surfaces_long_page_capture_details_when_available():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "indexing",
            "status": "Indexing visible workflow areas.",
            "goal": "Review the site.",
            "last_capture_at": "2026-03-22T14:00:00Z",
            "last_capture_page": "Invest in the Future",
            "last_capture_region": {
                "slice_count": 3,
                "scroll_height": 2460,
                "viewport_height": 820,
            },
        }
    )

    assert "Viewport slices" in html
    assert "3 total" in html
    assert "2 below the fold" in html
    assert "2460px page" in html


def test_overlay_html_shows_return_to_active_session_in_setup():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "setup",
            "title": "Live Navigator",
            "project_name": "Navigator Session",
            "goal": "Help me navigate this website.",
            "active_session_id": "sess_active",
        }
    )

    assert "Back to Session" in html
    assert "data-session-id='sess_active'" in html


def test_overlay_css_adds_click_feedback_for_buttons():
    css = LocalBrowserBridge._overlay_css()

    assert "transition: transform .12s ease" in css
    assert ".ln-primary:active" in css
    assert ".ln-clicked" in css
    assert "scale(.985)" in css
    assert ".ln-setup-actions" in css
    assert ".ln-setup-footer" in css
    assert ".ln-setup-scroll" in css
    assert ".ln-rail-setup" in css
    assert "overflow: hidden;" in css
    assert "overflow: auto;" in css
    assert ".ln-rail-toggle" in css
    assert '[data-rail-collapsed="true"] .ln-rail' in css


def test_overlay_css_offsets_the_bottom_dock_and_attaches_the_handle_to_the_rail():
    css = LocalBrowserBridge._overlay_css()

    assert "--ln-rail-width" in css
    assert "--ln-dock-clearance" in css
    assert "box-sizing: border-box;" in css
    assert "min(360px, calc(100vw - 34px))" in css
    assert "right: calc(20px + var(--ln-rail-width) - 4px);" in css
    assert "border-right: none;" in css
    assert "[data-rail-collapsed=\"false\"]" in css
    assert "calc(100vw - 48px - var(--ln-dock-clearance))" in css


def test_overlay_runtime_stacks_inline_notes_instead_of_reusing_one_position():
    source = Path("src/marketplace_bot/bridge.py").read_text(encoding="utf-8")

    assert "const placedNotes = [];" in source
    assert "while (placedNotes.some" in source


def test_overlay_runtime_opens_the_map_in_a_new_tab():
    source = Path("src/marketplace_bot/bridge.py").read_text(encoding="utf-8")

    assert "window.open(panel.map_url" in source
    assert "window.open(panel.review_url" in source
    assert "noopener,noreferrer" in source


def test_overlay_html_uses_cached_review_actions_when_pending_actions_are_empty():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "review_batch_ready",
            "status": "Review ready.",
            "goal": "Optimize the current quarter",
            "actions": [
                {"action": "click", "target_text": "Hangzhou", "reasoning": "Lowest-cost visible store candidate."},
            ],
            "review_batch": {
                "summary": "Prepared detailed review.",
                "current_focus": "Quarter 4",
                "rationale": ["Cached review is available."],
                "apply_ready": True,
                "items": [],
            },
        }
    )

    assert "Hangzhou" in html
    assert "Cached review actions will appear here" not in html


def test_overlay_html_disables_post_index_actions_until_the_site_is_ready():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "session_ready_to_index",
            "status": "Session created. Run the site index first.",
            "goal": "Optimize the current quarter",
            "site_check_required": True,
            "mode": "complex_workspace",
        }
    )

    assert '<button type="button" class="ln-secondary" data-command="enter_live_advice" disabled>Live Notes</button>' in html
    assert '<button type="button" class="ln-secondary" data-command="prepare_review_batch" disabled>Refresh Review</button>' in html


def test_overlay_html_disables_post_index_actions_when_coverage_is_degraded():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "index_summary_ready",
            "status": "Coverage degraded.",
            "goal": "Optimize the current quarter",
            "site_check_required": False,
            "site_ready": False,
            "mode": "complex_workspace",
            "degraded_reason": "Coverage degraded: only 1 visible node was indexed.",
        }
    )

    assert '<button type="button" class="ln-secondary" data-command="enter_live_advice" disabled>Live Notes</button>' in html
    assert '<button type="button" class="ln-secondary" data-command="prepare_review_batch" disabled>Refresh Review</button>' in html
    assert '<button type="button" class="ln-secondary" data-command="open_review" disabled>See Review</button>' in html


def test_overlay_html_lets_users_return_to_setup_from_session_view():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "review_batch_ready",
            "status": "Review ready.",
            "goal": "Review the site.",
        }
    )

    assert 'data-command="show_setup"' in html
    assert "Start Another Session" in html


def test_overlay_html_shows_review_surface_when_opened():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "review_batch_ready",
            "status": "Review ready.",
            "goal": "Review the site.",
            "mode": "complex_workspace",
            "review_open": True,
            "coverage_summary": {
                "discovered_nodes": 9,
                "indexed_nodes": 7,
                "skipped_nodes": 1,
                "blocked_nodes": 1,
                "alias_collapsed_nodes": 2,
            },
            "structure_map_summary": {
                "mode": "complex_workspace",
                "active_node": "Pricing",
                "editable_quarter": 4,
                "parent_sections": ["Marketing", "Finance"],
            },
            "structure_manifest": {
                "nodes": [
                    {"title": "Pricing", "url": "https://example.com/pricing", "section_count": 3, "quarter_number": 4, "editable": True},
                    {"title": "Cash Flow", "url": "https://example.com/cash-flow", "section_count": 0, "quarter_number": 4, "editable": False},
                ]
            },
            "review_batch": {
                "summary": "Prepared a grounded review.",
                "current_focus": "Quarter 4 Pricing",
                "items": [
                    {
                        "title": "Premium price gap",
                        "page_hint": "Pricing",
                        "anchor_text": "EDGETOSPEED",
                        "field_type": "numeric_row",
                        "current_value": "1,159",
                        "recommended_value": "1,199",
                        "why_it_matters": "Premium positioning is already visible in the current ladder.",
                        "evidence": "Visible price ladder shows EDGETOSPEED above the rest.",
                        "priority": "high",
                        "confidence": 0.82,
                        "actionability": "manual_only",
                        "dependencies": ["Confirm competitor price page."],
                        "requires_followup_check": True,
                    }
                ],
            },
        }
    )

    assert "Full Review" in html
    assert "Current value" in html
    assert "Recommended" in html
    assert "Why it matters" in html
    assert "Evidence" in html


def test_overlay_html_shows_structure_checklist_counts_when_available():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "indexing",
            "status": "Indexing visible workflow areas.",
            "goal": "Review the site.",
            "site_check_details": {
                "change_status": "changed",
                "strategy": "partial",
                "matched_nodes": 18,
                "changed_nodes_count": 2,
                "new_nodes_count": 3,
                "removed_nodes_count": 1,
                "current_node_count": 23,
            },
        }
    )

    assert "Structure Checklist" in html
    assert "Reused" in html
    assert "18" in html
    assert "Changed" in html
    assert "2" in html
    assert "New" in html
    assert "3" in html
    assert "Removed" in html
    assert "1" in html
    assert "Visible" in html
    assert "23" in html
    assert "partial refresh" in html


def test_overlay_html_shows_compact_ax_diagnostics_when_available():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "review_batch_ready",
            "status": "Review ready.",
            "goal": "Review the site.",
            "ax_summary": "AX: 42 interactive nodes, 3 blocked, 1 likely occluded",
        }
    )

    assert "AX: 42 interactive nodes, 3 blocked, 1 likely occluded" in html


def test_overlay_html_shows_structure_map_preview_when_logs_are_open():
    html = LocalBrowserBridge._overlay_html(
        {
            "view": "session",
            "title": "Live Navigator",
            "stage": "review_batch_ready",
            "status": "Review ready.",
            "goal": "Review the site.",
            "logs_open": True,
            "structure_map_preview": [
                "Quarter 4 Finance | 6 sections | editable",
                "Quarter 4 Cash Flow | 0 sections",
            ],
            "structure_map_total": 12,
        }
    )

    assert '<p class="ln-label">Structure Map</p>' in html
    assert "Quarter 4 Finance | 6 sections | editable" in html
    assert "Showing 2 of 12 indexed nodes." in html


def test_overlay_css_keeps_more_vertical_room_for_the_desktop_rail():
    css = LocalBrowserBridge._overlay_css()

    assert "max-height: calc(100dvh - 32px);" in css


def test_overlay_css_offsets_active_dock_from_the_rail_and_wraps_long_status_text():
    css = LocalBrowserBridge._overlay_css()

    assert "left: calc((100vw - var(--ln-dock-clearance)) / 2);" in css
    assert "transform: translateX(-50%);" in css
    assert "width: min(520px, calc(100vw - 48px - var(--ln-dock-clearance)));" in css
    assert "overflow-wrap: anywhere;" in css
