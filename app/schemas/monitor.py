from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class MonitorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    url: HttpUrl

    interval_seconds: int = Field(default=300, ge=60, le=86400)
    timeout_seconds: int = Field(default=10, ge=1, le=30)

    expected_status_min: int = Field(default=200, ge=100, le=599)
    expected_status_max: int = Field(default=299, ge=100, le=599)

    degraded_threshold_ms: int = Field(
        default=1500,
        ge=100,
        le=60000,
    )

    @model_validator(mode="after")
    def validate_status_range(self):
        if self.expected_status_min > self.expected_status_max:
            raise ValueError(
                "expected_status_min must be less than or equal to expected_status_max"
            )
        return self


class MonitorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: HttpUrl | None = None

    interval_seconds: int | None = Field(default=None, ge=60, le=86400)
    timeout_seconds: int | None = Field(default=None, ge=1, le=30)

    expected_status_min: int | None = Field(default=None, ge=100, le=599)
    expected_status_max: int | None = Field(default=None, ge=100, le=599)

    degraded_threshold_ms: int | None = Field(
        default=None,
        ge=100,
        le=60000,
    )

    is_active: bool | None = None


class MonitorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    name: str
    url: str

    interval_seconds: int
    timeout_seconds: int

    expected_status_min: int
    expected_status_max: int
    degraded_threshold_ms: int

    status: str
    is_active: bool

    consecutive_failures: int
    consecutive_successes: int

    last_response_ms: float | None
    last_http_status: int | None

    last_checked_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    next_check_at: datetime | None

    created_at: datetime
    updated_at: datetime


class MonitorListResponse(BaseModel):
    items: list[MonitorResponse]
    count: int


class MonitorMetricPoint(BaseModel):
    timestamp: datetime
    avg_response_ms: float | None
    success_rate_percent: float | None
    checks: int


class MonitorMetricsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    monitor_id: int
    range: str
    from_: datetime = Field(alias="from", serialization_alias="from")
    to: datetime

    total_checks: int
    successful_checks: int
    failed_checks: int

    uptime_percent: float | None

    avg_response_ms: float | None
    p95_response_ms: float | None
    min_response_ms: float | None
    max_response_ms: float | None

    points: list[MonitorMetricPoint]


class MonitorCheckResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    checked_at: datetime
    success: bool
    http_status: int | None
    response_ms: float | None
    error_type: str | None
    error_message: str | None


class MonitorCheckListResponse(BaseModel):
    items: list[MonitorCheckResponse]
    count: int
