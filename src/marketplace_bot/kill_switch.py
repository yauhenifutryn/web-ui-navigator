KILL_SWITCH_ACTIVE = False
CONSECUTIVE_EXECUTOR_FAILURES = 0


def activate_kill_switch(reason: str = "manual") -> None:
    global KILL_SWITCH_ACTIVE
    KILL_SWITCH_ACTIVE = True


def reset_kill_switch() -> None:
    global KILL_SWITCH_ACTIVE, CONSECUTIVE_EXECUTOR_FAILURES
    KILL_SWITCH_ACTIVE = False
    CONSECUTIVE_EXECUTOR_FAILURES = 0


def record_executor_success() -> None:
    global CONSECUTIVE_EXECUTOR_FAILURES
    CONSECUTIVE_EXECUTOR_FAILURES = 0


def record_executor_failure() -> None:
    global CONSECUTIVE_EXECUTOR_FAILURES, KILL_SWITCH_ACTIVE
    CONSECUTIVE_EXECUTOR_FAILURES += 1
    if CONSECUTIVE_EXECUTOR_FAILURES >= 3:
        KILL_SWITCH_ACTIVE = True
