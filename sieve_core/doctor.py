"""Setup Doctor: every missing prerequisite has an exact fix.

Checks are cheap and read-only. The walk doubles as demo footage.
"""

from . import escalate


def run_checks(dispatch, conn) -> list:
    """Returns [{name, status: 'ok'|'warn'|'fail', detail, fix}]."""
    checks = []

    # 1. Delivery reachable (the engine's send CLI; the name stays out of the text)
    import shutil
    if shutil.which("hermes"):
        checks.append({"name": "message delivery", "status": "ok",
                       "detail": "send command available", "fix": ""})
    else:
        checks.append({"name": "message delivery", "status": "fail",
                       "detail": "the assistant's send command is not on PATH",
                       "fix": "restart the assistant; if this persists, contact support"})

    # 2. Device toolset + registration
    status = escalate.device_status(dispatch)
    if not status["available"]:
        checks.append({"name": "mobile_device toolset", "status": "fail",
                       "detail": status["reason"],
                       "fix": "turn on the mobile device toolset for the assistant, then restart it"})
    elif not status["devices"]:
        checks.append({"name": "mobile_device toolset", "status": "warn",
                       "detail": "tools loaded but no device paired to this profile",
                       "fix": "open the TeamShift app and sign in to this profile"})
    else:
        checks.append({"name": "mobile_device toolset", "status": "ok",
                       "detail": f"{len(status['devices'])} device(s)", "fix": ""})

    # 3. Push freshness (gates T4/T5)
    ready, reason = escalate.push_ready(dispatch)
    checks.append({"name": "push (T4/T5 gate)", "status": "ok" if ready else "warn",
                   "detail": reason,
                   "fix": "" if ready else escalate.push_fix(reason)})

    # 4. Delivery target configured
    from . import queue as q
    target = q.kv_get(conn, "target")
    checks.append({"name": "delivery target", "status": "ok" if target else "warn",
                   "detail": target or "not set (will use the home channel)",
                   "fix": "" if target else "/sieve target <platform[:chat_id]>"})

    # 5. Out-of-band watchdog (catches a link drop while Desktop is closed).
    # macOS launchd only, and only when the opt-in installer ships beside the
    # plugin. Elsewhere (a Linux host, or a build with scripts/ stripped) the
    # check is skipped: the plugin never installs or recommends an OS service there.
    if watchdog_available():
        checks.append(_watchdog_check())

    return checks


def watchdog_available() -> bool:
    """True only on macOS with the opt-in installer present beside the plugin."""
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "scripts" / "install-watchdog.sh"
    return sys.platform == "darwin" and script.exists()


def _watchdog_check() -> dict:
    import os
    import subprocess
    plist = os.path.expanduser("~/Library/LaunchAgents/com.vgfreak.sieve-watch.plist")
    loaded = False
    try:
        subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.vgfreak.sieve-watch"],
                       capture_output=True, timeout=5, check=True)
        loaded = True
    except Exception:
        loaded = False
    return {
        "name": "link watchdog (catches a drop while Desktop is closed)",
        "status": "ok" if loaded else "warn",
        "detail": "loaded" if loaded else (
            "not loaded" if os.path.exists(plist) else "plist not installed"),
        "fix": "" if loaded else (
            "optional: run once in a terminal from the plugin dir: bash scripts/install-watchdog.sh, "
            "then launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vgfreak.sieve-watch.plist "
            "(the agent cannot bootstrap). It cannot see an outage while the Mac is asleep; "
            "after a sleep the link state for that gap is unknown."),
    }


def format_checks(checks) -> str:
    icon = {"ok": "✓", "warn": "⚠", "fail": "✗"}
    lines = ["SIEVE Setup Doctor:"]
    for c in checks:
        lines.append(f"{icon.get(c['status'], '?')} {c['name']}: {c['detail']}")
        if c["fix"]:
            lines.append(f"    fix: {c['fix']}")
    return "\n".join(lines)
