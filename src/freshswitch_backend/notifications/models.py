from datetime import datetime
from typing import Literal

from pydantic import BaseModel

MOCK_CHANNEL = "알림톡(모의)"

NotificationKind = Literal[
    "refill_offer",
    "order_confirmation_requested",
    "order_notified",
    "order_committed",
    "change_request_applied",
    "change_request_rejected",
    "plan_confirmation_requested",
]


class Notification(BaseModel):
    notification_id: str
    kind: NotificationKind
    channel: str
    shipper_id: str | None
    plan_id: str | None
    order_id: str | None
    change_request_id: str | None
    title: str
    body: str
    link: str
    sent: bool = False
    created_at: datetime
