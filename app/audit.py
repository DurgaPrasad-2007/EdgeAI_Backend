from __future__ import annotations

from contextvars import ContextVar

# Who is driving the current request. The tick loop and startup code keep the default.
SYSTEM_ACTOR = "coordinator"
actor_var: ContextVar[str] = ContextVar("edgefleet_actor", default=SYSTEM_ACTOR)
