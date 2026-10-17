from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------
# Authenticated Management Schemas
# ----------------------------------------------------

class StatusPageMonitorItem(BaseModel):
    monitor_id: int
    display_name: str | None = None
    display_order: int = 0


class StatusPageMonitorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    monitor_id: int
    monitor_name: str
    monitor_type: str
    display_name: str | None = None
    display_order: int
    current_status: str


class StatusPageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=3, max_length=63)
    description: str | None = Field(default=None, max_length=5000)
    accent_color: str = Field(default="#00f0ff", max_length=32)
    logo_url: str | None = Field(default=None, max_length=1024)
    is_published: bool = False
    show_incident_history: bool = True
    monitors: list[StatusPageMonitorItem] = []


class StatusPageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=3, max_length=63)
    description: str | None = Field(default=None, max_length=5000)
    accent_color: str | None = Field(default=None, max_length=32)
    logo_url: str | None = Field(default=None, max_length=1024)
    is_published: bool | None = None
    show_incident_history: bool | None = None
    monitors: list[StatusPageMonitorItem] | None = None


class StatusPageSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str | None
    accent_color: str
    logo_url: str | None
    is_published: bool
    show_incident_history: bool
    monitor_count: int
    public_url: str
    created_at: datetime
    updated_at: datetime


class StatusPageDetailResponse(StatusPageSummaryResponse):
    monitors: list[StatusPageMonitorResponse]


class StatusPageListResponse(BaseModel):
    items: list[StatusPageSummaryResponse]
    count: int


# ----------------------------------------------------
# Public Status Page Schemas (Strict Allowlist)
# ----------------------------------------------------

class PublicServiceItem(BaseModel):
    id: int
    name: str
    type: str
    status: str  # operational, degraded, down, maintenance, unknown
    display_order: int
    last_checked_at: datetime | None = None


class PublicStatusPageResponse(BaseModel):
    name: str
    slug: str
    description: str | None = None
    accent_color: str
    logo_url: str | None = None
    overall_status: str
    overall_headline: str
    show_incident_history: bool
    updated_at: datetime
    services: list[PublicServiceItem]


class PublicIncidentItem(BaseModel):
    id: int
    service_name: str
    status: str
    started_at: datetime
    resolved_at: datetime | None = None
    duration_seconds: int | None = None
    public_message: str


class PublicIncidentsResponse(BaseModel):
    items: list[PublicIncidentItem]
    count: int


class PublicUptimeDay(BaseModel):
    date: str
    has_data: bool
    uptime_percent: float | None = None
    status: str
    total_checks: int
    successful_checks: int


class PublicServiceHistory(BaseModel):
    service_id: int
    service_name: str
    service_type: str
    uptime_30d_percent: float | None = None
    days: list[PublicUptimeDay]
    note: str | None = None


class PublicHistoryResponse(BaseModel):
    services: list[PublicServiceHistory]
    range: str = "30d"
