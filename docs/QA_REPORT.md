# Live Navigator QA Report

- Timestamp: 2026-03-15T14:40:21Z
- Build/runtime tested: local workspace checkout on branch `main`, FastAPI app `marketplace_bot.api.main:app`, `uv run` Python environment, Chrome CDP on `http://127.0.0.1:9222`.
- Environment used: macOS local dev machine, server on `http://127.0.0.1:8002`, dedicated Chrome debug profile, live Marketplace site, live Apple site.
- Session mode tested: `complex_workspace` and `review_only`.
- Target site tested: Marketplace Simulation Quarter 4 flow and Apple Mac navigation and Mac mini pricing flow.
- Automated baseline: `uv run --group dev python -m pytest -q` => `131 passed, 29 warnings`.

## Failures Found

- Generic branch selection could still waste crawl budget on `compare` / `buy` leaves before exploring the higher-value family page.
- The structure graph for legacy sessions was still too flat to be useful because stale manifests had no parent links, and Marketplace graphs did not reconstruct quarter / task-workspace / parent-resource hierarchy.
- Synthetic graph helper nodes initially inflated coverage counts, which could have masked a thin Marketplace crawl if coverage gating treated them as indexed pages.
- The right-rail collapse toggle was visually intruding into the panel body because the rail sizing math used the content box instead of the rendered box.
- Google Cloud Console proof is still unverified in this environment because the debug browser profile is not signed in and `gcloud` is not installed.

## Fix Status

- Fixed: generic branch selection now uses a universal parent-branch heuristic, so relevant family pages outrank action leaves like `compare`, `buy`, `configure`, and `pricing` when the goal terms match.
- Fixed: the structure graph now rebuilds from `indexed_context.site_index` and synthesizes Marketplace quarter / tab / parent-resource containers when stored manifests are flat, so legacy sessions still render an informative hierarchy.
- Fixed: synthetic graph containers are explicitly marked and excluded from coverage/degraded gating, so Marketplace fail-closed logic still measures real indexed pages only.
- Fixed: the right rail and its collapse tab now share the same box model, and the tab is offset farther right so it visibly connects to the panel seam instead of floating inside the body.
- Open: Google Cloud Console proof could not be completed from this machine because Google Cloud authentication is unavailable here.
- Open risk: bridge logs still show intermittent `ax_capture_failed` events with `"'NoneType' object has no attribute 'lower'"`, but this did not block the verified flows in this cycle.

## Retest Status

- Automated retest after fixes: `uv run --group dev python -m pytest -q` => `131 passed, 29 warnings`.
- Live retest after fixes:
Evidence: targeted regression `tests/test_bridge_controller.py::test_score_generic_link_prefers_relevant_parent_branches_over_leaf_actions` is green, proving the family-link preference is structural rather than a `Mac` special case.
Evidence: live Apple seam checks on session `sess_0f5ca6373a` showed `rail.left=790`, `toggle.right=794`, `dock.right=706`, and `toggleBorderRight=0px none`, so the handle now overlaps the rail seam by 4 px without the extra separator line and the dock still clears the rail completely.
Evidence: the Apple map tab now roots the graph under `Apple` instead of rendering only a flat line of leaf pages.
Evidence: the Marketplace map for `sess_309ebea131` now renders quarter and task/workspace containers with child nodes under them instead of a pure vertical list, and the node-details panel shows `Quarter 1 -> Task -> Welcome To Marketplace` style ancestry.
Evidence: Marketplace session `sess_309ebea131` remains `review_batch_ready` with plausible real-page coverage (`discovered_nodes: 76`, `indexed_nodes: 60`, `skipped_nodes: 16`, `current_node_count: 60`) after synthetic hierarchy nodes were excluded from coverage gating.
Evidence: `/architecture` now renders two judge-facing Excalidraw PNG exports, `docs/diagrams/live_navigator_system_architecture.png` and `docs/diagrams/live_navigator_session_lifecycle.png`, and the matching static assets are served from `/static/live_navigator_system_architecture.png` and `/static/live_navigator_session_lifecycle.png`.
Evidence: GCP Console verification hit the Google sign-in page instead of an authenticated Cloud Run view, so that checklist item remains open.

