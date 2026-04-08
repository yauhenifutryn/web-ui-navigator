from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from marketplace_bot.bridge import LocalBrowserBridge
from marketplace_bot.config import SETTINGS
from marketplace_bot.logging_json import log_event
from marketplace_bot.navigator_models import ActionProposal, ApprovalRequest, CreateSessionRequest, ExecuteResultPayload
from marketplace_bot.remote_client import RemoteNavigatorClient
from marketplace_bot.state_store import StateStore


CHROME_REMOTE_COMMAND = (
    "/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome "
    "--remote-debugging-port=9222 --no-first-run --no-default-browser-check "
    "--user-data-dir=$(mktemp -d -t 'chrome-remote_data_dir')"
)


async def _get_cdp_version(cdp_url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=5.0) as client:
        return await client.get(f"{cdp_url.rstrip('/')}/json/version")


async def check_cdp_available(cdp_url: str) -> tuple[bool, str]:
    try:
        response = await _get_cdp_version(cdp_url)
        if response.status_code == 200:
            data = response.json()
            browser = data.get("Browser", "unknown")
            return True, f"Chrome CDP available at {cdp_url}. Browser: {browser}"
    except Exception:
        pass
    return (
        False,
        "Chrome CDP is not reachable at "
        f"{cdp_url}. Launch Chrome with:\n{CHROME_REMOTE_COMMAND}",
    )


def _extract_session_id(payload: dict[str, Any]) -> str:
    session_id = str(payload.get("session_id", "")).strip()
    if not session_id:
        raise RuntimeError("Cloud backend did not return a session_id")
    return session_id


async def _sync_overlay(bridge: Any, panel: dict[str, Any]) -> None:
    if hasattr(bridge, "sync_agent_overlay"):
        await bridge.sync_agent_overlay(panel)


