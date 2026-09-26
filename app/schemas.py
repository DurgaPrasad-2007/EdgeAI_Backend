from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

# Robots are data (see graph.ROBOT_FLEET); ids are validated against the live fleet, not a Literal.
RobotId = str
RobotStatus = Literal["Idle", "Moving", "Yielding", "Rerouting", "Task handoff", "Charging", "Blocked"]
RobotLeg = Literal["idle", "to_pickup", "to_drop", "to_home", "to_charge"]
EventType = Literal["LEASE", "INTENT", "REROUTE", "HANDOFF", "HEARTBEAT"]
P2PType = Literal["MUTEX_REQ", "MUTEX_GRANT", "YIELD_ACK", "OBSTACLE_ALERT", "TASK_BID", "HEARTBEAT"]
TaskStatus = Literal["Queued", "Assigned", "In Progress", "Completed", "Blocked"]


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
    task_id: str | None = None
    leg: RobotLeg = "idle"
    target: str | None = None
    priority: int = Field(ge=0, le=100)
    path: list[Point]
    path_index: int = Field(ge=0)
    progress: float = Field(ge=0, lt=1)
    position: Point
    completed: int = Field(ge=0)
    payload_capacity_kg: float = 0.0
    current_payload_kg: float = 0.0
    decision: str = ""  # the robot's own last decision, in words (why it yielded, rerouted, won a bid...)
    online: bool = True  # false while its radio is silent


class FleetEvent(BaseModel):
    id: int = 0
    time: str
    type: EventType
    message: str


class P2PMessage(BaseModel):
    id: str
    sender: RobotId
    recipient: str
    type: P2PType
    payload: str
    timestamp: str


class AuditRecord(BaseModel):
    id: int
    created_at: datetime
    actor: str
    category: Literal["AUTH", "CONTROL", "TASK", "CONFIG", "SYSTEM"]
    event_type: str | None = None
    sim_time: str | None = None
    message: str


class Kpis(BaseModel):
    fleet_utilization_pct: int = 0
    avg_battery_pct: int = 0
    collision_count: int = 0
    active_leases: int = 0
    operational_pct: int = 100
    completed_total: int = 0
    queued_tasks: int = 0
    tasks_per_hour: float = 0.0
    tick: int = 0


class TaskRecord(BaseModel):
    id: str
    pickup: str
    destination: str
    priority: int = Field(ge=1, le=100)
    status: TaskStatus
    payload_kg: float = 150.0
    payload_size: Literal["small", "medium", "heavy", "pallet"] = "medium"
    urgency: Literal["low", "standard", "critical"] = "standard"
    assigned_robot_id: RobotId | None = None
    created_at: datetime


class FleetState(BaseModel):
    seq: int = 0
    tick: int = Field(ge=0)
    running: bool
    aisle_blocked: bool
    blocked_nodes: list[str] = Field(default_factory=list)
    reservation: RobotId | None
    lease_until: int = Field(ge=0)
    leases: dict[str, RobotId] = Field(default_factory=dict)
    completed_tasks: int = Field(ge=0)
    collision_count: int = Field(ge=0)
    messages: int = Field(ge=0)
    events: list[FleetEvent]
    p2p: list[P2PMessage] = Field(default_factory=list)
    robots: list[RobotState]
    tasks: list[TaskRecord] = Field(default_factory=list)
    kpis: Kpis = Field(default_factory=Kpis)
    mode: str = "decentralized"


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
    aisle_id: str = Field(default="B-07", min_length=1, max_length=64)
    blocked: bool = True


class TaskBidRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    destination: str = Field(min_length=1, max_length=64)
    candidate_robot_ids: list[RobotId] = Field(min_length=1)


class TaskCreate(BaseModel):
    pickup: str = Field(min_length=1, max_length=64)
    destination: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=50, ge=1, le=100)
    payload_kg: float = Field(default=150.0, ge=1.0, le=2000.0)
    payload_size: Literal["small", "medium", "heavy", "pallet"] = Field(default="medium")
    urgency: Literal["low", "standard", "critical"] = Field(default="standard")


class TaskUpdate(BaseModel):
    priority: int | None = Field(default=None, ge=1, le=100)
    payload_kg: float | None = Field(default=None, ge=1.0, le=2000.0)
    payload_size: Literal["small", "medium", "heavy", "pallet"] | None = None
    urgency: Literal["low", "standard", "critical"] | None = None
    assigned_robot_id: RobotId | None = None
    status: TaskStatus | None = None


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
    similarity: float | None = None  # cosine similarity to the query, when searched


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


class BatteryFaultRequest(BaseModel):
    robot_id: str
    battery: float = 22.0


class AgentFaultRequest(BaseModel):
    robot_id: str
