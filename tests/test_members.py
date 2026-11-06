import threading
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from saas_fixtures import SAAS_CSRF, SHIPPER_ORG_ID, _mint_session, login_client, saas_app
from sqlalchemy import select, update

from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import redeem_invitation
from freshswitch_backend.persistence.seed import demo_orgs
from freshswitch_backend.persistence.tables import MembershipRow, OrgInvitationRow, UserRow

PASSWORD = "members-password"


def _client() -> TestClient:
    return TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF)


def _signup(email: str, org_name: str | None = None, org_type: str = "shipper", **extra):
    client = _client()
    body = {"email": email, "password": PASSWORD, "name": email.split("@")[0], **extra}
    if org_name is not None:
        body.update(org_name=org_name, org_type=org_type)
    return client, client.post("/api/auth/signup", json=body)


def _admin(email: str, org_name: str, org_type: str = "shipper") -> tuple[TestClient, str]:
    client, response = _signup(email, org_name, org_type)
    assert response.status_code == 201, response.text
    return client, response.json()["active_org"]["org_id"]


def _invite(client: TestClient, role: str = "member") -> dict:
    response = client.post("/api/saas/org/invitations", json={"role": role})
    assert response.status_code == 201, response.text
    return response.json()


def test_signup_with_invite_code_joins_existing_org() -> None:
    admin, org_id = _admin("boss@farm.example.com", "합천 딸기")
    invitation = _invite(admin)
    assert invitation["org_id"] == org_id
    assert invitation["org_name"] == "합천 딸기"
    assert invitation["status"] == "pending"
    assert len(invitation["code"]) >= 12

    staff, joined = _signup("staff@farm.example.com", invite_code=invitation["code"])
    assert joined.status_code == 201, joined.text
    me = joined.json()
    assert [m["org_id"] for m in me["memberships"]] == [org_id]
    assert me["active_org"]["role"] == "member"

    members = admin.get("/api/saas/org/members")
    assert members.status_code == 200
    assert [(m["email"], m["role"]) for m in members.json()] == [
        ("boss@farm.example.com", "admin"),
        ("staff@farm.example.com", "member"),
    ]
    assert staff.get("/api/saas/org/members").json() == members.json()

    listed = admin.get("/api/saas/org/invitations").json()
    assert listed[0]["status"] == "used"
    assert listed[0]["used_by"]["name"] == "staff"
    assert "code" not in listed[0]

    _, reuse = _signup("again@farm.example.com", invite_code=invitation["code"])
    assert reuse.status_code == 422
    assert "set-cookie" not in reuse.headers
    with database.begin() as db:
        assert (
            db.scalar(select(UserRow.id).where(UserRow.email == "again@farm.example.com")) is None
        )

    assert staff.post("/api/saas/org/invitations", json={}).status_code == 403
    assert staff.get("/api/saas/org/invitations").status_code == 403
    pending = _invite(admin)
    assert staff.delete(f"/api/saas/org/invitations/{pending['invitation_id']}").status_code == 403
    still = admin.get("/api/saas/org/invitations").json()[0]
    assert (still["invitation_id"], still["status"]) == (pending["invitation_id"], "pending")


def test_signup_requires_org_fields_without_invite() -> None:
    _, missing = _signup("lonely@example.com")
    assert missing.status_code == 422
    _, bogus = _signup("bogus@example.com", invite_code="no-such-code")
    assert bogus.status_code == 422


def test_invited_existing_user_switches_between_two_orgs() -> None:
    shipper, shipper_org = _admin("dual@example.com", "산청 딸기")
    forwarder_admin, forwarder_org = _admin("ops@forward.example.com", "남해 포워딩", "forwarder")
    invitation = _invite(forwarder_admin, "admin")

    accepted = shipper.post("/api/saas/invitations/accept", json={"code": invitation["code"]})
    assert accepted.status_code == 200, accepted.text
    me = accepted.json()
    assert [m["org_id"] for m in me["memberships"]] == [shipper_org, forwarder_org]
    assert me["active_org"]["org_id"] == forwarder_org
    assert me["active_org"]["role"] == "admin"
    assert shipper.get("/api/me").json()["active_org"]["org_id"] == forwarder_org
    assert shipper.get("/api/saas/forwarder/plans").status_code == 200
    assert shipper.get("/api/saas/shipper/profile").status_code == 403

    back = shipper.put("/api/auth/active-org", json={"org_id": shipper_org})
    assert back.status_code == 200
    assert back.json()["active_org"]["org_id"] == shipper_org
    assert shipper.get("/api/saas/shipper/profile").json()["org_id"] == shipper_org
    assert shipper.get("/api/saas/forwarder/plans").status_code == 403

    header = shipper.get("/api/saas/org/members", headers={"X-Org-Id": forwarder_org})
    assert {m["email"] for m in header.json()} == {"ops@forward.example.com", "dual@example.com"}

    again = shipper.post("/api/saas/invitations/accept", json={"code": invitation["code"]})
    assert again.status_code == 422
    second = _invite(forwarder_admin)
    already = shipper.post("/api/saas/invitations/accept", json={"code": second["code"]})
    assert already.status_code == 409


