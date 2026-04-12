import asyncio
import sys
from app.core.database import AsyncSessionLocal
from sqlalchemy import select
from app.models.session import Step

async def show():
    token = sys.argv[1] if len(sys.argv) > 1 else 'qnknbgfgtx4i'
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Step).where(Step.session_token == token).order_by(Step.step_number)
        )
        for s in result.scalars().all():
            act = s.action.value if hasattr(s.action, 'value') else s.action
            print(f"Step {s.step_number}: [{act}] selector={s.selector!r} prefill={s.prefill_value!r}")
            print(f"  >> {s.instruction}")

asyncio.run(show())
