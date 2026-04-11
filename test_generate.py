"""Direct test of the generate_link logic to expose the actual error."""
import asyncio, traceback, sys
sys.path.insert(0, '.')

async def test():
    from app.core.database import AsyncSessionLocal as async_session_factory
    from app.core.security import generate_session_token
    from app.models.session import Session, SessionStatus, Step, StepAction
    from app.services.llm import generate_steps
    from app.services.memory import get_memory
    from datetime import datetime, timezone

    user_id = "demo-user"
    task = (
        "Search for remote Python developer jobs on Indeed.com. "
        "Fill in 'Python Developer' as the job title and 'Remote' as the location, "
        "then click Find Jobs. After seeing results, use the 'Job Type' filter to select "
        "'Full-time', then use the 'Date posted' filter to show only jobs from the last "
        "7 days. Finally, click the top job listing to read the full job description."
    )
    target_url = "https://www.indeed.com"

    print("1. get_memory …")
    try:
        learned_flow = await get_memory(user_id, task)
        print(f"   learned_flow = {learned_flow}")
    except Exception as e:
        print(f"   ERROR: {e}")
        traceback.print_exc()
        return

    print("2. generate_steps …")
    try:
        steps_data = await generate_steps(task=task, context="", target_url=target_url, learned_flow=learned_flow)
        print(f"   steps count = {len(steps_data)}")
        for s in steps_data:
            print(f"   {s.get('step_number')}. [{s.get('action')}] sel={s.get('selector')!r}")
    except Exception as e:
        print(f"   ERROR: {e}")
        traceback.print_exc()
        return

    print("3. DB persist …")
    try:
        async with async_session_factory() as db:
            token = generate_session_token()
            session = Session(
                token=token, user_id=user_id, task=task, target_url=target_url,
                context="", status=SessionStatus.PENDING, current_step=1,
                created_at=datetime.now(timezone.utc),
            )
            db.add(session)
            for s in steps_data:
                action_str = s.get("action", "wait").lower()
                try:
                    action = StepAction(action_str)
                except ValueError:
                    action = StepAction.WAIT
                step = Step(
                    session_token=token,
                    step_number=s.get("step_number", 1),
                    action=action,
                    selector=s.get("selector"),
                    instruction=s.get("instruction", "Follow the instructions."),
                    prefill_value=s.get("prefill_value"),
                    url=s.get("url"),
                    is_skippable=s.get("is_skippable", False),
                    is_done=False,
                )
                db.add(step)
            await db.commit()
            print(f"   token = {token}   steps saved = {len(steps_data)}")
    except Exception as e:
        print(f"   ERROR: {e}")
        traceback.print_exc()

asyncio.run(test())