## Remaining Open Issues

- Blocking for hosted-backend proof: Cloud Run service or logs cannot be shown in GCP Console from this environment because the Chrome debug profile is not signed in and `gcloud` is unavailable.
- Non-blocking risk: `ax_capture_failed` with `"'NoneType' object has no attribute 'lower'"` still appears in logs during some index/live captures and should be cleaned up before a public demo if time allows.

## Release Recommendation

- Recommendation: `CONDITIONAL GO`.
Evidence: the local product flows, Marketplace flagship benchmark, review-only Apple path, universal family-link traversal heuristic, structure-graph hierarchy, automated suite, and wrapper launch path are green. The only remaining red item is authenticated Google Cloud Console proof, which is an environment/access gap rather than a local product failure.

# Live Navigator QA Checklist

## Test Setup

- [x] Start from a clean local state with `make reset-cache` if you want a fresh session baseline.
Evidence: runtime state was reset with `scripts/reset_runtime.py`, which is the implementation behind `make reset-cache`.
- [x] Launch the app with `make launch`.
Evidence: `make launch` succeeded and printed `Overlay bootstrapped into the active website tab. No localhost dashboard was opened.`.
- [x] Confirm the dedicated Chrome debug window opens.
Evidence: live CDP attachment to `http://127.0.0.1:9222` succeeded throughout Marketplace and Apple QA.
- [x] Confirm the local server is reachable at `http://127.0.0.1:8002`.
Evidence: health checks, session APIs, and overlay commands succeeded throughout the run.
- [x] Open the real target site in the dedicated Chrome debug window, not in your normal browser profile.
Evidence: Marketplace and Apple checks both ran inside the controlled Chrome debug profile.

## Bootstrap And Recovery

- [x] Confirm the bootstrap overlay initializes on the target tab.
Evidence: repeated `POST /api/bootstrap-overlay` calls injected the overlay into the live Marketplace and Apple tabs.
- [x] If no target tab is available, confirm the app shows a recovery message instead of crashing.
Evidence: automated coverage exists in `tests/test_api_dashboard.py::test_bootstrap_overlay_returns_recovery_message_when_target_tab_is_missing` and `..._when_cdp_is_not_ready`.
- [x] Confirm the bootstrap page is only a setup and recovery surface, not the main workflow UI.
Evidence: the working UI remained inside the real website overlay; `make launch` explicitly reported that no localhost dashboard was opened.

## Session Creation

- [x] Start a new session from the overlay.
Evidence: live sessions were created from the overlay for Marketplace (`sess_309ebea131`) and Apple (`sess_f925164b51`).
- [x] Confirm `Project`, `Goal`, `Workspace Type`, and `How Thorough` are visible.
Evidence: the setup rail showed all four controls before session creation.
- [x] On Marketplace, confirm the defaults are `Complex Workspace` and `Deep Scan`.
Evidence: Marketplace setup defaults showed `Complex Workspace` and `Deep Scan`.
- [x] Confirm the primary CTA is visible without awkward scrolling on desktop.
Evidence: `Start New Session` stayed fully visible in the default launch viewport after the setup-rail fix.
- [x] Confirm `Live Notes` and `Refresh Review` are disabled before indexing.
Evidence: a fresh Marketplace session after stop/reset showed `Live Notes`, `Refresh Review`, and `See Review` disabled before indexing.

## Indexing Flow

- [x] Click `Index Site First`.
Evidence: both Marketplace and Apple sessions were started from the overlay command row.
- [x] Confirm the overlay enters an active indexing state.
Evidence: live runs switched into active indexing, showed the bottom activity strip, and exposed `Stop`.
- [x] Confirm the progress bar moves and the status text changes during indexing.
Evidence: persisted progress advanced through the initial fingerprint check and later crawl/index steps.
- [x] Confirm the new `Structure Checklist` card appears during or immediately after the fingerprint check.
Evidence: live Marketplace and resumed Apple runs both rendered the `Structure Checklist` card.
- [x] Confirm the card shows `Reused`, `Changed`, `New`, `Removed`, and `Visible` counts.
Evidence: Marketplace and Apple cards showed those counters in the overlay.
- [x] Confirm the card also shows the selected refresh strategy, for example `partial refresh` or `full refresh`.
Evidence: Marketplace showed a full refresh during earlier drift; resumed Apple showed `Changed · partial refresh`.
- [x] Confirm the browser returns to the original working page after indexing completes.
Evidence: Marketplace returned to Quarter 4 pages after crawl completion, and the final Apple Mac mini review-only run returned to `https://www.apple.com/mac/` after cross-page exploration.

