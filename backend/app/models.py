"""Validated robot event schema (simulated SG-R1 detections)."""
import math
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from typing_extensions import Annotated

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,63}$")]
EventId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,95}$")]
Zone = Literal["A1", "A2", "B1", "B2", "C1", "C2", "UNMAPPED"]
EventType = Literal["unauthorized_access", "person_detected", "fire_detected", "smoke_detected",
                    "patrol_status", "robot_status"]
Severity = Literal["info", "warning", "high", "critical"]
SEVERITY_RANK = {"info": 0, "warning": 1, "high": 2, "critical": 3}
MIN_SEVERITY = {"unauthorized_access": "high", "fire_detected": "high", "smoke_detected": "warning"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Numeric(Strict):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)  # no "1" -> 1.0 coercion


class Position(Numeric):
    x: float = Field(ge=-15, le=15)
    y: float = Field(ge=-12, le=12)


class Person(Numeric):
    person_id: Identifier
    classification: Literal["AUTHORIZED", "UNAUTHORIZED", "UNKNOWN"]
    role: Optional[Annotated[str, StringConstraints(max_length=32)]] = None
    badge_id: Optional[Identifier] = None
    zone: Zone
    distance_m: float = Field(ge=0, le=50)


class RobotState(Numeric):
    speed_mps: float = Field(ge=-3, le=3)
    heading_rad: float = Field(ge=-math.pi - 0.01, le=math.pi + 0.01)
    camera_enabled: bool
    sim_time_s: float = Field(ge=0)


class RobotEvent(Strict):
    event_id: EventId
    event_type: EventType
    robot_id: Identifier
    timestamp: AwareDatetime
    severity: Severity
    zone: Zone
    position: Position
    source: Literal["webots-sgr1-simulation"] = "webots-sgr1-simulation"
    person: Optional[Person] = None
    robot: Optional[RobotState] = None
    message: Optional[Annotated[str, StringConstraints(max_length=200)]] = None

    @field_validator("timestamp", mode="before")
    @classmethod
    def iso_with_offset(cls, value):
        if not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})", value):
            raise ValueError("timestamp must be ISO 8601 with a UTC offset")
        return value

    @field_validator("timestamp")
    @classmethod
    def to_utc(cls, value: datetime):
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def consistent(self):
        if self.event_type in ("unauthorized_access", "person_detected") and self.person is None:
            raise ValueError(f"{self.event_type} requires person")
        if self.event_type == "unauthorized_access" and self.person.classification != "UNAUTHORIZED":
            raise ValueError("unauthorized_access requires classification UNAUTHORIZED")
        if self.event_type == "robot_status" and self.robot is None:
            raise ValueError("robot_status requires robot")
        floor = MIN_SEVERITY.get(self.event_type)
        if floor and SEVERITY_RANK[self.severity] < SEVERITY_RANK[floor]:
            raise ValueError(f"{self.event_type} severity must be at least {floor}")
        return self
