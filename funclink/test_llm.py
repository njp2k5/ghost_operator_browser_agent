import asyncio
import sys
sys.path.insert(0, '.')

async def test():
    from app.services.llm import generate_steps
    steps = await generate_steps(
        task=(
            "Search for remote Python developer jobs on Indeed.com. "
            "Fill in Python Developer as the job title and Remote as the location, "
            "then click Find Jobs. After seeing results, use the Job Type filter to select "
            "Full-time, then use the Date posted filter to show only jobs from the last "
            "7 days. Finally, click the top job listing to read the full job description."
        ),
        context="",
        target_url="https://www.indeed.com",
    )
    print("Steps generated:", len(steps))
    for s in steps:
        num = s.get("step_number")
        act = s.get("action")
        ins = s.get("instruction")
        sel = s.get("selector")
        print(f"  {num}. [{act}] sel={sel!r}  |  {ins}")

asyncio.run(test())