## Post-Index State

- [x] Confirm indexing ends in a review-ready state, not a dead-end state.
Evidence: Marketplace `sess_309ebea131` completed as `review_batch_ready` with `review_ready: true`.
- [x] Confirm `Live Notes` becomes enabled after indexing.
Evidence: Marketplace post-index command row enabled `Live Notes`.
- [x] Confirm `Refresh Review` becomes enabled after indexing.
Evidence: Marketplace and Apple post-index command rows enabled `Refresh Review`.
- [x] Confirm `Index Summary` is populated with a meaningful summary, not empty placeholders.
Evidence: Marketplace post-index summary included strategic summary, current focus, and detected changes.
- [x] Confirm `Detected Changes` matches the structure-checklist story.
Evidence: the Marketplace and Apple summaries aligned with the structure-checklist drift status and refresh strategy.

## Live Notes

- [x] Click `Live Notes`.
Evidence: Marketplace `Price and Priority` entered live-advice mode from the overlay.
- [x] Confirm the overlay enters live-advice mode without errors.
Evidence: the rail auto-collapsed, the session moved into live-advice behavior, and inline notes rendered.
- [x] Change to another relevant page and confirm notes refresh only when the page actually changes.
Evidence: live notes changed after navigating from broader Marketplace pages into `Price and Priority`; automated coverage in `tests/test_overlay_first_contract.py::test_overlay_first_api_contract` confirms repeated identical page signatures are ignored.
- [x] Confirm notes are page-specific, not generic filler.
Evidence: live notes specifically referenced `EDGETOSPEED` premium positioning and `EDGETOWORK1` entry positioning on `Price and Priority`.
- [x] Confirm inline notes anchor near relevant visible controls when possible.
Evidence: two inline notes rendered in-page near the live pricing content and, after the stacking fix, occupied distinct positions (`top: 12px` and `top: 172px`).

## Review Batch

- [x] Click `Refresh Review`.
Evidence: Marketplace and Apple review batches were rebuilt from the overlay.
- [x] Confirm a review summary appears.
Evidence: Marketplace showed a typed review summary for Quarter 4; Apple showed `Cheapest visible option: Mac mini at $599.`.
- [x] Confirm the recommendations match the current page and current quarter.
Evidence: Marketplace `Price and Priority` review items referenced visible brands and Quarter 4; Apple review-only items referenced visible `Mac mini` offers and prices.
- [x] Confirm the rationale is understandable and grounded in visible UI state.
Evidence: Marketplace review items cited the visible price ladder and ending cash; Apple review cited the visible `Mac mini` `$599` rows gathered from the explored Apple pages.
- [x] Confirm `Apply Review (Beta)` is disabled when there is nothing safe to apply.
Evidence: live Marketplace `Price and Priority` review and live Apple review-only flow both kept `Apply Review (Beta)` disabled.
- [x] Confirm `Apply Review (Beta)` becomes enabled only when executable actions exist.
Evidence: automated coverage now enforces this contract through `tests/test_api_dashboard.py::test_apply_review_batch_executes_only_auto_approved_actions` and the overlay rendering contract in `tests/test_agent_overlay.py`.

## Logs And Saved Sessions

- [x] Click `Saved Sessions` and confirm the panel opens inline.
Evidence: live Marketplace session showed an inline `Saved Sessions` card when the button was toggled.
- [x] Click `Saved Sessions` again and confirm it closes.
Evidence: the second toggle removed the inline `Saved Sessions` card.
- [x] Click `Logs` and confirm the panel opens inline.
Evidence: baseline item is obsolete by design; the primary row no longer shows `Logs`, and the replacement `View Map` action opens a dedicated structure-graph browser tab instead.
- [x] Click `Logs` again and confirm it closes.
Evidence: baseline item is obsolete by design; the replacement `View Map` surface is a separate browser tab rather than an inline toggle.
- [x] Confirm the current session can be resumed from the saved sessions list.
Evidence: after switching to setup, the saved-session button resumed the Marketplace session inline, and resume was also verified through `/api/sessions/{id}/resume`.

