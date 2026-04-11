"""
FuncLink Demo Generator
=======================
Generates shareable demo links on BOT-FRIENDLY public sites.
No login pages, no captchas, no Cloudflare.

Run while the server is on port 8000:
    python demo_gen.py        # default = Demo 1
    python demo_gen.py 2      # pick a specific demo

Then open the printed URL in a browser (or share it).
"""

import httpx, sys

# ──────────────────────────────────────────────────────────────────────────────
# DEMO 1 — DemoQA Practice Form  ★ BEST FOR DEMOS
# A professional-looking form with text inputs, radio buttons, dropdowns,
# checkboxes, and a submit button.  Zero bot-detection.
# Shows: FILL × 4 → SELECT (radio) → FILL → SELECT (dropdown) × 2 → CLICK
# ──────────────────────────────────────────────────────────────────────────────
DEMO_FORM = {
    "user_id": "demo-user",
    "task": (
        "Fill out the student registration form on DemoQA. "
        "Enter 'John' as the First Name and 'Smith' as the Last Name. "
        "Enter 'john.smith@example.com' as the Email. "
        "Select 'Male' for Gender. "
        "Enter '9876543210' as the Mobile number. "
        "Enter '15 Jan 1995' as the Date of Birth. "
        "Type 'Maths' in the Subjects field. "
        "Select 'Sports' for Hobbies. "
        "Type '123 Marine Drive, Mumbai' in the Current Address field. "
        "Select 'NCR' as the State and 'Delhi' as the City. "
        "Finally click the Submit button."
    ),
    "target_url": "https://demoqa.com/automation-practice-form",
}

# ──────────────────────────────────────────────────────────────────────────────
# DEMO 2 — Wikipedia Search
# Everyone recognises Wikipedia.  Simple fill → click → navigate flow.
# Shows: FILL → CLICK → CLICK (article link)
# ──────────────────────────────────────────────────────────────────────────────
DEMO_WIKI = {
    "user_id": "demo-user",
    "task": (
        "Search for 'Artificial Intelligence' on Wikipedia. "
        "Type 'Artificial Intelligence' in the search box and click the search button. "
        "Once the article opens, click the 'History of artificial intelligence' link "
        "in the article body to read about its history."
    ),
    "target_url": "https://en.wikipedia.org/wiki/Main_Page",
}

# ──────────────────────────────────────────────────────────────────────────────
# DEMO 3 — Books to Scrape (e-commerce browsing)
# A clean, static e-commerce catalogue.  No JS frameworks, no bot detection.
# Shows: NAVIGATE → CLICK (category) → CLICK (book) → read details
# ──────────────────────────────────────────────────────────────────────────────
DEMO_BOOKS = {
    "user_id": "demo-user",
    "task": (
        "Browse the online bookstore at books.toscrape.com. "
        "Click on the 'Science' category in the sidebar. "
        "Then click the first book in the list to see its details. "
        "Note the price and availability shown on the book page."
    ),
    "target_url": "https://books.toscrape.com",
}

# ──────────────────────────────────────────────────────────────────────────────
# DEMO 4 — SauceDemo (standard QA test e-commerce with public demo creds)
# Shows fill + click + product browsing + cart.
# ──────────────────────────────────────────────────────────────────────────────
DEMO_SAUCE = {
    "user_id": "demo-user",
    "task": (
        "Log in to the Sauce Labs demo store using the test credentials: "
        "username 'standard_user' and password 'secret_sauce'. "
        "After logging in, click on the 'Sauce Labs Backpack' product. "
        "Then click the 'Add to cart' button. "
        "Finally click the shopping cart icon to view the cart."
    ),
    "target_url": "https://www.saucedemo.com",
}

# ──────────────────────────────────────────────────────────────────────────────
# Choose which demo to generate
# ──────────────────────────────────────────────────────────────────────────────
DEMOS = {
    "1": ("DemoQA Practice Form", DEMO_FORM),
    "2": ("Wikipedia Search",     DEMO_WIKI),
    "3": ("Books to Scrape",      DEMO_BOOKS),
    "4": ("SauceDemo E-Commerce", DEMO_SAUCE),
}

choice = sys.argv[1] if len(sys.argv) > 1 else "1"
if choice not in DEMOS:
    print("Usage: python demo_gen.py [1|2|3|4]")
    for k, (name, _) in DEMOS.items():
        print(f"  {k} - {name}")
    sys.exit(1)

demo_name, ACTIVE_DEMO = DEMOS[choice]

# ──────────────────────────────────────────────────────────────────────────────
print(f"\n🚀  Generating FuncLink demo link — {demo_name}")
print(f"   Task  : {ACTIVE_DEMO['task'][:90]}...")
print(f"   Target: {ACTIVE_DEMO['target_url']}\n")

try:
    r = httpx.post(
        "http://127.0.0.1:8000/generate-link",
        json=ACTIVE_DEMO,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("token", "-")
    steps = data.get("step_count", "?")
    link  = f"http://127.0.0.1:8000/f/{token}"

    print(f"✅  Link generated!")
    print(f"   Token      : {token}")
    print(f"   Steps      : {steps}")
    print(f"   Demo URL   : {link}")
    print(f"\n   Open in browser or share via WhatsApp.\n")

except httpx.HTTPStatusError as e:
    print(f"❌  HTTP error {e.response.status_code}: {e.response.text}")
except Exception as e:
    print(f"❌  Error: {e}")