async def prompt_for_approvals(actions: list[dict[str, Any]]) -> list[str]:
    approved: list[str] = []
    for action in actions:
        action_id = str(action.get("action_id", ""))
        label = action.get("target_text") or action.get("value") or action.get("url") or action.get("action")
        reasoning = action.get("reasoning", "")
        answer = input(f"Approve sensitive action {action_id} on '{label}'? {reasoning} [y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            approved.append(action_id)
    return approved


async def run_connect_loop(
    bridge: LocalBrowserBridge,
    remote_client: RemoteNavigatorClient,
    goal: str,
    project_name: str,
    domain_hint: str | None,
    safety_mode: str,
    poll_interval: float,
    max_loops: int | None = None,
    session_id: str | None = None,
    approval_callback: Callable[[list[dict[str, Any]]], list[str] | Awaitable[list[str]]] | None = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    session_payload = (
        {"session_id": session_id}
        if session_id
        else await remote_client.create_session(
            CreateSessionRequest(
                goal=goal,
                project_name=project_name,
                domain_hint=domain_hint,
                safety_mode=safety_mode,
            )
        )
    )
    active_session_id = session_id or _extract_session_id(session_payload)
    loops = 0
    last_result: dict[str, Any] = {"session_id": active_session_id}

    while True:
        await _sync_overlay(
            bridge,
            {
                "title": project_name,
                "stage": "indexing",
                "status": "Reading the current screen and refreshing context.",
                "goal": goal,
                "strategic_summary": session_payload.get("strategic_summary", ""),
                "live_advice": ["Collecting grounded context from the active tab."],
                "actions": [],
            },
        )
        observation = await bridge.capture_observation(
            session_id=active_session_id,
            active_goal=goal,
            domain_pack=domain_hint or "generic_web",
            safety_mode=safety_mode,
            prior_actions=[],
        )
        await remote_client.observe(observation)
        plan_payload = await remote_client.plan(active_session_id)
        actions = list(plan_payload.get("actions", []))
        await _sync_overlay(
            bridge,
            {
                "title": project_name,
                "stage": "planning",
                "status": "Planning the next grounded steps.",
                "goal": goal,
                "strategic_summary": plan_payload.get("strategic_summary", session_payload.get("strategic_summary", "")),
                "live_advice": plan_payload.get("live_advice", []),
                "actions": actions,
            },
        )

        auto_ids = [str(item.get("action_id")) for item in actions if not item.get("requires_confirmation")]
        if auto_ids:
            await remote_client.approve(active_session_id, ApprovalRequest(action_ids=auto_ids))

        sensitive_actions = [item for item in actions if item.get("requires_confirmation")]
        if sensitive_actions:
            selected_ids: list[str] = []
            if approval_callback is not None:
                result = approval_callback(sensitive_actions)
                selected_ids = await result if asyncio.iscoroutine(result) else result
            else:
                selected_ids = await prompt_for_approvals(sensitive_actions)
            if selected_ids:
                await remote_client.approve(active_session_id, ApprovalRequest(action_ids=selected_ids))

        session_payload = await remote_client.get_session(active_session_id)
        pending = session_payload.get("pending_approvals", [])
        approved_actions = [
            ActionProposal.model_validate(item)
            for item in pending
            if item.get("status") == "approved"
        ]

        if approved_actions:
            await _sync_overlay(
                bridge,
                {
                    "title": project_name,
                    "stage": "executing",
                    "status": "Executing approved browser actions locally.",
                    "goal": goal,
                    "strategic_summary": session_payload.get("strategic_summary", ""),
                    "live_advice": plan_payload.get("live_advice", []),
                    "actions": [item.model_dump(mode="json") for item in approved_actions],
                },
            )
            results = await bridge.execute_actions(approved_actions)
            last_result = await remote_client.execute_result(
                ExecuteResultPayload(session_id=active_session_id, results=results)
            )
            if any(action.action == "stop" for action in approved_actions):
                break
        else:
            last_result = session_payload

        loops += 1
        await _sync_overlay(
            bridge,
            {
                "title": project_name,
                "stage": "ready",
                "status": "Agent is ready for the next step.",
                "goal": goal,
                "strategic_summary": session_payload.get("strategic_summary", ""),
                "live_advice": plan_payload.get("live_advice", []) if "plan_payload" in locals() else [],
                "actions": [],
            },
        )
        if max_loops is not None and loops >= max_loops:
            break
        await sleep_fn(poll_interval)

    return last_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Connect a local Chrome browser to the cloud navigator backend.")
    parser.add_argument("--goal", required=True, help="Natural-language objective for the navigator.")
    parser.add_argument("--project-name", default="Live Navigator Demo", help="Project/session display name.")
    parser.add_argument("--domain", default="generic_web", help="Domain hint, for example generic_web or marketplace_simulation.")
    parser.add_argument("--safety-mode", default="confirm_before_act", choices=["guided", "confirm_before_act", "autonomous"])
    parser.add_argument("--session-id", default="", help="Reuse an existing cloud session ID.")
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval between steps.")
    parser.add_argument("--max-loops", type=int, default=1, help="Maximum loop iterations before exit.")
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    base_url = os.getenv("NAVIGATOR_CLOUD_BACKEND_URL", "").strip()
    if not base_url:
        print("NAVIGATOR_CLOUD_BACKEND_URL is required.", file=sys.stderr)
        return 1

    client = RemoteNavigatorClient(base_url)
    try:
        health = await client.health()
    except Exception as exc:
        print(f"Cloud backend health check failed: {exc}", file=sys.stderr)
        return 1
    if not health.get("ok"):
        print(f"Cloud backend did not report ok: {health}", file=sys.stderr)
        return 1

    cdp_ok, cdp_message = await check_cdp_available(SETTINGS.cdp_url)
    if not cdp_ok:
        print(cdp_message, file=sys.stderr)
        return 1
    print(cdp_message)

    store = StateStore(SETTINGS.runtime_dir)
    store.bootstrap()
    bridge = LocalBrowserBridge(
        state_store=store,
        cdp_url=SETTINGS.cdp_url,
        target_domain=SETTINGS.target_domain,
    )

    try:
        result = await run_connect_loop(
            bridge=bridge,
            remote_client=client,
            goal=args.goal,
            project_name=args.project_name,
            domain_hint=args.domain,
            safety_mode=args.safety_mode,
            poll_interval=args.interval,
            max_loops=args.max_loops,
            session_id=args.session_id or None,
        )
    except KeyboardInterrupt:
        print("Connect loop stopped by user.")
        return 130

    log_event("cli", "connect_loop_finished", session_id=result.get("session_id", ""))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