## Resume Flow

- [x] Resume a previously indexed session.
Evidence: Marketplace and Apple sessions were resumed successfully.
- [x] Confirm cached review or cached index summary is restored.
Evidence: resumed sessions restored their cached review/index surfaces instead of returning empty state.
- [x] Confirm the overlay still recommends re-indexing before critical edits.
Evidence: resumed sessions remained index-aware and required `Index Site First` for fresh crawl validation before new guidance.
- [x] Confirm re-indexing a resumed session shows the structure checklist again.
Evidence: resumed Apple session re-index rendered `Structure Checklist Changed · partial refresh ...`.
- [x] Confirm a small site drift triggers partial refresh, not a full reset.
Evidence: resumed Apple indexing used `partial refresh` during re-index.

## Apply Flow

- [x] If executable actions exist, test one safe action only.
Evidence: automated coverage in `tests/test_api_dashboard.py::test_apply_review_batch_executes_only_auto_approved_actions` executes only the low-risk action.
- [x] Confirm risky actions remain confirmation-gated.
Evidence: the same automated coverage leaves the medium-risk click action in `proposed` status.
- [x] Confirm applied actions disappear from the pending executable batch.
Evidence: automated coverage in `tests/test_navigator_core.py::test_record_execution_removes_executed_actions_from_pending_batch` verifies executed actions are removed from `pending_approvals` and `review_batch.actions`.
- [x] Confirm the app does not auto-submit, auto-delete, or take another destructive step without explicit approval.
Evidence: apply safety now auto-runs only low-risk actions, and confirmation-gated actions stay blocked; destructive tokens remain high-risk in the safety policy.

## Stop And Reset

- [x] Confirm `Stop` appears only during actively working states.
Evidence: live idle sessions showed no `Stop`; a live indexing run exposed `Stop`.
- [x] Confirm stopping the session clears the active session UI and returns to setup cleanly.
Evidence: stopping a live Marketplace index returned the overlay to the setup surface.
- [x] Confirm starting a new session after stopping does not inherit stale notes or stale pending actions.
Evidence: the fresh post-stop Marketplace session showed disabled post-index controls and zero inline notes.

## Failure And Edge Cases

- [x] Reload the page and confirm the overlay can recover.
Evidence: repeated page reloads followed by `POST /api/bootstrap-overlay` restored control.
- [x] Switch tabs and come back, then confirm the session remains usable.
Evidence: a temporary Apple tab was opened and closed; returning to the working tab preserved the session list and allowed resume.
- [x] If the site opens a modal or submenu, confirm indexing still proceeds without getting stuck.
Evidence: Marketplace crawl logs still showed hidden submenu targets being skipped without stalling the crawl.
- [x] Confirm the app does not leave the browser stranded on the wrong quarter or summary page after a deep scan.
Evidence: Marketplace indexing returned to the working Quarter 4 page after the crawl.
- [x] Confirm the app does not start duplicate index runs for the same session.
Evidence: automated coverage in `tests/test_api_dashboard.py::test_index_site_rejects_duplicate_active_index` now rejects duplicate starts with `409`.

## Portfolio Demo Readiness

- [x] Confirm the demo uses a real live workflow, not mockups.
Evidence: Marketplace and Apple checks were both performed in the live browser against real sites.
- [x] Confirm the optional hosted backend path is ready:
Evidence: `src/marketplace_bot/cloud/main.py` and `scripts/deploy_cloud_run.sh` define the Cloud Run path.
- [ ] Cloud Run service page or logs can be shown in GCP Console if the hosted backend is part of the demo.
Evidence: direct browser check opened `https://console.cloud.google.com/run` and hit the Google sign-in page; `gcloud` is also not installed here.
- [x] Repo lines showing Google GenAI SDK usage can be shown quickly.
Evidence: `README.md` and `src/marketplace_bot/llm/gemini_provider.py` both point to `google.genai`.
- [x] Architecture diagram is easy to open.
Evidence: `/architecture` loads and automated API coverage still passes.
- [x] README spin-up steps are current.
Evidence: `README.md` documents `make setup`, `make launch`, `make relaunch`, Chrome debug mode, and the optional hosted backend path.

