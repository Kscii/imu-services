from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CreateRun(StrictModel):
    request_key: Key
    recording_id: Key
    device_id: Key
    start_time: datetime
    steps_enabled: bool | None = Field(default=None, strict=True)
    mode: Literal["incremental"] = "incremental"

    @field_validator("start_time")
    @classmethod
    def explicit_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("start_time requires an explicit UTC offset")
        return value


class BatchRequest(StrictModel):
    batch_id: Key
    start_sample: int = Field(ge=0, le=2**53, strict=True)
    sample_rate_hz: int = Field(default=25, strict=True)
    acceleration_mps2: list[tuple[float, float, float]] = Field(min_length=1, max_length=15000)
    angular_velocity_rads: list[tuple[float, float, float]]
    valid: list[Annotated[bool, Field(strict=True)]] | None = None
    excluded: list[Annotated[bool, Field(strict=True)]] | None = None

    @field_validator("sample_rate_hz")
    @classmethod
    def supported_rate(cls, value):
        if value != 25:
            raise ValueError("only standardized 25 Hz input is supported")
        return value


class GapRequest(StrictModel):
    batch_id: Key
    start_sample: int = Field(ge=0, le=2**53, strict=True)
    sample_count: int = Field(ge=1, le=15000, strict=True)
    excluded: bool = Field(default=False, strict=True)


class WholeRecordHeader(CreateRun):
    format: Literal["imu.recording.ndjson.v2"]
    mode: Literal["whole_record"] = "whole_record"
    sample_rate_hz: Literal[25] = 25
    sample_count: int = Field(ge=1, le=2_250_000, strict=True)


class SealRequest(StrictModel):
    end_sample: int = Field(ge=0, le=2**53, strict=True)


class ActivityAnalysisRequest(StrictModel):
    participant_id: Key
    run_ids: list[UUID] = Field(max_length=100)
    timezone: str = Field(min_length=1, max_length=128)
    date_from: date
    date_to: date

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("unknown IANA timezone") from None
        return value

    @model_validator(mode="after")
    def bounded_dates(self):
        if not 0 <= (self.date_to-self.date_from).days < 31:
            raise ValueError("analysis must span 1 to 31 inclusive dates")
        return self
