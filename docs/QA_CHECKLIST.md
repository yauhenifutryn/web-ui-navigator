# Live Navigator QA Checklist

Use this as a manual pass before the final demo and again after every fix.

## Test Setup

- [ ] Start from a clean local state with `make reset-cache` if you want a fresh session baseline.
- [ ] Launch the app with `make launch`.
- [ ] Confirm the dedicated Chrome debug window opens.
- [ ] Confirm the local server is reachable at `http://127.0.0.1:8002`.
- [ ] Open the real target site in the dedicated Chrome debug window, not in your normal browser profile.

## Bootstrap And Recovery

- [ ] Confirm the bootstrap overlay initializes on the target tab.
- [ ] If no target tab is available, confirm the app shows a recovery message instead of crashing.
- [ ] Confirm the bootstrap page is only a setup and recovery surface, not the main workflow UI.

## Session Creation

- [ ] Start a new session from the overlay.
- [ ] Confirm `Project`, `Goal`, `Workspace Type`, and `How Thorough` are visible.
- [ ] On Marketplace, confirm the defaults are `Complex Workspace` and `Deep Scan`.
- [ ] Confirm the primary CTA is visible without awkward scrolling on desktop.
- [ ] Confirm `Live Notes` and `Refresh Review` are disabled before indexing.

## Indexing Flow

- [ ] Click `Index Site First`.
- [ ] Confirm the overlay enters an active indexing state.
- [ ] Confirm the progress bar moves and the status text changes during indexing.
- [ ] Confirm the new `Structure Checklist` card appears during or immediately after the fingerprint check.
- [ ] Confirm the card shows `Reused`, `Changed`, `New`, `Removed`, and `Visible` counts.
- [ ] Confirm the card also shows the selected refresh strategy, for example `partial refresh` or `full refresh`.
- [ ] Confirm the browser returns to the original working page after indexing completes.

## Post-Index State

- [ ] Confirm indexing ends in a review-ready state, not a dead-end state.
- [ ] Confirm `Live Notes` becomes enabled after indexing.
- [ ] Confirm `Refresh Review` becomes enabled after indexing.
- [ ] Confirm `Index Summary` is populated with a meaningful summary, not empty placeholders.
- [ ] Confirm `Detected Changes` matches the structure-checklist story.

## Live Notes

- [ ] Click `Live Notes`.
- [ ] Confirm the overlay enters live-advice mode without errors.
- [ ] Change to another relevant page and confirm notes refresh only when the page actually changes.
- [ ] Confirm notes are page-specific, not generic filler.
- [ ] Confirm inline notes anchor near relevant visible controls when possible.

## Review Batch

- [ ] Click `Refresh Review`.
- [ ] Confirm a review summary appears.
- [ ] Confirm the recommendations match the current page and current quarter.
- [ ] Confirm the rationale is understandable and grounded in visible UI state.
- [ ] Confirm `Apply Review (Beta)` is disabled when there is nothing safe to apply.
- [ ] Confirm `Apply Review (Beta)` becomes enabled only when executable actions exist.

## Logs And Saved Sessions

- [ ] Click `Saved Sessions` and confirm the panel opens inline.
- [ ] Click `Saved Sessions` again and confirm it closes.
- [ ] Click `Logs` and confirm the panel opens inline.
- [ ] Click `Logs` again and confirm it closes.
- [ ] Confirm the current session can be resumed from the saved sessions list.

## Resume Flow

- [ ] Resume a previously indexed session.
- [ ] Confirm cached review or cached index summary is restored.
- [ ] Confirm the overlay still recommends re-indexing before critical edits.
- [ ] Confirm re-indexing a resumed session shows the structure checklist again.
- [ ] Confirm a small site drift triggers partial refresh, not a full reset.

## Apply Flow

- [ ] If executable actions exist, test one safe action only.
- [ ] Confirm risky actions remain confirmation-gated.
- [ ] Confirm applied actions disappear from the pending executable batch.
- [ ] Confirm the app does not auto-submit, auto-delete, or take another destructive step without explicit approval.

## Stop And Reset

- [ ] Confirm `Stop` appears only during actively working states.
- [ ] Confirm stopping the session clears the active session UI and returns to setup cleanly.
- [ ] Confirm starting a new session after stopping does not inherit stale notes or stale pending actions.

## Failure And Edge Cases

- [ ] Reload the page and confirm the overlay can recover.
- [ ] Switch tabs and come back, then confirm the session remains usable.
- [ ] If the site opens a modal or submenu, confirm indexing still proceeds without getting stuck.
- [ ] Confirm the app does not leave the browser stranded on the wrong quarter or summary page after a deep scan.
- [ ] Confirm the app does not start duplicate index runs for the same session.

## Portfolio Demo Readiness

- [ ] Confirm the demo uses a real live workflow, not mockups.
- [ ] Confirm the optional hosted backend path is ready if you plan to demo it.
- [ ] Cloud Run service page or logs can be shown in GCP Console if the hosted backend is part of the demo.
- [ ] Repo lines showing Google GenAI SDK usage can be shown quickly.
- [ ] Architecture diagram is easy to open.
- [ ] README spin-up steps are current.

## Bug Log Template

Copy this for each issue:

```text
Title:
Area:
Severity:
Steps to reproduce:
Expected:
Actual:
Screenshot or clip:
Can reproduce again:
```
