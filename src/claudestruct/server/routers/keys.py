"""API key management. Admin role required for create/revoke."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from claudestruct.server import audit as audit_mod
from claudestruct.server import auth as auth_mod
from claudestruct.server.models import ApiKey, Role
from claudestruct.server.schema import (
    CreateKeyRequest,
    CreateKeyResponse,
    KeyList,
    KeyMetadata,
)

router = APIRouter(prefix="/v1/keys", tags=["keys"])


def _to_metadata(k: ApiKey) -> KeyMetadata:
    return KeyMetadata(
        key_id=k.key_id,
        name=k.name,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
        revoked_at=k.revoked_at,
    )


@router.get("", response_model=KeyList)
def list_keys(
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.admin)),
    session: Session = Depends(auth_mod.get_session),
) -> KeyList:
    rows = session.execute(
        auth_mod.with_org_scope(
            select(ApiKey).order_by(ApiKey.created_at.desc()),
            ApiKey, principal,
        )
    ).scalars().all()
    return KeyList(keys=[_to_metadata(k) for k in rows])


@router.post(
    "",
    response_model=CreateKeyResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_key(
    body: CreateKeyRequest,
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.admin)),
    session: Session = Depends(auth_mod.get_session),
) -> CreateKeyResponse:
    full_key, key_id, hashed = auth_mod.generate_key()
    row = ApiKey(
        user_id=principal.user_id,
        org_id=principal.org_id,
        key_id=key_id,
        hashed_secret=hashed,
        name=body.name,
    )
    session.add(row)
    session.flush()
    audit_mod.record(
        session,
        org_id=principal.org_id,
        actor_user_id=principal.user_id,
        action="key.create",
        resource_type="api_key",
        resource_id=row.key_id,
        payload={"name": row.name},
    )
    session.commit()
    session.refresh(row)
    return CreateKeyResponse(
        full_key=full_key,
        key_id=row.key_id,
        name=row.name,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_key(
    key_id: str,
    principal: auth_mod.Principal = Depends(auth_mod.require_role(Role.admin)),
    session: Session = Depends(auth_mod.get_session),
) -> None:
    row = session.execute(
        auth_mod.with_org_scope(
            select(ApiKey).where(ApiKey.key_id == key_id),
            ApiKey, principal,
        )
    ).scalar_one_or_none()
    auth_mod.require_org_owned(row, principal, label="key")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        audit_mod.record(
            session,
            org_id=principal.org_id,
            actor_user_id=principal.user_id,
            action="key.revoke",
            resource_type="api_key",
            resource_id=row.key_id,
            payload={},
        )
        session.commit()
