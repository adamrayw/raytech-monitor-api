from datetime import datetime
from pydantic import BaseModel, ConfigDict


class NotificationPreferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    email_enabled: bool = False
    http_down_enabled: bool = True
    http_recovered_enabled: bool = True
    heartbeat_missed_enabled: bool = True
    heartbeat_recovered_enabled: bool = True
    ssl_expiring_enabled: bool = True
    ssl_expired_enabled: bool = True
    ssl_invalid_enabled: bool = True
    ssl_recovered_enabled: bool = True
    test_mode: bool = False
    test_recipient: str | None = None
    account_email: str | None = None


class NotificationPreferenceUpdate(BaseModel):
    email_enabled: bool | None = None
    http_down_enabled: bool | None = None
    http_recovered_enabled: bool | None = None
    heartbeat_missed_enabled: bool | None = None
    heartbeat_recovered_enabled: bool | None = None
    ssl_expiring_enabled: bool | None = None
    ssl_expired_enabled: bool | None = None
    ssl_invalid_enabled: bool | None = None
    ssl_recovered_enabled: bool | None = None


class TestEmailResponse(BaseModel):
    status: str
    delivery_id: int
    recipient: str
    test_mode: bool
    message: str


class EmailDeliveryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    subject: str
    recipient_email: str
    status: str
    attempt_count: int
    provider_message_id: str | None = None
    last_error_message: str | None = None
    created_at: datetime
    sent_at: datetime | None = None


class EmailHistoryResponse(BaseModel):
    items: list[EmailDeliveryItem]
    count: int
