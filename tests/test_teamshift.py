"""TeamShift carry fixes: phone arm off by default, owner-only /sieve, neutral text, no OS service.

Engine-dependent checks skip when the engine is not importable.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from sieve_core import doctor, matrix, queue, setup

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sieve_plugin_teamshift", ROOT / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

RUNTIME_PY = [ROOT / "__init__.py", *sorted((ROOT / "sieve_core").glob("*.py"))]
# The capitalized product names anywhere; the lowercase binary only where a phrase shows it to a person
# (the send subprocess argv ``["hermes", "send"]`` is not text anyone reads).
ENGINE_NAME = re.compile(r"\bHermes\b|\bNous\b|hermes-agent|(?i:\bhermes (send|config|app|cli)\b)")


def _fresh(monkeypatch, tmp_path):
    conn = sqlite3.connect(str(tmp_path / "ts.db"))
    queue.init_schema(conn)
    monkeypatch.setattr(queue, "db", lambda: conn)
    monkeypatch.setattr(plugin.delivery, "send", lambda *a, **k: True)
    monkeypatch.setattr(plugin, "_CTX", None)
    return conn


class _Ctx:
    def __init__(self, owner_ids=None, boom=False):
        self._owner_ids, self._boom = owner_ids, boom

    def get_config(self, key, default=None):
        if self._boom:
            raise RuntimeError("config unreadable")
        return self._owner_ids if key == "owner_ids" and self._owner_ids is not None else default


def _chat(platform="telegram", user_id="111", **extra):
    return SimpleNamespace(platform=SimpleNamespace(value=platform), user_id=user_id,
                           user_id_alt=extra.pop("user_id_alt", None), is_bot=extra.pop("is_bot", False),
                           chat_type=extra.pop("chat_type", "dm"), **extra)


# -- 1. the phone arm is off unless the owner turns it on --------------------------------------
def test_phone_arm_defaults_off_and_only_yes_arms_it(monkeypatch, tmp_path):
    conn = _fresh(monkeypatch, tmp_path)
    assert plugin._phone_wanted(conn) is False
    for odd in ("", "maybe", "YES ", "both", "1"):
        queue.kv_set(conn, "phone_arm", odd)
        assert plugin._phone_wanted(conn) is False, odd
    queue.kv_set(conn, "phone_arm", "yes")
    assert plugin._phone_wanted(conn) is True
    queue.kv_set(conn, "phone_arm", "")
    assert plugin._run_command("arm").startswith("phone arm: no")


def test_top_tier_never_touches_the_phone_by_default(monkeypatch, tmp_path):
    conn = _fresh(monkeypatch, tmp_path)
    queue.kv_set(conn, "target", "telegram")
    touched = []
    monkeypatch.setattr(plugin.escalate, "push_ready", lambda _d: touched.append("ready") or (True, "fresh"))
    monkeypatch.setattr(plugin.escalate, "fire_alarm", lambda *a, **k: touched.append("alarm") or {"ok": True})
    monkeypatch.setattr(plugin.escalate, "fire_timer", lambda *a, **k: touched.append("timer") or {"ok": True})
    for tier in (4, 5):
        out = plugin._deliver(conn, tier, "service_down", {}, matrix.Decision(tier, "t", "down", "user"))
        assert out["arm"] == "desktop" and out["escalated"] is False
    assert plugin._run_command("test T5").startswith("phone arm is off")
    assert plugin._run_command("test T4").startswith("phone arm is off")
    assert touched == []


def test_explicit_arm_yes_reaches_the_phone(monkeypatch, tmp_path):
    conn = _fresh(monkeypatch, tmp_path)
    queue.kv_set(conn, "target", "telegram")
    monkeypatch.setattr(plugin.escalate, "push_ready", lambda _d: (True, "fresh"))
    monkeypatch.setattr(plugin.escalate, "fire_timer", lambda *a, **k: {"ok": True})
    assert "phone arm on" in plugin._run_command("arm yes")
    out = plugin._deliver(conn, 1, "needs_human", {}, matrix.Decision(4, "t", "gate", "user"))
    assert out["arm"] == "timer"


# -- 2. /sieve is owner-only and fails closed ---------------------------------------------------
@pytest.mark.parametrize("surface", ["cli", "tui", "desktop", "local", "CLI"])
def test_local_owner_surfaces_may_redirect(monkeypatch, tmp_path, surface):
    conn = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_session", lambda name: "")
    assert "delivery target set: discord:#ops" in plugin._cmd("target discord:#ops", platform=surface)
    assert queue.kv_get(conn, "target") == "discord:#ops"


def test_gateway_local_session_is_the_owner(monkeypatch, tmp_path):
    conn = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_session", lambda name: "")
    src = SimpleNamespace(platform=SimpleNamespace(value="local"), user_id=None, is_bot=False)
    assert "T5 chain set" in plugin._cmd("target t5 telegram", source=src, platform="local")
    assert queue.kv_get(conn, "target:T5") == "telegram"


def test_listed_chat_sender_may_redirect_and_import(monkeypatch, tmp_path):
    conn = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_CTX", _Ctx(["telegram:111", "discord:999"]))
    monkeypatch.setattr(plugin, "_session", lambda name: "111" if name == "HERMES_SESSION_ACTOR_ID" else "")
    monkeypatch.setattr(plugin, "_policy_dir", lambda: tmp_path / "pol")
    src = _chat("telegram", "111")
    assert "delivery target set" in plugin._cmd("target slack:C1", source=src, platform="telegram")
    assert plugin._cmd("export", source=src, platform="telegram").startswith("policy exported")
    assert plugin._cmd("import", source=src, platform="telegram").startswith("policy merged")
    assert queue.kv_get(conn, "target") == "slack:C1"
    # a comma-string setting and the platform's alternate id also prove the owner
    monkeypatch.setattr(plugin, "_CTX", _Ctx("signal:uuid-1"))
    monkeypatch.setattr(plugin, "_session", lambda name: "")
    assert plugin._caller_is_owner(_chat("signal", "+1555", user_id_alt="uuid-1"), "signal")


REFUSED = [
    ("no surface at all", None, ""),
    ("unknown surface", None, "webhook"),
    ("chat platform without a source", None, "telegram"),
    ("api server", None, "api_server"),
    ("unlisted chat sender", _chat("telegram", "222"), "telegram"),
    ("listed id on another platform", _chat("discord", "111"), "discord"),
    ("bot sender", _chat("telegram", "111", is_bot=True), "telegram"),
    ("no sender id", _chat("telegram", None), "telegram"),
    ("empty platform", _chat("", "111"), ""),
]


@pytest.mark.parametrize("why,source,surface", REFUSED, ids=[r[0] for r in REFUSED])
def test_other_callers_cannot_redirect_or_import(monkeypatch, tmp_path, why, source, surface):
    conn = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_CTX", _Ctx(["telegram:111"]))
    monkeypatch.setattr(plugin, "_session", lambda name: "")
    monkeypatch.setattr(plugin, "_policy_dir", lambda: tmp_path / "pol")
    (tmp_path / "pol").mkdir()
    (tmp_path / "pol" / "policy.json").write_text('{"kv": {"target": "attacker:1"}}')
    for sub in ("target attacker:1", "target t5 attacker:1", "import", "import policy.json replace",
                "target", "arm yes", "setup apply digest=attacker:1", "status"):
        assert plugin._cmd(sub, source=source, platform=surface) == plugin.OWNER_REFUSAL, (why, sub)
    assert queue.kv_get(conn, "target") is None
    assert queue.kv_get(conn, "target:T5") is None
    assert queue.kv_get(conn, "phone_arm") is None


def test_refused_when_owner_cannot_be_proven(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    src = _chat("telegram", "111")
    monkeypatch.setattr(plugin, "_session", lambda name: "")
    # no plugin context, no setting, an unreadable setting, a malformed setting
    for ctx in (None, _Ctx(None), _Ctx(boom=True), _Ctx({"telegram": "111"}), _Ctx(["111"])):
        monkeypatch.setattr(plugin, "_CTX", ctx)
        assert plugin._cmd("target x", source=src, platform="telegram") == plugin.OWNER_REFUSAL
    # another sender's turn: the bound actor is not the command's sender
    monkeypatch.setattr(plugin, "_CTX", _Ctx(["telegram:111"]))
    monkeypatch.setattr(plugin, "_session", lambda name: "222" if name == "HERMES_SESSION_ACTOR_ID" else "")
    assert plugin._cmd("target x", source=src, platform="telegram") == plugin.OWNER_REFUSAL
    # a "cli" claim while the bound session is a chat platform
    monkeypatch.setattr(plugin, "_session", lambda name: "telegram" if name == "HERMES_SESSION_PLATFORM" else "")
    assert plugin._cmd("target x", platform="cli") == plugin.OWNER_REFUSAL


def test_engine_dispatch_passes_the_caller_to_the_handler(monkeypatch, tmp_path):
    dispatch = pytest.importorskip("hermes_cli.plugin_command_dispatch")
    gw = pytest.importorskip("gateway.session")
    conn = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_CTX", _Ctx(["telegram:111"]))
    monkeypatch.setattr(plugin, "_session", lambda name: "")
    stranger = gw.SessionSource(platform=gw.Platform.TELEGRAM, chat_id="g1", chat_type="group", user_id="222")
    owner = gw.SessionSource(platform=gw.Platform.TELEGRAM, chat_id="d1", user_id="111")
    ctx = dict(session_id="s", session_key="k")
    assert dispatch.invoke_plugin_command(plugin._cmd, "target evil", source=stranger, platform="telegram",
                                          **ctx) == plugin.OWNER_REFUSAL
    assert "set" in dispatch.invoke_plugin_command(plugin._cmd, "target good", source=owner, platform="telegram", **ctx)
    assert "set" in dispatch.invoke_plugin_command(plugin._cmd, "target t4 cli", source=None, platform="cli", **ctx)
    assert queue.kv_get(conn, "target") == "good" and queue.kv_get(conn, "target:T4") == "cli"


# -- 3. no user-facing string names the engine --------------------------------------------------
def _user_strings(path: Path) -> list[str]:
    """String literals in a module, minus docstrings (developer text a person never sees)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


