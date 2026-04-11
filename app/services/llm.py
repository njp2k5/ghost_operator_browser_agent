import json
import re

from groq import AsyncGroq

from app.core.config import settings

client = AsyncGroq(api_key=settings.GROQ_API_KEY)

SYSTEM_PROMPT = """You are a web automation planner. Given a task and the EXACT target URL, generate a step-by-step browser automation plan as a JSON array.

Each step must follow this exact schema:
{{
  "step_number": <int>,
  "action": <"navigate" | "fill" | "select" | "click" | "wait">,
  "selector": <label text or CSS selector — see rules>,
  "instruction": <short human-readable instruction shown to user>,
  "url": <full URL string if action is "navigate", else null>,
  "prefill_value": <value to pre-fill or select, or null>
}}

CRITICAL RULES:
- First step is ALWAYS "navigate" with url set to EXACTLY the target_url provided — do NOT change it.
- DO NOT generate separate "highlight" steps. Every field interaction must be a single step (fill, select, or click).
- For "fill" steps (text inputs, textareas): set selector to the VISIBLE LABEL TEXT next to the field.
  Example: if the form shows "Name *" as a label, use selector: "Name"
  Example: if the form shows "Mobile number *", use selector: "Mobile number"
  Example: if the form shows "E-mail address *", use selector: "E-mail address"
- For "select" steps (dropdowns / <select> elements): set selector to the LABEL TEXT, and prefill_value to the option text.
  Example: selector: "State", prefill_value: "Kerala"
  Example: selector: "Gender", prefill_value: "Male"  (also works for radio buttons)
  Example: selector: "Country", prefill_value: "India"
  Example: selector: "District", prefill_value: "Thiruvananthapuram"
- For "click" steps (buttons, links): use readable text or a simple CSS selector.
  Example: selector: "Submit" or selector: "button[type='submit']" or selector: "Sign In"
- "instruction" must be short and friendly (max 10 words).
- Radio buttons (like Gender: Male/Female) should use action "select" with the label and option value.
- Do NOT include markdown, code fences, or explanation — ONLY the raw JSON array.
- Keep the plan MINIMAL. One step per field. No redundant highlight steps.
"""

MEMORY_PROMPT = """You are a web automation planner. The user has done this task before.
Use the learned flow below to generate a SHORTENED step plan that skips already-known steps and pre-fills known values.

Learned flow:
{learned_flow}

Generate a minimal JSON array using the same schema. Mark skippable steps with is_skippable: true.
Do NOT include markdown or explanation — ONLY the raw JSON array.
"""


REPLAN_PROMPT = """You are a web automation planner. The user is in the middle of a task and the page has changed unexpectedly.

You must generate NEW remaining steps based on what is ACTUALLY visible on the page right now.

IMPORTANT: Do NOT repeat steps that were already completed. Do NOT include a "navigate" step — the user is already on the page.

Each step must follow this exact schema:
{{
  "step_number": <int — start from {next_step_number}>,
  "action": <"fill" | "select" | "click" | "wait">,
  "selector": <label text of the visible field>,
  "instruction": <short human-readable instruction shown to user>,
  "url": null,
  "prefill_value": <value to pre-fill or select, or null>
}}

RULES:
- ONLY use fields that are in the "Visible fields" list below. Do NOT invent fields that aren't there.
- For text inputs: action = "fill", selector = the label text.
- For dropdowns / radio buttons: action = "select", selector = label text, prefill_value = option text.
- For buttons: action = "click", selector = button text.
- Keep the plan MINIMAL. One step per visible field.
- Do NOT include markdown, code fences, or explanation — ONLY the raw JSON array.
"""


async def generate_steps(
    task: str,
    context: str,
    target_url: str = "",
    learned_flow: dict | None = None,
) -> list[dict]:
    """
    Call Groq Llama 3.1 8B to generate a step plan for the given task.
    If learned_flow is provided, generate a shortened plan using memory.
    """
    url_hint = f"\ntarget_url: {target_url}" if target_url else ""
    if learned_flow:
        user_content = (
            f"Task: {task}\nContext: {context}{url_hint}\n\n"
            f"Generate a shortened plan using the learned flow."
        )
        system = MEMORY_PROMPT.format(learned_flow=json.dumps(learned_flow, indent=2))
    else:
        user_content = f"Task: {task}\nContext: {context}{url_hint}"
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


async def replan_remaining_steps(
    task: str,
    completed_steps: list[dict],
    visible_fields: list[dict],
    page_url: str = "",
    next_step_number: int = 1,
) -> list[dict]:
    """
    Call the LLM to dynamically re-plan remaining steps based on what is
    ACTUALLY visible on the current page.  Used when the page changes
    unexpectedly (e.g. verification code screen, multi-step wizard).
    """
    # Build a readable summary of what was already done
    done_summary = "\n".join(
        f"  Step {s.get('step_number')}: [{s.get('action')}] selector=\"{s.get('selector', '')}\" — {s.get('instruction')}"
        for s in completed_steps
    )
    # Build a readable list of visible fields
    fields_summary = "\n".join(
        f"  - \"{f.get('label')}\" (type={f.get('type')}, tag={f.get('tag')}, inputType={f.get('inputType', '')})"
        for f in visible_fields
    )

    system = REPLAN_PROMPT.format(next_step_number=next_step_number)
    user_content = (
        f"Task: {task}\n"
        f"Current page URL: {page_url}\n\n"
        f"Steps already completed:\n{done_summary}\n\n"
        f"Visible fields on the current page:\n{fields_summary}\n\n"
        f"Generate the remaining steps starting from step_number {next_step_number}."
    )

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

    # Safety: renumber from next_step_number
    for i, s in enumerate(steps):
        s["step_number"] = next_step_number + i

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
