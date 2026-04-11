import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SessionStatus(str, enum.Enum):
    PENDING = "pending"       # link generated, user hasn't opened yet
    ACTIVE = "active"         # user opened the link, guidance in progress
    COMPLETE = "complete"     # all steps done successfully
    FAILED = "failed"         # something went wrong
    EXPIRED = "expired"       # token timed out


class StepAction(str, enum.Enum):
    NAVIGATE = "navigate"     # open a URL
    HIGHLIGHT = "highlight"   # glow an element to draw attention
    FILL = "fill"             # type into an input
    CLICK = "click"           # click a button/link
    WAIT = "wait"             # wait for user acknowledgement


# ---------------------------------------------------------------------------
# Table 1 — sessions
# ---------------------------------------------------------------------------
class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(32), primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    task: Mapped[str] = mapped_column(String(256), nullable=False)
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus), default=SessionStatus.PENDING, nullable=False
    )
    current_step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    steps: Mapped[list["Step"]] = relationship(
        "Step", back_populates="session", order_by="Step.step_number", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Table 2 — steps
# ---------------------------------------------------------------------------
class Step(Base):
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_token: Mapped[str] = mapped_column(
        String(32), ForeignKey("sessions.token", ondelete="CASCADE"), index=True
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[StepAction] = mapped_column(Enum(StepAction), nullable=False)
    selector: Mapped[str | None] = mapped_column(String(512), nullable=True)   # CSS selector
    instruction: Mapped[str] = mapped_column(Text, nullable=False)             # shown to user
    prefill_value: Mapped[str | None] = mapped_column(Text, nullable=True)     # from memory
    url: Mapped[str | None] = mapped_column(Text, nullable=True)               # for NAVIGATE
    is_skippable: Mapped[bool] = mapped_column(Boolean, default=False)         # memory shortcut
    is_done: Mapped[bool] = mapped_column(Boolean, default=False)

    session: Mapped["Session"] = relationship("Session", back_populates="steps")


# ---------------------------------------------------------------------------
# Table 3 — task_memory  (cached learned flows per user+task)
# ---------------------------------------------------------------------------
class TaskMemory(Base):
    __tablename__ = "task_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    task_type: Mapped[str] = mapped_column(String(256), index=True, nullable=False)
    learned_flow: Mapped[dict] = mapped_column(JSON, nullable=False)  # full step plan + prefills
    last_used: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