@pytest.mark.parametrize("path", RUNTIME_PY, ids=lambda p: p.name)
def test_no_engine_name_in_runtime_text(path):
    bad = [s for s in _user_strings(path) if ENGINE_NAME.search(s)]
    assert not bad, bad


def test_no_engine_name_in_manifest_or_command_text(monkeypatch, tmp_path):
    description = next(line for line in (ROOT / "plugin.yaml").read_text().splitlines()
                       if line.startswith("description:"))
    assert not ENGINE_NAME.search(description)
    _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin.escalate, "device_status", lambda _d: {"available": True, "devices": []})
    shown = [setup.show(), setup.question("done"), plugin._run_command("doctor"), plugin._run_command("help"),
             plugin.OWNER_REFUSAL, *[plugin.escalate.push_fix(r) for r in
                                     ("toolset", "no device", "stale", "push is not configured", "x")]]
    assert not [s for s in shown if ENGINE_NAME.search(s)]


# -- 4. registration: one tool, six hooks, one command; no job, no OS service ------------------
def test_register_surface_and_no_service(monkeypatch, tmp_path):
    seen = {"tool": [], "hook": [], "command": [], "other": []}

    class Ctx(_Ctx):
        def register_tool(self, name, toolset, **kw):
            seen["tool"].append((name, toolset))

        def register_hook(self, name, fn, **kw):
            seen["hook"].append(name)

        def register_command(self, name, fn, **kw):
            seen["command"].append(name)

        def __getattr__(self, name):
            seen["other"].append(name)
            return lambda *a, **k: None

    monkeypatch.setattr(plugin, "_CTX", None)
    plugin.register(Ctx())
    monkeypatch.setattr(plugin, "_CTX", None)
    assert seen["tool"] == [("sieve_ping", "sieve")]
    assert sorted(seen["hook"]) == sorted(["on_human_input_request", "on_human_input_resolved",
                                           "kanban_task_blocked", "subagent_stop", "on_session_end",
                                           "post_approval_response"])
    assert seen["command"] == ["sieve"] and seen["other"] == []
    # the launchd watchdog is a manual macOS opt-in; nothing offers it elsewhere
    monkeypatch.setattr(doctor, "watchdog_available", lambda: False)
    conn = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin.escalate, "device_status", lambda _d: {"available": False, "reason": "x"})
    text = "\n".join([setup.show(), setup.question("done"), doctor.format_checks(doctor.run_checks(None, conn))])
    assert "launchctl" not in text and "install-watchdog" not in text


