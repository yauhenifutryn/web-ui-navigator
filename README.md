# Live Navigator Companion

Live Navigator Companion is a research project in **deep indexing and stateful browser interaction**. Instead of treating each page as a fresh observation, it builds a persistent structural index of a complex web workspace, reuses that memory across sessions, and grounds live guidance against the current page plus the known workspace graph.

## What Makes This Different

Most browser agents work page-by-page. This project explores a structure-first path:

- **Deep indexing first**: Before giving guidance, the system builds a reusable structure checklist of the workspace, including navigation nodes, workflow areas, and editable regions.
- **Persistent local memory**: On later visits, it reuses saved site memory and only refreshes the parts that appear to have drifted.
- **Visual grounding**: Screenshots are the primary signal. Visible UI text and lightweight browser metadata are secondary. The system does not rely on raw DOM parsing.
- **Overlay-first interaction**: Controls live inside the real website as an injected overlay, not in a separate dashboard or browser extension popup.
- **Review-before-action**: The system generates grouped, page-anchored review items before any execution. Sensitive actions remain confirmation-gated.

The proof case is Marketplace Simulation, a quarter-based business simulation chosen because it stresses assumptions that commodity browser agents usually make: nested navigation, editable tables, prior-period context, and repeated revisits to the same workspace.

## How It Works

1. Opens a persistent overlay on top of the real website.
2. Runs a visible index pass that builds a reusable structure checklist.
3. Compares the current structure fingerprint against saved local site memory.
4. Chooses to reuse, partially refresh, or rebuild the site map based on structural drift.
5. Produces a grouped review with page-anchored notes.
6. Offers two modes after review:
   - **Live Notes**: dynamic, event-driven notes near the current editable area.
   - **Apply Review (Beta)**: batch execution with confirmation gating.

## Scan Profiles

- **Quick Scan**: current page plus nearby visible navigation.
- **Smart Scan** (default): starts small, escalates if complexity warrants it.
- **Deep Scan**: builds deeper reusable workspace memory for dense, recurring systems.

## Workspace Presets

- **General Web**: simpler sites and short workflows.
- **Complex Workspace**: nested, recurring, data-heavy systems such as legacy business apps, internal tools, and simulation workspaces.

## Architecture

The operator stack is tuned for dense interfaces:

- **Visual grounding**: screenshot, visible UI, lightweight browser metadata.
- **Workspace memory**: deep current-workspace index + compact prior-context memory.
- **Decision layer**: page-specific review, live notes, domain-pack optimizations.
- **Safe execution**: local browser control, confirmation-gated actions, structured action validation.
- **Optional hosted backend**: Gemini/Vertex-backed planning, Firestore session memory, Cloud Storage artifacts.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full architecture diagrams and component details.

## Requirements

- Python 3.11+
- Google Chrome installed on macOS
- Chrome debug mode enabled on port `9222`
- Gemini API key for local planning, or Google Cloud credentials for the optional hosted backend
- Playwright installed in the local environment

## Local Setup

```bash
make setup
```

## Launch

Chrome debug mode is required. After cloning:

```bash
make launch
```

This creates `.venv` if needed, installs the project in editable mode, launches a dedicated Chrome window with `--remote-debugging-port=9222`, starts the local server on `http://127.0.0.1:8002`, and opens the bootstrap page.

Then move to the real website tab — the overlay and runtime controls live there.

Deep indexing artifacts and reusable structure memory are stored under `runtime/site_memory`.

Other make targets:
- `make relaunch` — clean restart that replaces a stale local server.
- `make reset-cache` — clean local reset before a fresh demo.

## Environment Variables

### Local Gemini mode

```bash
export GEMINI_API_KEY="your_gemini_api_key"
export GEMINI_MODEL="gemini-3-flash"
export GEMINI_INDEX_MODEL="gemini-3-flash"
export GEMINI_LIVE_MODEL="gemini-3.1-flash-lite"
export MARKETPLACE_TARGET_DOMAIN="play.marketplace-simulation.com"
```

- `GEMINI_INDEX_MODEL` — used for strategic indexing.
- `GEMINI_LIVE_MODEL` — used for fast current-page advice.
- `MARKETPLACE_TARGET_DOMAIN` — optional proof-case override. Leave it unset for generic browsing and the bridge will attach to the active non-local tab instead.
- Uses the official Google GenAI SDK via `google.genai`.

### Optional hosted backend

```bash
export GOOGLE_CLOUD_PROJECT="your-gcp-project-id"
export GOOGLE_CLOUD_LOCATION="us-central1"
export NAVIGATOR_GCS_BUCKET="your-artifact-bucket"
export NAVIGATOR_CLOUD_BACKEND_URL="https://your-cloud-run-service-url"
export NAVIGATOR_USE_CLOUD_BACKEND="1"
```

When enabled, the browser stays local while planning and persistence can route through Cloud Run.

## Manual Chrome Launch

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --no-first-run --no-default-browser-check --user-data-dir=$(mktemp -d -t 'chrome-remote_data_dir')
```

## Testing

```bash
make setup
python -m pytest
```

## Repo Structure

- `src/marketplace_bot/api/main.py` — local API, bootstrap, overlay command routing.
- `src/marketplace_bot/bridge.py` — persistent browser controller, overlay injection, screenshot capture.
- `src/marketplace_bot/companion.py` — session state machine, index summary, review batch flow.
- `src/marketplace_bot/site_intelligence.py` — structure fingerprinting, durable site memory, incremental refresh.
- `src/marketplace_bot/planner.py` — Gemini-powered review generation, live notes, Apple proof-case extraction, and fallback heuristics.
- `src/marketplace_bot/domain_packs.py` — `generic_web` and `marketplace_simulation` domain packs.
- `src/marketplace_bot/cloud/main.py` — Cloud Run backend entrypoint (optional).
