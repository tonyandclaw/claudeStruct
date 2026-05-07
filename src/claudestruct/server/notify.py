"""Notification surface for hosted-side alerts (W6.5).

Three providers:

- ``LogNotifier`` — writes structured JSON to stdlib logging at WARNING.
  Default; safe in every environment, no external deps.
- ``SlackWebhookNotifier`` — POSTs an incoming-webhook payload to a
  Slack channel URL. Used by hosted deployments that have wired up a
  Slack workspace.
- ``EmailNotifier`` — sends one ``email.message.EmailMessage`` per
  alert via stdlib ``smtplib``. SMTP host / port / TLS / auth driven
  by env vars so the operator doesn't pin a per-call config.

Provider selection is env-driven so the ``cs serve alerts`` command
doesn't grow N flags for N future channels:

    CLAUDESTRUCT_NOTIFY_PROVIDER=log                  (default)
    CLAUDESTRUCT_NOTIFY_PROVIDER=slack
        CLAUDESTRUCT_SLACK_WEBHOOK_URL=https://hooks.slack.com/...
    CLAUDESTRUCT_NOTIFY_PROVIDER=email
        CLAUDESTRUCT_SMTP_HOST=smtp.example.com
        CLAUDESTRUCT_SMTP_PORT=587                    (default)
        CLAUDESTRUCT_SMTP_USER=alerts@example.com     (optional)
        CLAUDESTRUCT_SMTP_PASSWORD=...                (optional)
        CLAUDESTRUCT_SMTP_STARTTLS=1                  (default; 0 disables)
        CLAUDESTRUCT_SMTP_FROM=alerts@example.com
        CLAUDESTRUCT_SMTP_TO=oncall@example.com,sre@example.com

Test seam: producers should call ``notifier.notify(alert)`` rather
than constructing a provider directly so unit tests can swap in a
``CapturingNotifier`` and assert on the calls.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol

log = logging.getLogger("claudestruct.notify")


# --- Alert payload --------------------------------------------------


@dataclass(frozen=True)
class Alert:
    """Stable wire format. Producers populate the relevant fields and
    leave the rest empty rather than inventing per-channel shapes.

    ``severity`` is "info" / "warning" / "critical" — the Slack
    formatter colour-codes by it; the log formatter promotes critical
    to ERROR.
    """
    kind: str          # "cost_regression", "budget_breach", ...
    severity: str      # "info" / "warning" / "critical"
    org_slug: str
    summary: str       # one-line human-readable headline
    details: dict[str, Any]


# --- Notifier protocol ----------------------------------------------


class Notifier(Protocol):
    name: str

    def notify(self, alert: Alert) -> None: ...


# --- Providers ------------------------------------------------------


class LogNotifier:
    """Default provider. Writes a single JSON line per alert at WARNING
    (or ERROR for ``severity="critical"``). Suitable for every
    environment — node-exporter / loki / journald all consume it."""

    name = "log"

    def notify(self, alert: Alert) -> None:
        payload = json.dumps(asdict(alert) if is_dataclass(alert) else alert)
        if alert.severity == "critical":
            log.error("alert %s", payload)
        else:
            log.warning("alert %s", payload)


class SlackWebhookNotifier:
    """POSTs a Slack incoming-webhook payload.

    The webhook URL is an opaque secret — keep it out of logs and
    error messages. We use a per-instance ``http_client`` (default
    lazy-imported ``httpx``) so tests don't make real network calls.
    """

    name = "slack"

    def __init__(self, webhook_url: str, *, http_client: Any | None = None) -> None:
        if not webhook_url:
            raise ValueError("SlackWebhookNotifier requires a non-empty webhook URL")
        self._url = webhook_url
        self._http_client = http_client

    def _client(self) -> Any:
        if self._http_client is not None:
            return self._http_client
        # Lazy-import: keeps the lean install free of httpx until a
        # Slack notifier is actually constructed.
        import httpx
        return httpx.Client()

    def notify(self, alert: Alert) -> None:
        colour = {
            "critical": "#cc0000",
            "warning": "#cc9900",
            "info": "#3399cc",
        }.get(alert.severity, "#666666")
        payload = {
            "text": f"[{alert.severity.upper()}] {alert.org_slug}: {alert.summary}",
            "attachments": [{
                "color": colour,
                "fields": [
                    {"title": "kind", "value": alert.kind, "short": True},
                    {"title": "org", "value": alert.org_slug, "short": True},
                    *[
                        {"title": k, "value": str(v), "short": True}
                        for k, v in alert.details.items()
                    ],
                ],
            }],
        }
        client = self._client()
        try:
            resp = client.post(self._url, json=payload, timeout=10.0)
        finally:
            close = getattr(client, "close", None)
            # Only close if we built our own client; injected ones are
            # the caller's responsibility.
            if callable(close) and self._http_client is None:
                close()
        # Slack returns 200 + body "ok" on success. We log + swallow on
        # error — an alert delivery failure shouldn't crash the worker
        # that produced it.
        if getattr(resp, "status_code", 500) != 200:
            log.warning(
                "slack webhook returned %s; alert dropped",
                getattr(resp, "status_code", "<no-status>"),
            )


# --- EmailNotifier --------------------------------------------------


def _split_recipients(raw: str) -> list[str]:
    """Comma- or whitespace-separated list → list of stripped non-empty
    addresses. Tolerant of trailing commas and extra spacing because
    operators copy-paste from runbooks."""
    parts: list[str] = []
    for chunk in raw.replace(";", ",").split(","):
        addr = chunk.strip()
        if addr:
            parts.append(addr)
    return parts


class EmailNotifier:
    """Sends one email per alert via stdlib ``smtplib``.

    The SMTP transport is opened per-call rather than held open across
    alerts: ``cs serve alerts`` typically runs from cron with batches
    of 0–10 alerts, and a long-lived connection adds a reconnect-loop
    failure mode that pays for itself only at much higher rates.

    Test seam: ``smtp_factory`` returns a context-manager-compatible
    SMTP-like object so the unit tests don't open real sockets.
    """

    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        use_starttls: bool = True,
        sender: str,
        recipients: list[str],
        smtp_factory: Any | None = None,
    ) -> None:
        if not host:
            raise ValueError("EmailNotifier requires a non-empty SMTP host")
        if not sender:
            raise ValueError("EmailNotifier requires a non-empty sender address")
        if not recipients:
            raise ValueError("EmailNotifier requires at least one recipient")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_starttls = use_starttls
        self._sender = sender
        self._recipients = list(recipients)
        self._smtp_factory = smtp_factory

    def _smtp(self) -> Any:
        if self._smtp_factory is not None:
            return self._smtp_factory(self._host, self._port)
        # Lazy stdlib import — the import is cheap but keeps the module
        # graph clean for static analysers tracking deps.
        import smtplib
        return smtplib.SMTP(self._host, self._port, timeout=10)

    def _build_message(self, alert: Alert) -> Any:
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = (
            f"[{alert.severity.upper()}] claudeStruct alert: "
            f"{alert.org_slug} — {alert.kind}"
        )
        msg["From"] = self._sender
        msg["To"] = ", ".join(self._recipients)
        # Plaintext body keeps the alert readable in every mail UA and
        # pages cleanly through pagers / SMS gateways. JSON tail makes
        # the row machine-parseable for downstream tooling.
        details_lines = [f"  {k}: {v}" for k, v in alert.details.items()]
        body = (
            f"Severity: {alert.severity}\n"
            f"Org:      {alert.org_slug}\n"
            f"Kind:     {alert.kind}\n"
            f"Summary:  {alert.summary}\n"
            f"\nDetails:\n"
            + ("\n".join(details_lines) if details_lines else "  (none)")
            + "\n\n"
            f"JSON: {json.dumps(asdict(alert) if is_dataclass(alert) else alert)}\n"
        )
        msg.set_content(body)
        return msg

    def notify(self, alert: Alert) -> None:
        msg = self._build_message(alert)
        try:
            smtp = self._smtp()
        except Exception as exc:  # noqa: BLE001
            log.warning("smtp connect failed: %s; alert dropped", exc)
            return
        try:
            if self._use_starttls:
                # Best-effort: a server that refuses STARTTLS shouldn't
                # crash the cron caller. Log and keep going (matches
                # SlackWebhookNotifier's swallow-and-log policy).
                try:
                    smtp.starttls()
                except Exception as exc:  # noqa: BLE001
                    log.warning("smtp starttls failed: %s", exc)
            if self._username and self._password:
                try:
                    smtp.login(self._username, self._password)
                except Exception as exc:  # noqa: BLE001
                    log.warning("smtp login failed: %s; alert dropped", exc)
                    return
            try:
                smtp.send_message(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("smtp send failed: %s; alert dropped", exc)
                return
        finally:
            quit_fn = getattr(smtp, "quit", None)
            if callable(quit_fn):
                try:
                    quit_fn()
                except Exception:  # noqa: BLE001
                    pass


# --- Factory --------------------------------------------------------


def _email_notifier_from_env() -> EmailNotifier:
    """Read SMTP config out of the env and instantiate ``EmailNotifier``.

    Raises ``RuntimeError`` with an actionable message if any required
    variable is missing — keeps the cron job's failure mode loud.
    """
    host = os.environ.get("CLAUDESTRUCT_SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError(
            "CLAUDESTRUCT_NOTIFY_PROVIDER=email requires "
            "CLAUDESTRUCT_SMTP_HOST to be set"
        )
    sender = os.environ.get("CLAUDESTRUCT_SMTP_FROM", "").strip()
    if not sender:
        raise RuntimeError(
            "CLAUDESTRUCT_NOTIFY_PROVIDER=email requires "
            "CLAUDESTRUCT_SMTP_FROM to be set"
        )
    raw_to = os.environ.get("CLAUDESTRUCT_SMTP_TO", "").strip()
    recipients = _split_recipients(raw_to)
    if not recipients:
        raise RuntimeError(
            "CLAUDESTRUCT_NOTIFY_PROVIDER=email requires "
            "CLAUDESTRUCT_SMTP_TO to be set (comma-separated)"
        )
    port_raw = os.environ.get("CLAUDESTRUCT_SMTP_PORT", "587").strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(
            f"CLAUDESTRUCT_SMTP_PORT={port_raw!r} is not a valid integer"
        ) from exc
    starttls_raw = os.environ.get("CLAUDESTRUCT_SMTP_STARTTLS", "1").strip().lower()
    use_starttls = starttls_raw not in {"0", "false", "no", "off"}
    username = os.environ.get("CLAUDESTRUCT_SMTP_USER") or None
    password = os.environ.get("CLAUDESTRUCT_SMTP_PASSWORD") or None
    return EmailNotifier(
        host=host,
        port=port,
        username=username,
        password=password,
        use_starttls=use_starttls,
        sender=sender,
        recipients=recipients,
    )


def default_notifier() -> Notifier:
    """Build the configured provider.

    Defaults to ``LogNotifier`` so a fresh deployment never silently
    swallows alerts to a broken Slack URL — the operator has to opt in.
    """
    name = os.environ.get("CLAUDESTRUCT_NOTIFY_PROVIDER", "log").strip().lower()
    if name == "log":
        return LogNotifier()
    if name == "slack":
        url = os.environ.get("CLAUDESTRUCT_SLACK_WEBHOOK_URL", "").strip()
        if not url:
            raise RuntimeError(
                "CLAUDESTRUCT_NOTIFY_PROVIDER=slack requires "
                "CLAUDESTRUCT_SLACK_WEBHOOK_URL to be set"
            )
        return SlackWebhookNotifier(url)
    if name == "email":
        return _email_notifier_from_env()
    raise RuntimeError(
        f"unknown CLAUDESTRUCT_NOTIFY_PROVIDER={name!r}; "
        "expected one of: log, slack, email"
    )
