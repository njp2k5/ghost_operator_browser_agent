import asyncio
from app.core.database import AsyncSessionLocal
from sqlalchemy import text

TOKEN = "blp25pst6d4o"

async def check():
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            text("SELECT token, task, target_url, status FROM sessions WHERE token=:tok"),
            {"tok": TOKEN},
        )
        row = r.fetchone()
        if row:
            print("Session:", dict(row._mapping))
        else:
            print("Session NOT FOUND for token:", TOKEN)

        r2 = await db.execute(
            text("SELECT step_number, action, selector, instruction, url FROM steps WHERE session_token=:tok ORDER BY step_number"),
            {"tok": TOKEN},
        )
        steps = r2.fetchall()
        if steps:
            for s in steps:
                print(f"  Step {s.step_number}: action={s.action}  selector={s.selector}  url={s.url}")
        else:
            print("  No steps found")

asyncio.run(check())
