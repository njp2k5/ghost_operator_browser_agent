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


SCAN_BASED_SYSTEM = """You are a web automation planner.
The user wants to: {task}
The browser is currently on: {target_url}

These are the EXACT interactive fields and buttons visible on the page RIGHT NOW:
{fields_summary}

Generate a minimal step-by-step JSON array to complete the task using ONLY the fields listed above.
Do NOT invent selectors that are not in the list.

Each step must follow this exact schema:
{{
  "step_number": <int, starting from {next_step_number}>,
  "action": <"fill" | "select" | "click" | "wait">,
  "selector": <EXACT label text from the visible fields list — no rewording>,
  "instruction": <short friendly instruction, max 12 words>,
  "url": null,
  "prefill_value": <the value to pre-fill, or null>
}}

RULES:
- selector MUST exactly match a label from the visible fields list above.
- Do NOT add a navigate step — the user is already on the page.
- For text inputs: action = "fill".
- For dropdowns / radio buttons: action = "select", set prefill_value = the option to pick.
- For buttons/links: action = "click".
- Keep the plan MINIMAL — one step per field.
- IMPORTANT: For search boxes (type=search, or label/placeholder contains "search"), do NOT add a separate "click" step for the search button — pressing Enter is handled automatically after typing.
- Do NOT include markdown, code fences, or explanation — ONLY the raw JSON array.
"""


async def generate_steps_from_scan(
    task: str,
    target_url: str,
    visible_fields: list[dict],
    next_step_number: int = 2,
) -> list[dict]:
    """
    Generate steps based on what is ACTUALLY visible on the page (scan-first approach).
    Far more accurate than generating from the task description alone.
    """
    fields_summary = "\n".join(
        f"  - label=\"{f.get('label')}\"  type={f.get('type')}  tag={f.get('tag')}"
        for f in visible_fields
        if f.get("label")
    ) or "  (no interactive fields detected)"

    system = SCAN_BASED_SYSTEM.format(
        task=task,
        target_url=target_url,
        fields_summary=fields_summary,
        next_step_number=next_step_number,
    )

    response = await client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"Generate steps for: {task}"},
        ],
        temperature=0.1,
        max_tokens=1024,
    )
    raw = response.choices[0].message.content.strip()
    steps = _parse_json_steps(raw)
    # Ensure step numbers start from next_step_number
    for i, s in enumerate(steps):
        s["step_number"] = next_step_number + i
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
