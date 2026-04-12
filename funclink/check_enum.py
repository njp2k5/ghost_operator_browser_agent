import asyncio
from sqlalchemy import text
from app.core.database import engine

async def run():
    async with engine.connect() as c:
        res = await c.execute(text("SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid WHERE t.typname = 'stepaction'"))
        labels = [row[0] for row in res.fetchall()]
        print("Current enum values:", labels)
        
        for val in ['select', 'SELECT']:
            if val not in labels:
                print(f"Adding '{val}' to stepaction enum...")
                await c.execute(text(f"ALTER TYPE stepaction ADD VALUE IF NOT EXISTS '{val}'"))
                await c.commit()
                print(f"Added '{val}'")
        
        # Verify
        res = await c.execute(text("SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid WHERE t.typname = 'stepaction'"))
        labels = [row[0] for row in res.fetchall()]
        print("Final enum values:", labels)

asyncio.run(run())
