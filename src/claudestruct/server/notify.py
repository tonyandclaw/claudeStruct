"""Notification surface for hosted-side alerts (W6.5).

Two providers:

- ``LogNotifier`` — writes structured JSON to stdlib logging at WARNING.
  Default; safe in every environment, no external deps.
- ``SlackWebhookNotifier`` — POSTs an incoming-webhook payload to a
  Slack channel URL. Used by hosted deployments that have wired up a
  Slack workspace.
- ``EmailNotifier`` — sends a plain-text email via SMTP+STARTTLS.
  Useful where Slack isn't an option (regulated environments,
  on-call rotations that page via email-to-SMS, etc.).

Provider selection is env-driven so the ``cs serve alerts`` command
doesn't grow N flags for N future channels:

    CLAUDESTRUCT_NOTIFY_PROVIDER=log                  (default)
    CLAUDESTRUCT_NOTIFY_PROVIDER=slack
        CLAUDESTRUCT_SLACK_WEBHOOK_URL=https://hooks.slack.com/...
    CLAUDESTRUCT_NOTIFY_PROVIDER=email
        CLAUDESTRUCT_EMAIL_SMTP_HOST=smtp.example.com
        CLAUDESTRUCT_EMAIL_SMTP_PORT=587
        CLAUDESTRUCT_EMAIL_SMTP_USERNAME=alerts@example.com
        CLAUDESTRUCT_EMAIL_SMTP_PASSWORD=...
        CLAUDESTRUCT_EMAIL_FROM=alerts@example.com
        CLAUDESTRUCT_EMAIL_TO=oncall@example.com,sre@example.com

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


class EmailNotifier:
    """Sends an alert as a plain-text email via SMTP+STARTTLS.

    Designed for regulated / no-Slack environments. We use stdlib
    ``smtplib`` (no extra dep) and the connection is short-lived: one
    SMTP session per alert. STARTTLS is required — we deliberately
    don't support unencrypted SMTP because alert bodies routinely
    name orgs and quote details that a hosted deployment would not
    want on the wire in plaintext.

    Inject ``smtp_factory`` for tests. Production callers leave it
    None and we lazy-construct a real ``smtplib.SMTP``.
    """

    name = "email"

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: list[str],
        smtp_factory: Any | None = None,
    ) -> None:
        if not smtp_host:
            raise ValueError("EmailNotifier requires smtp_host")
        if not from_addr:
            raise ValueError("EmailNotifier requires from_addr")
        if not to_addrs:
            raise ValueError("EmailNotifier requires at least one to_addr")
        self._host = smtp_host
        self._port = smtp_port
        self._username = username
        self._password = password
        self._from = from_addr
        self._to = to_addrs
        self._smtp_factory = smtp_factory

    def _open_smtp(self) -> Any:
        if self._smtp_factory is not None:
            return self._smtp_factory(self._host, self._port)
        # Lazy-import: stdlib so no extra dep, but we still defer the
        # import until an EmailNotifier is actually constructed.
        import smtplib
        return smtplib.SMTP(self._host, self._port, timeout=10.0)

    def _build_message(self, alert: Alert) -> str:
        # Plain-text RFC 5322 message. Keep it dependency-free: no
        # MIMEMultipart, no HTML. Alert details serialise as one
        # `key: value` line each.
        from email.utils import formatdate

        lines = [
            f"From: {self._from}",
            f"To: {', '.join(self._to)}",
            f"Date: {formatdate(localtime=False)}",
            f"Subject: [{alert.severity.upper()}] {alert.org_slug}: "
            f"{alert.summary}",
            "",
            f"kind:     {alert.kind}",
            f"severity: {alert.severity}",
            f"org:      {alert.org_slug}",
            f"summary:  {alert.summary}",
            "",
            "details:",
        ]
        for k, v in alert.details.items():
            lines.append(f"  {k}: {v}")
        return "\r\n".join(lines)

    def notify(self, alert: Alert) -> None:
        # Best-effort: an SMTP failure must not crash the producer.
        try:
            smtp = self._open_smtp()
        except Exception as exc:  # noqa: BLE001
            log.warning("email notifier failed to open SMTP: %s", exc)
            return
        try:
            try:
                smtp.starttls()
            except Exception:  # noqa: BLE001
                # If the server doesn't support STARTTLS we abort
                # rather than fall back to plaintext — see class
                # docstring.
                log.warning(
                    "email notifier: server does not support STARTTLS; "
                    "alert dropped"
                )
                return
            if self._username:
                try:
                    smtp.login(self._username, self._password)
                except Exception as exc:  # noqa: BLE001
                    log.warning("email notifier: login failed: %s", exc)
                    return
            try:
                smtp.sendmail(
                    self._from, self._to, self._build_message(alert),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("email notifier: sendmail failed: %s", exc)
        finally:
            quit_ = getattr(smtp, "quit", None)
            if callable(quit_):
                try:
                    quit_()
                except Exception:  # noqa: BLE001
                    pass


# --- Factory --------------------------------------------------------


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
        host = os.environ.get("CLAUDESTRUCT_EMAIL_SMTP_HOST", "").strip()
        port_raw = os.environ.get("CLAUDESTRUCT_EMAIL_SMTP_PORT", "587").strip()
        from_addr = os.environ.get("CLAUDESTRUCT_EMAIL_FROM", "").strip()
        to_raw = os.environ.get("CLAUDESTRUCT_EMAIL_TO", "").strip()
        username = os.environ.get(
            "CLAUDESTRUCT_EMAIL_SMTP_USERNAME", "",
        ).strip()
        password = os.environ.get(
            "CLAUDESTRUCT_EMAIL_SMTP_PASSWORD", "",
        )
        if not host or not from_addr or not to_raw:
            raise RuntimeError(
                "CLAUDESTRUCT_NOTIFY_PROVIDER=email requires "
                "CLAUDESTRUCT_EMAIL_SMTP_HOST, CLAUDESTRUCT_EMAIL_FROM, "
                "and CLAUDESTRUCT_EMAIL_TO to be set"
            )
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise RuntimeError(
                f"CLAUDESTRUCT_EMAIL_SMTP_PORT={port_raw!r} is not an integer"
            ) from exc
        # `addr1,addr2` → ["addr1", "addr2"], whitespace-stripped.
        to_addrs = [a.strip() for a in to_raw.split(",") if a.strip()]
        return EmailNotifier(
            smtp_host=host,
            smtp_port=port,
            username=username,
            password=password,
            from_addr=from_addr,
            to_addrs=to_addrs,
        )
    raise RuntimeError(
        f"unknown CLAUDESTRUCT_NOTIFY_PROVIDER={name!r}; "
        f"expected one of: log, slack, email"
    )