# -- 5. no default delivery target: record only until the owner sets one ------------------------
def test_no_target_records_only_and_sends_nothing(monkeypatch, tmp_path):
    conn = _fresh(monkeypatch, tmp_path)
    sent, phone = [], []
    monkeypatch.setattr(plugin.delivery, "send", lambda text, target=None, timeout=30: sent.append(target) or True)
    monkeypatch.setattr(plugin.escalate, "push_ready", lambda _d: phone.append("ready") or (True, "fresh"))
    queue.kv_set(conn, "phone_arm", "yes")  # even an armed phone stays silent without a target
    queue.kv_set(conn, "promo_minutes", "1")
    assert plugin._targets_for(conn, 4) == [] and plugin._digest_targets(conn) == []
    for cls in ("needs_human", "service_down", "task_failed", "agent_message"):
        out = plugin._route(cls, {"summary": "x"})
        assert out.get("recorded") is True and out["delivered"] is False, cls
    held = queue.log_event(conn, "task_done", {}, 0, "held")
    queue.add_hold(conn, held)
    assert plugin._maybe_digest(conn) == ""
    assert plugin._run_command("flush").startswith("flush skipped: 1 held")
    assert "target: none" in plugin._run_command("target")
    assert sent == [] and phone == []
    assert queue.kv_get(conn, "desktop_notice") is None
    actions = [row[4] for row in queue.recent_events(conn, 10)]
    assert actions.count("recorded") == 4
    # delivery starts once the owner sets a target
    plugin._run_command("target discord:#ops")
    out = plugin._route("task_failed", {"summary": "y"})
    assert out["delivered"] is True and sent == ["discord:#ops"]


