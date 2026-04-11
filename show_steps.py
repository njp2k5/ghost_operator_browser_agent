import asyncio
from app.core.database import AsyncSessionLocal
from sqlalchemy import select
from app.models.session import Step

async def show():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Step).where(Step.session_token == 'qnknbgfgtx4i').order_by(Step.step_number)
        )
        for s in result.scalars().all():
            act = s.action.value if hasattr(s.action, 'value') else s.action
            print(f"Step {s.step_number}: [{act}] selector={s.selector!r} prefill={s.prefill_value!r}")
            print(f"  >> {s.instruction}")

asyncio.run(show())
