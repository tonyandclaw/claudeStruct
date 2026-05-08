"""Billing routes (W8.2).

Three endpoints, all admin-only except the read-only subscription view:

- ``GET  /v1/billing/subscription``   member+ — current tier + Stripe state
- ``POST /v1/billing/checkout``       admin — create a Stripe Checkout session
- ``GET  /v1/billing/usage``          member+ — period-to-date token usage
- ``POST /v1/billing/webhook``        unauthenticated — Stripe webhook receiver

The webhook intentionally lives off the bearer-auth path: Stripe
signs each delivery with a shared secret and we verify the signature
manually. The webhook secret is read from the
``STRIPE_WEBHOOK_SECRET`` env var via :mod:`claudestruct.secrets`.

Stripe SDK is **lazy-imported**. When it's not installed (the OSS
self-host path), checkout returns a deterministic placeholder URL
and the webhook returns 503 with a clear "Stripe not configured"
message rather than 500.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from claudestruct import dashboard as dash_mod
from claudestruct import secrets as secrets_mod
from claudestruct.server import audit as audit_mod
from claudestruct.server import auth as auth_mod
from claudestruct.server import billing as billing_mod
from claudestruct.server.models import Org, Role
from claudestruct.server.schema import (
    CheckoutRequest,
    CheckoutResponse,
    InvoicePdfResponse,
    SubscriptionResponse,
    UsageResponse,
)

router = APIRouter(prefix="/v1/billing", tags=["billing"])


@router.get("/subscription", response_model=SubscriptionResponse)
def get_subscription(
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.viewer)),
    session: Session = Depends(auth_mod.get_session),
) -> SubscriptionResponse:
    sub = billing_mod.get_or_default(session, principal.org_id)
    session.commit()
    limits = billing_mod.sandbox_limits_for_tier(sub.tier)
    from claudestruct.server.schema import SandboxLimitsResponse
    return SubscriptionResponse(
        org_slug=principal.org_slug,
        tier=billing_mod.Tier(sub.tier).value,  # type: ignore[arg-type]
        status=sub.status,
        stripe_customer_id=sub.stripe_customer_id,
        current_period_end=sub.current_period_end,
        sandbox_limits=SandboxLimitsResponse(**limits.as_dict()),
    )


@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_checkout(
    body: CheckoutRequest,
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.admin)),
    session: Session = Depends(auth_mod.get_session),
) -> CheckoutResponse:
    tier = billing_mod.Tier(body.tier)
    if billing_mod.stripe_sdk_available():
        session_id, url = billing_mod.create_stripe_checkout_session(
            org_slug=principal.org_slug,
            tier=tier,
            success_url=body.success_url,
            cancel_url=body.cancel_url,
        )
    else:
        session_id, url = billing_mod.stub_checkout_url(
            org_slug=principal.org_slug,
            tier=tier,
        )
    audit_mod.record(
        session,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        action="billing.checkout.create",
        resource_type="checkout_session",
        resource_id=session_id,
        payload={"tier": tier.value},
    )
    session.commit()
    return CheckoutResponse(checkout_session_id=session_id, url=url)


@router.get("/usage", response_model=UsageResponse)
def get_usage(
    request: Request,
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.viewer)),
    session: Session = Depends(auth_mod.get_session),
) -> UsageResponse:
    sub = billing_mod.get_or_default(session, principal.org_id)
    period_start, period_end = billing_mod.current_period_bounds(sub)
    # In the draft we read from the JSONL store the same way the
    # dashboard does. W6.1's run table will replace this with a
    # tenant-scoped DB query.
    run_root = Path(request.app.state.run_root)
    summaries = dash_mod.load_summaries(run_root)
    in_tok = out_tok = cache_r = cache_c = 0
    cost = 0.0
    for s in summaries:
        if not s.started_at:
            continue
        try:
            ts = datetime.fromisoformat(s.started_at)
        except ValueError:
            continue
        if ts.tzinfo is None:
            from datetime import timezone as _tz

            ts = ts.replace(tzinfo=_tz.utc)
        if not (period_start <= ts < period_end):
            continue
        in_tok += s.input_tokens
        out_tok += s.output_tokens
        cache_r += s.cache_read_tokens
        cache_c += s.cache_creation_tokens
        cost += s.cost_usd
    session.commit()
    return UsageResponse(
        period_start=period_start,
        period_end=period_end,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_r,
        cache_creation_tokens=cache_c,
        cost_usd=round(cost, 6),
    )


@router.get(
    "/invoices/{invoice_id}/pdf",
    response_model=InvoicePdfResponse,
)
def get_invoice_pdf(
    invoice_id: str,
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.viewer)),
    session: Session = Depends(auth_mod.get_session),
) -> InvoicePdfResponse:
    """Return a signed Stripe `invoice_pdf` URL for an invoice this
    org owns.

    Tenant isolation: the endpoint refuses to serve an invoice whose
    Stripe customer doesn't match the caller's
    `subscription.stripe_customer_id`. A cross-tenant invoice ID
    must look identical to a non-existent one (404, not 403) so the
    endpoint can't be turned into a customer-ID enumeration oracle.
    """
    if not billing_mod.stripe_sdk_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Stripe SDK not installed; install with `pip install stripe` "
                "to enable invoice PDF passthrough."
            ),
        )

    sub = billing_mod.get_or_default(session, principal.org_id)
    if not sub.stripe_customer_id:
        # Org has never gone through Checkout; can't possibly own an
        # invoice. 404 (not 403) keeps cross-tenant indistinguishable.
        raise HTTPException(status_code=404, detail="invoice not found")

    import stripe  # type: ignore

    try:
        invoice = stripe.Invoice.retrieve(invoice_id)
    except stripe.InvalidRequestError as exc:  # type: ignore[attr-defined]
        # Unknown ID, malformed ID, or already-deleted invoice all
        # land here. Map to 404 so we don't leak which case it was.
        raise HTTPException(status_code=404, detail="invoice not found") from exc
    except stripe.StripeError as exc:  # type: ignore[attr-defined]
        # Network blip / Stripe outage — surface the upstream
        # condition rather than swallowing it as a 404.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream Stripe error: {exc}",
        ) from exc

    # Tenant check.
    invoice_customer = (
        invoice.get("customer") if isinstance(invoice, dict)
        else getattr(invoice, "customer", None)
    )
    if invoice_customer != sub.stripe_customer_id:
        raise HTTPException(status_code=404, detail="invoice not found")

    pdf_url = (
        invoice.get("invoice_pdf") if isinstance(invoice, dict)
        else getattr(invoice, "invoice_pdf", None)
    )
    hosted_url = (
        invoice.get("hosted_invoice_url") if isinstance(invoice, dict)
        else getattr(invoice, "hosted_invoice_url", None)
    )
    if not pdf_url:
        # Invoice exists but Stripe hasn't finalised the PDF yet
        # (e.g. draft invoice, or a `void` finalised but PDF generation
        # hasn't run). Surface as 409 so the frontend can retry later
        # rather than redirect to a 404 page.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="invoice PDF not yet available; try again later",
        )

    return InvoicePdfResponse(
        invoice_id=invoice_id,
        invoice_pdf_url=pdf_url,
        hosted_invoice_url=hosted_url,
    )


@router.post("/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    session: Session = Depends(auth_mod.get_session),
) -> None:
    if not billing_mod.stripe_sdk_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Stripe SDK not installed; install with `pip install stripe` "
                "to enable webhook handling."
            ),
        )
    secret = secrets_mod.get("stripe.webhook_secret")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="STRIPE_WEBHOOK_SECRET not configured",
        )
    if stripe_signature is None:
        raise HTTPException(status_code=400, detail="missing Stripe-Signature header")
    raw = await request.body()
    import stripe  # type: ignore

    try:
        event = stripe.Webhook.construct_event(
            payload=raw,
            sig_header=stripe_signature,
            secret=secret,
        )
    except (stripe.SignatureVerificationError, ValueError) as exc:  # type: ignore[attr-defined]
        raise HTTPException(status_code=400, detail=f"invalid signature: {exc}") from exc

    _handle_stripe_event(session, event)
    session.commit()


def _handle_stripe_event(session: Session, event) -> None:
    """Route a verified Stripe event to the appropriate handler."""
    handlers = {
        "checkout.session.completed": _handle_checkout_completed,
        "customer.subscription.updated": _handle_subscription_updated,
        "customer.subscription.deleted": _handle_subscription_deleted,
        "invoice.payment_succeeded": _handle_invoice_payment_succeeded,
        "invoice.payment_failed": _handle_invoice_payment_failed,
    }
    handler = handlers.get(event.get("type", ""))
    if handler:
        handler(session, event)
    else:
        audit_mod.record(
            session,
            org_id=0,
            actor_user_id=None,
            action=f"stripe.{event.get('type', 'unknown')}",
            resource_type="stripe_event",
            resource_id=str(event.get("id", "")),
            payload={"type": event.get("type")},
        )


def _handle_checkout_completed(session: Session, event) -> None:
    """Transition org to the paid tier after successful checkout payment."""
    obj = event.get("object", "")
    if obj != "checkout.session":
        return

    customer_id: str | None = event.get("customer")
    subscription_id: str | None = event.get("subscription")
    metadata: dict = event.get("metadata", {})
    org_slug = metadata.get("org_slug")
    tier_str = metadata.get("tier")

    if not org_slug or not tier_str:
        audit_mod.record(
            session, org_id=0, actor_user_id=None,
            action="stripe.checkout.session.completed.invalid_metadata",
            resource_type="stripe_event",
            resource_id=str(event.get("id", "")),
            payload={"org_slug": org_slug, "tier": tier_str},
        )
        return

    org = session.execute(
        select(Org).where(Org.slug == org_slug)
    ).scalar_one_or_none()
    if org is None:
        return

    sub = billing_mod.get_or_default(session, org.id)
    sub.stripe_customer_id = customer_id
    sub.stripe_subscription_id = subscription_id
    sub.tier = tier_str
    sub.status = "active"
    if subscription_id:
        _update_subscription_period(session, sub, subscription_id)

    audit_mod.record(
        session,
        org_id=org.id,
        actor_user_id=None,
        action="stripe.checkout.session.completed",
        resource_type="subscription",
        resource_id=subscription_id or "",
        payload={"tier": tier_str},
    )


def _update_subscription_period(session: Session, sub, subscription_id: str) -> None:
    """Fetch the Stripe subscription and update period bounds on the row."""
    import stripe  # type: ignore

    try:
        stripe_sub = stripe.Subscription.retrieve(subscription_id)
    except Exception:  # noqa: BLE001
        return

    sub.current_period_start = datetime.fromisoformat(
        stripe_sub.current_period_start.isoformat()
    ) if stripe_sub.current_period_start else None
    sub.current_period_end = datetime.fromisoformat(
        stripe_sub.current_period_end.isoformat()
    ) if stripe_sub.current_period_end else None


def _handle_subscription_updated(session: Session, event) -> None:
    """Sync Stripe subscription status + period to our Subscription row."""
    obj = event.get("object", "")
    if obj != "customer.subscription":
        return

    subscription_id = event.get("id", "")
    customer_id = event.get("customer", "")
    status = event.get("status")

    sub = session.execute(
        select(billing_mod.Subscription).where(
            billing_mod.Subscription.stripe_customer_id == customer_id
        )
    ).scalar_one_or_none()
    if sub is None:
        return

    sub.status = status
    _update_subscription_period(session, sub, subscription_id)

    audit_mod.record(
        session,
        org_id=sub.org_id,
        actor_user_id=None,
        action="stripe.customer.subscription.updated",
        resource_type="subscription",
        resource_id=subscription_id,
        payload={"status": status},
    )


def _handle_subscription_deleted(session: Session, event) -> None:
    """Downgrade org to free tier when subscription is canceled."""
    obj = event.get("object", "")
    if obj != "customer.subscription":
        return

    subscription_id = event.get("id", "")
    customer_id = event.get("customer", "")

    sub = session.execute(
        select(billing_mod.Subscription).where(
            billing_mod.Subscription.stripe_customer_id == customer_id
        )
    ).scalar_one_or_none()
    if sub is None:
        return

    sub.tier = billing_mod.Tier.free.value
    sub.status = "canceled"

    audit_mod.record(
        session,
        org_id=sub.org_id,
        actor_user_id=None,
        action="stripe.customer.subscription.deleted",
        resource_type="subscription",
        resource_id=subscription_id,
        payload={"tier": "free"},
    )


def _handle_invoice_payment_succeeded(session: Session, event) -> None:
    """Renew the period bounds when Stripe successfully collects payment."""
    obj = event.get("object", "")
    if obj != "invoice":
        return

    customer_id = event.get("customer", "")
    subscription_id = event.get("subscription")

    sub = session.execute(
        select(billing_mod.Subscription).where(
            billing_mod.Subscription.stripe_customer_id == customer_id
        )
    ).scalar_one_or_none()
    if sub is None or not subscription_id:
        return

    _update_subscription_period(session, sub, subscription_id)

    audit_mod.record(
        session,
        org_id=sub.org_id,
        actor_user_id=None,
        action="stripe.invoice.payment_succeeded",
        resource_type="subscription",
        resource_id=subscription_id,
        payload={"period_end": str(sub.current_period_end) if sub.current_period_end else None},
    )


def _handle_invoice_payment_failed(session: Session, event) -> None:
    """Record payment failure — keep current tier but mark at risk."""
    obj = event.get("object", "")
    if obj != "invoice":
        return

    customer_id = event.get("customer", "")
    sub = session.execute(
        select(billing_mod.Subscription).where(
            billing_mod.Subscription.stripe_customer_id == customer_id
        )
    ).scalar_one_or_none()
    if sub is None:
        return

    sub.status = "past_due"
    audit_mod.record(
        session,
        org_id=sub.org_id,
        actor_user_id=None,
        action="stripe.invoice.payment_failed",
        resource_type="subscription",
        resource_id=str(event.get("id", "")),
        payload={},
    )