def test_expired_and_revoked_invitations_are_rejected() -> None:
    admin, _ = _admin("boss@expire.example.com", "만료 농장")
    expired = _invite(admin)
    with database.begin() as db:
        db.execute(
            update(OrgInvitationRow)
            .where(OrgInvitationRow.id == expired["invitation_id"])
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
    assert admin.get("/api/saas/org/invitations").json()[0]["status"] == "expired"
    _, late = _signup("late@expire.example.com", invite_code=expired["code"])
    assert late.status_code == 422

    revoked = _invite(admin)
    deleted = admin.delete(f"/api/saas/org/invitations/{revoked['invitation_id']}")
    assert deleted.status_code == 204
    _, gone = _signup("gone@expire.example.com", invite_code=revoked["code"])
    assert gone.status_code == 422

    other, _ = _admin("boss@other.example.com", "다른 농장")
    foreign = _invite(other)
    stolen = admin.delete(f"/api/saas/org/invitations/{foreign['invitation_id']}")
    assert stolen.status_code == 404


def test_sandbox_orgs_cannot_invite_or_accept(demo_tenants) -> None:
    sandbox = login_client(SHIPPER_ORG_ID)
    assert sandbox.post("/api/saas/org/invitations", json={}).status_code == 403
    members = sandbox.get("/api/saas/org/members")
    assert members.status_code == 200
    assert [m["role"] for m in members.json()] == ["admin", "admin"]

    admin, real_org = _admin("boss@real.example.com", "실제 농장")
    invitation = _invite(admin)
    demo_user = next(org.user_id for org in demo_orgs() if org.id == SHIPPER_ORG_ID)
    demo = _client()
    _mint_session(demo, saas_app, demo_user, SHIPPER_ORG_ID)
    refused = demo.post("/api/saas/invitations/accept", json={"code": invitation["code"]})
    assert refused.status_code == 401
    with database.begin() as db:
        joined = db.scalars(select(MembershipRow.user_id).where(MembershipRow.org_id == real_org))
        assert len(list(joined)) == 1

    anonymous = TestClient(saas_app)
    assert anonymous.get("/api/saas/org/members").status_code == 401
    accept = anonymous.post("/api/saas/invitations/accept", json={"code": invitation["code"]})
    assert accept.status_code == 401


def test_concurrent_accepts_of_two_invitations_return_conflict() -> None:
    admin, org_id = _admin("boss@race.example.com", "경합 농장")
    first = _invite(admin)
    second = _invite(admin)
    joiner, own_org = _admin("joiner@race.example.com", "가입자 농장")
    user_id = joiner.get("/api/me").json()["user"]["id"]
    results: list[int] = []

    def accept() -> None:
        response = joiner.post("/api/saas/invitations/accept", json={"code": second["code"]})
        results.append(response.status_code)

    with database.begin() as db:
        redeem_invitation(db, first["code"], user_id)
        racer = threading.Thread(target=accept)
        racer.start()
        time.sleep(0.5)
        assert racer.is_alive()
    racer.join(timeout=10)

    assert results == [409]
    statuses = {
        i["invitation_id"]: i["status"] for i in admin.get("/api/saas/org/invitations").json()
    }
    assert statuses == {first["invitation_id"]: "used", second["invitation_id"]: "pending"}
    with database.begin() as db:
        rows = db.scalars(
            select(MembershipRow.org_id).where(MembershipRow.user_id == user_id)
        ).all()
    assert sorted(rows) == sorted([org_id, own_org])
