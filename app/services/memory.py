import json
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.supermemory.ai/v3"
HEADERS = {
    "Authorization": f"Bearer {settings.SUPERMEMORY_API_KEY}",
    "Content-Type": "application/json",
}


async def get_memory(user_id: str, task: str) -> dict | None:
    """
    Search Supermemory for a previously learned flow for this user+task.
    Returns the learned_flow dict if found, or None.
    """
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.post(
                f"{BASE_URL}/search",
                headers=HEADERS,
                json={
                    "q": f"user:{user_id} task:{task}",
                    "limit": 1,
                },
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            if results:
                content = results[0].get("content", "")
                # Content is stored as JSON string
                return json.loads(content)
    except Exception as e:
        logger.warning(f"Supermemory get_memory failed: {e}")
    return None


async def save_memory(user_id: str, task: str, steps: list[dict], prefill_values: dict) -> bool:
    """
    Save a completed task flow to Supermemory for future reuse.
    """
    payload = {
        "task": task,
        "steps": steps,
        "prefill": prefill_values,
    }
    content = json.dumps(payload)

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.post(
                f"{BASE_URL}/documents",
                headers=HEADERS,
                json={
                    "content": content,
                    "metadata": {
                        "user_id": user_id,
                        "task": task,
                        "type": "funclink_flow",
                    },
                },
            )
            response.raise_for_status()
            return True
    except Exception as e:
        logger.warning(f"Supermemory save_memory failed: {e}")
        return False
