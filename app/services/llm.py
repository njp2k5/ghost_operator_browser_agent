import json
import re

from groq import AsyncGroq

from app.core.config import settings

client = AsyncGroq(api_key=settings.GROQ_API_KEY)

SYSTEM_PROMPT = """You are a web automation planner. Given a task description, generate a step-by-step browser automation plan as a JSON array.

Each step must follow this exact schema:
{
  "step_number": <int>,
  "action": <"navigate" | "highlight" | "fill" | "click" | "wait">,
  "selector": <CSS selector string or null>,
  "instruction": <short human-readable instruction shown to user>,
  "url": <full URL string if action is "navigate", else null>,
  "prefill_value": null
}

Rules:
- First step is always "navigate" with the target URL.
- Use simple, reliable CSS selectors (id > name > type).
- "instruction" must be short and friendly (max 10 words).
- Do NOT include markdown, code fences, or explanation — ONLY the raw JSON array.
- Sensitive actions (confirm payment, submit form) must be "click" not auto-filled.
"""

MEMORY_PROMPT = """You are a web automation planner. The user has done this task before.
Use the learned flow below to generate a SHORTENED step plan that skips already-known steps and pre-fills known values.

Learned flow:
{learned_flow}

Generate a minimal JSON array using the same schema. Mark skippable steps with is_skippable: true.
Do NOT include markdown or explanation — ONLY the raw JSON array.
"""


async def generate_steps(task: str, context: str, learned_flow: dict | None = None) -> list[dict]:
    """
    Call Groq Llama 3.1 8B to generate a step plan for the given task.
    If learned_flow is provided, generate a shortened plan using memory.
    """
    if learned_flow:
        user_content = (
            f"Task: {task}\nContext: {context}\n\n"
            f"Generate a shortened plan using the learned flow."
        )
        system = MEMORY_PROMPT.format(learned_flow=json.dumps(learned_flow, indent=2))
    else:
        user_content = f"Task: {task}\nContext: {context}"
        system = SYSTEM_PROMPT

    response = await client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=1024,
    )

    raw = response.choices[0].message.content.strip()
    steps = _parse_json_steps(raw)
    return steps


def _parse_json_steps(raw: str) -> list[dict]:
    """Robustly extract a JSON array from LLM output."""
    # Strip accidental markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()

    # Find the first [ ... ] block
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        raw = match.group(0)

    try:
        steps = json.loads(raw)
        if isinstance(steps, list):
            return steps
    except json.JSONDecodeError:
        pass

    # Fallback: return a single navigate + wait step so session never breaks
    return [
        {
            "step_number": 1,
            "action": "navigate",
            "selector": None,
            "instruction": "Opening the website...",
            "url": None,
            "prefill_value": None,
        },
        {
            "step_number": 2,
            "action": "wait",
            "selector": None,
            "instruction": "Follow the on-screen instructions.",
            "url": None,
            "prefill_value": None,
        },
    ]