# -- 6. hook alert text carries identifiers, never the raw payload -------------------------------
SECRET = "sk-live-PAYLOAD-should-not-leak"


def _hook_texts(monkeypatch, tmp_path):
    conn = _fresh(monkeypatch, tmp_path)
    queue.kv_set(conn, "target", "telegram")
    texts = []
    monkeypatch.setattr(plugin.delivery, "send", lambda text, target=None, timeout=30: texts.append(text) or True)
    hooks = {}

    class Ctx(_Ctx):
        def register_tool(self, *a, **k):
            pass

        def register_hook(self, name, fn, **k):
            hooks[name] = fn

        def register_command(self, *a, **k):
            pass

    plugin.register(Ctx())
    monkeypatch.setattr(plugin, "_CTX", None)
    return conn, texts, hooks


def test_kanban_blocked_alert_uses_identifiers_only(monkeypatch, tmp_path):
    conn, texts, hooks = _hook_texts(monkeypatch, tmp_path)
    hooks["kanban_task_blocked"](task_id="t_42", profile_name="default", board="ops", assignee="dana",
                                 run_id=7, reason="needs the\nlogin code", body=SECRET, extra={"k": SECRET})
    stored = queue.recent_events(conn, 1)[0]
    blob = "\n".join(texts) + str(stored) + json_payloads(conn)
    assert "t_42" in blob and "run 7" in blob and "reason: needs the login code" in blob
    assert SECRET not in blob and "assignee" not in blob and "dana" not in blob and "{" not in "\n".join(texts)


def test_subagent_stop_alert_uses_identifiers_only(monkeypatch, tmp_path):
    conn, texts, hooks = _hook_texts(monkeypatch, tmp_path)
    plugin._run_command("rules add task_done 2")  # make the finished-subagent class deliver
    hooks["subagent_stop"](parent_session_id="p-1", parent_turn_id="turn", child_session_id="c-9",
                           child_role="worker", child_summary=SECRET, child_status="completed",
                           tool_call_history=[{"tool_name": "write_file", "tool_input": SECRET}],
                           duration_ms=12)
    blob = "\n".join(texts) + json_payloads(conn)
    assert texts, "the alert was delivered"
    assert "c-9" in blob and "status: completed" in blob and "p-1" in blob
    assert SECRET not in blob and "write_file" not in blob and "tool_call_history" not in blob


def test_summaries_never_render_structures():
    assert plugin.kanban_summary({}) == "kanban task unknown blocked"
    assert plugin.kanban_summary({"task_id": {"x": SECRET}, "reason": [SECRET]}) == "kanban task unknown blocked"
    assert plugin.subagent_summary({"child_session_id": "c", "child_status": {"s": SECRET}}) == "subagent c finished"


def json_payloads(conn) -> str:
    return "\n".join(str(r) for r in conn.execute("SELECT * FROM events").fetchall())
