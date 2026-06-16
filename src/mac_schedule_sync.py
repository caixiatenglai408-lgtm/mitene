"""後方互換: platform_schedule へ委譲."""

from platform_schedule import (  # noqa: F401
    iter_upcoming_wake_times,
    set_sleep_schedule,
    sync_platform_schedule,
)

sync_mac_schedule = sync_platform_schedule

__all__ = [
    "iter_upcoming_wake_times",
    "set_sleep_schedule",
    "sync_mac_schedule",
    "sync_platform_schedule",
]