## Bug Log Template

Title: Marketplace coverage accounting undercounted indexed nodes and left the flagship crawl falsely degraded.
Area: Structure manifest and coverage summary.
Severity: Blocker.
Steps to reproduce: Run a live Marketplace Quarter 4 deep index and inspect the final coverage summary.
Expected: Coverage should reflect the completed quarter and section structure and reach a review-ready state when plausible.
Actual: Coverage previously collapsed to `indexed_nodes=0` and `current_node_count=1`.
Screenshot or clip: UNCONFIRMED.
Can reproduce again: No after fix.

Title: Inline live notes overlapped each other in complex mode.
Area: Browser overlay inline-note positioning.
Severity: Medium.
Steps to reproduce: Enter `Live Notes` on a Marketplace page with multiple high-priority notes.
Expected: Notes should stack or offset cleanly.
Actual: Multiple notes rendered at the same coordinates.
Screenshot or clip: UNCONFIRMED.
Can reproduce again: No after fix.

Title: Apply flow auto-approved confirmation-gated actions.
Area: Review batch execution safety.
Severity: High.
Steps to reproduce: Prepare a review batch containing both low-risk and confirmation-gated actions, then run `Apply Review (Beta)`.
Expected: Only low-risk auto-approved actions should execute automatically.
Actual: All pending actions were auto-approved before execution.
Screenshot or clip: UNCONFIRMED.
Can reproduce again: No after fix.

Title: Generic Apple extraction missed compact model labels and buy-page size variants.
Area: Review-only comparison extraction.
Severity: Medium.
Steps to reproduce: Run review-only extraction on Apple compare and buy pages with compact MacBook labels.
Expected: Structured comparison rows should identify visible model variants and the cheapest option.
Actual: Compact labels were missed and buy-page variants collapsed into one entity.
Screenshot or clip: UNCONFIRMED.
Can reproduce again: No after fix.

Title: Goal-specific Apple review could choose the wrong product family after multi-page crawl.
Area: Review-only comparison synthesis.
Severity: High.
Steps to reproduce: Run a review-only Apple session with the goal `Find Mac mini pricing and configuration options on Apple.`, let the crawl explore Apple Mac pages, and inspect the final cheapest summary.
Expected: The comparison should scope to `Mac mini` rows when they are visibly available.
Actual: The review could previously fall back to `MacBook Air` because the comparison layer did not filter entities by the requested family.
Screenshot or clip: UNCONFIRMED.
Can reproduce again: No after fix.

Title: Duplicate index starts were not rejected server-side.
Area: Indexing API contract.
Severity: Medium.
Steps to reproduce: Send another index request for a session already in `indexing`.
Expected: The API should reject the duplicate run.
Actual: The crawl path could start again.
Screenshot or clip: UNCONFIRMED.
Can reproduce again: No after fix.

Title: Right-rail collapse tab overlapped the visible panel body.
Area: Overlay rail geometry.
Severity: Medium.
Steps to reproduce: Open a session with the right rail expanded on a desktop viewport and inspect the collapse tab position.
Expected: The tab should sit outside the rail, connected by a clean seam.
Actual: The tab previously intruded into the panel because the rail used content-box sizing while the position math assumed the rendered box width.
Screenshot or clip: UNCONFIRMED.
Can reproduce again: No after fix.

Title: Cloud Run console proof is unverified in the current environment.
Area: Hackathon proof.
Severity: Medium.
Steps to reproduce: Open `https://console.cloud.google.com/run` from the debug browser profile.
Expected: A signed-in Cloud Run service page or logs view should be available.
Actual: The profile redirected to Google sign-in and `gcloud` is not installed locally.
Screenshot or clip: UNCONFIRMED.
Can reproduce again: Yes.
