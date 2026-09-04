from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

RobotId = Literal["AMR-01", "AMR-02", "AMR-03"]
RobotStatus = Literal["Moving", "Yielding", "Rerouting", "Task handoff", "Charging"]
EventType = Literal["LEASE", "INTENT", "REROUTE", "HANDOFF", "HEARTBEAT"]


class Point(BaseModel):
    x: float
    y: float


class RobotState(BaseModel):
    id: RobotId
    name: str
    color: str
    battery: float = Field(ge=0, le=100)
    status: RobotStatus
    task: str
    priority: int = Field(ge=0, le=100)
    path: list[Point]
    path_index: int = Field(ge=0)
    progress: float = Field(ge=0, lt=1)
    position: Point
    completed: int = Field(ge=0)


class FleetEvent(BaseModel):
    time: str
    type: EventType
    message: str


class FleetState(BaseModel):
    tick: int = Field(ge=0)
    running: bool
    aisle_blocked: bool
    reservation: RobotId | None
    lease_until: int = Field(ge=0)
    completed_tasks: int = Field(ge=0)
    collision_count: int = Field(ge=0)
    messages: int = Field(ge=0)
    events: list[FleetEvent]
    robots: list[RobotState]


class SimulationControl(BaseModel):
    running: bool


class IntentRequest(BaseModel):
    robot_id: RobotId
    corridor_id: str = Field(default="C-14", min_length=1, max_length=32)
    eta_seconds: float = Field(ge=0, le=300)


class ReservationRequest(BaseModel):
    robot_id: RobotId
    corridor_id: str = Field(default="C-14", min_length=1, max_length=32)
    lease_seconds: float = Field(default=4.8, gt=0, le=30)


class BlockageRequest(BaseModel):
    aisle_id: str = Field(default="B-07", min_length=1, max_length=32)


class TaskBidRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    destination: str = Field(min_length=1, max_length=64)
    candidate_robot_ids: list[RobotId] = Field(min_length=1)


TaskStatus = Literal["Queued", "Assigned", "In Progress", "Completed", "Blocked"]


class TaskCreate(BaseModel):
    pickup: str = Field(min_length=1, max_length=64)
    destination: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=50, ge=1, le=100)


class TaskRecord(BaseModel):
    id: str
    pickup: str
    destination: str
    priority: int = Field(ge=1, le=100)
    status: TaskStatus
    assigned_robot_id: RobotId | None = None
    created_at: datetime


class KnowledgeChunkCreate(BaseModel):
    source_name: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=20_000)
    metadata: dict[str, str] = Field(default_factory=dict)
    embedding: list[float] = Field(min_length=384, max_length=384)


class KnowledgeSearch(BaseModel):
    embedding: list[float] = Field(min_length=384, max_length=384)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgeQuery(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgeChunk(KnowledgeChunkCreate):
    id: str
    created_at: datetime


class AccessToken(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class CurrentUser(BaseModel):
    id: str
    email: str
    roles: list[str]


class UserDetail(BaseModel):
    id: str
    email: str
    roles: list[str]
    active: bool
    created_at: datetime


class UserStatusUpdate(BaseModel):
    active: bool


AppRole = Literal["viewer", "operator", "admin", "fleet-agent"]


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    roles: list[AppRole] = Field(default_factory=lambda: ["viewer"], min_length=1, max_length=4)
