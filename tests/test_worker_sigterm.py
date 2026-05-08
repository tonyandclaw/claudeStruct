"""Tests for the worker daemon's SIGTERM handling.

`cs serve worker` (no --once) runs as a long-lived daemon. k8s
sends SIGTERM during pod termination; systemd sends it on stop;
`kill -TERM <pid>` is the manual equivalent. The default Python
SIGTERM handler exits the process without triggering
KeyboardInterrupt — which would skip the graceful drain and
leave the in-flight `Run` row stuck in `running`.

The CLI installs a custom SIGTERM handler that re-raises as
KeyboardInterrupt so the existing graceful-shutdown path runs.
These tests pin that handler.
"""
from __future__ import annotations

import signal

import pytest

pytest.importorskip("click")


def test_worker_serve_command_traps_sigterm(monkeypatch):
    """Invoking `cs serve worker` registers a SIGTERM handler that
    re-raises as KeyboardInterrupt — without this, k8s pod
    termination would hard-kill an in-flight run."""
    from claudestruct.server import cli as serve_cli

    # Capture the handler the CLI installs without actually
    # backgrounding the worker.
    installed: dict = {}
    real_signal = signal.signal

    def _capture(sig, handler):
        if sig == signal.SIGTERM:
            installed["sigterm"] = handler
        return real_signal(sig, handler)

    monkeypatch.setattr(signal, "signal", _capture)

    # Stub WorkerThread so `start()` doesn't actually spawn — we
    # just want to walk the CLI flow far enough to hit the
    # signal-handler install.

    class _StubInner:
        def join(self):
            # Simulate the SIGTERM landing during the join — the
            # signal handler installed by the CLI re-raises it as
            # KeyboardInterrupt.
            raise KeyboardInterrupt

        def is_alive(self):
            return False

    class _StubThread:
        def __init__(self, **kw):
            self._thread = _StubInner()

        def start(self):
            pass

        def stop(self, **kw):
            pass

    monkeypatch.setattr(
        "claudestruct.server.worker.WorkerThread", _StubThread,
    )

    from click.testing import CliRunner
    runner = CliRunner()
    res = runner.invoke(
        serve_cli.serve_group,
        ["worker", "--db-url", "sqlite:///:memory:"],
    )
    assert res.exit_code == 0, (
        f"CLI exited {res.exit_code}: {res.output} / {res.exception!r}"
    )
    assert "sigterm" in installed, (
        "CLI did not install a SIGTERM handler — k8s pod termination "
        "would skip the graceful-drain path"
    )
    handler = installed["sigterm"]
    # The handler must raise KeyboardInterrupt when the signal lands.
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGTERM, None)


def test_alerts_daemon_traps_sigterm(monkeypatch):
    """Same SIGTERM trap on `cs serve alerts --watch`. A k8s
    rollout-restart of the alerts deployment must let the
    in-flight pass finish rather than hard-killing the
    notifier call."""
    from claudestruct.server import cli as serve_cli

    installed: dict = {}
    real_signal = signal.signal

    def _capture(sig, handler):
        if sig == signal.SIGTERM:
            installed["sigterm"] = handler
        return real_signal(sig, handler)

    monkeypatch.setattr(signal, "signal", _capture)

    class _StubScheduler:
        passes_completed = 0

        def __init__(self, **kw):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(
        "claudestruct.server.alerts.AlertsScheduler", _StubScheduler,
    )

    # Simulate a SIGTERM landing inside the daemon's blocking sleep
    # by patching `time.sleep` to raise KeyboardInterrupt the first
    # time it's called.
    import time as _time

    def _sleep_raises(_n):
        raise KeyboardInterrupt
    monkeypatch.setattr(_time, "sleep", _sleep_raises)

    from click.testing import CliRunner
    runner = CliRunner()
    res = runner.invoke(
        serve_cli.serve_group,
        [
            "alerts",
            "--db-url", "sqlite:///:memory:",
            "--watch",
            "--interval", "60",
        ],
    )
    assert res.exit_code == 0, (
        f"CLI exited {res.exit_code}: {res.output} / {res.exception!r}"
    )
    assert "sigterm" in installed
    handler = installed["sigterm"]
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGTERM, None)
