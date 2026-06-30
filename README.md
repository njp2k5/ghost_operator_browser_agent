<div align="center">
  <br />
  <h1>👻 Ghost Operator — Browser Agent</h1>
  <p>
    <strong>A full-stack, AI-powered WhatsApp assistant that autonomously automates browsers and fetches real-time data.</strong>
  </p>
</div>

<br />

Ghost Operator is an intelligent assistant that lives right inside your WhatsApp. Built with **FastAPI**, **Node.js**, **Playwright**, and the **Groq LLM (llama-3.x)**, it seamlessly routes your natural-language text messages to specialized browser automation tools.

Whether you want to search Amazon, fetch live news, scrape MagicBricks for properties, find a doctor on Practo, or check your IRCTC train status—Ghost Operator handles it all transparently, entirely via WhatsApp.

---

## ✨ See It In Action

Experience seamless conversational data retrieval and multi-turn browser sessions directly in your chat.

<p align="center">
  <img src="assets/amazon_search.jpg" alt="Amazon Product Search" width="32%" />
  <img src="assets/amazon_login.jpg" alt="Amazon Guided Login" width="32%" />
  <img src="assets/fetch_properties.jpeg" alt="MagicBricks Property Search" width="32%" />
</p>

<p align="center">
  <img src="assets/fetch_doctors.jpeg" alt="Practo Doctor Search" width="48%" />
  <img src="assets/fetch_news.jpeg" alt="The Hindu News" width="48%" />
</p>

---

## 🏗️ Architecture & Flow

Ghost Operator pairs a **Node.js WhatsApp Bridge** with a high-performance **Python FastAPI Backend**. The Node.js bridge simply acts as a thin adapter for WhatsApp Web, while all the intelligence, tool execution, and LLM orchestration happens in Python over a per-sender WebSocket connection.

### System Architecture
<div align="center">
  <img src="assets/architecture_diagram.png" alt="Architecture Diagram" width="90%" />
</div>

<br/>

### User Flow
<div align="center">
  <img src="assets/user_flow.png" alt="User Flow Diagram" width="90%" />
</div>

1. **User sends a WhatsApp message** to the bot's number.
2. The **Node.js Bridge** (`whatsapp-web.js`) enqueues it to prevent flooding and sends it via WebSocket to `ws://backend/ws/{sender}`.
3. The **FastAPI Backend** appends the message to the sender's history and invokes the **LLM Tool Router**.
4. The **Groq LLM** decides whether to use a tool (like Playwright Amazon automation) or reply conversationally.
5. The chosen tool runs (web scraping, API call, headless browser) and returns structured data.
6. The LLM formats this data into a friendly, chunked response (≤3200 chars).
7. The reply is sent back through the WebSocket and delivered to the user via WhatsApp.

---

## 🛠️ Key Capabilities & Tools

All tools are built into a pluggable `tool_registry/` framework, making it trivially easy to add new capabilities.

- 🛒 **Amazon Search & Account (`amazon_search.py`, `amazon_account.py`)**: Uses a persistent, dedicated ProactorEventLoop thread for stateful Playwright sessions. Can search products and even guide users through interactive Amazon logins over WhatsApp chat.
- 🏠 **MagicBricks Properties (`housing_listings.py`)**: Scrapes rental/sale property listings by city, budget, and BHK filters.
- 🧑‍⚕️ **Practo Doctors (`practo_doctors.py`)**: Scrapes Practo using `requests` + `BeautifulSoup` to find doctors by city, speciality, and locality, ranking them by relevance.
- 💼 **LinkedIn Leads (`linkedin_leads.py`)**: Executes Playwright-driven public LinkedIn searches to find profiles and headlines.
- 📰 **The Hindu News (`hindu_ws.py`)**: Interactive news fetcher leveraging RSS feeds across 20+ sections (national, business, sport).
- 🚆 **IRCTC Train Status (`irctc_live_service.py`)**: Checks live train data and PNR status. Automatically falls back to DuckDuckGo-powered scraping of third-party portals if the live RapidAPI is down.
- 🔗 **FuncLink Guide (`funclink_service.py`)**: Integrates with [FuncLink](https://funclinkbackend-production.up.railway.app/) to generate guided browser session URLs for complex tasks (like hotel bookings) directly in the chat!

---

## 🚀 Getting Started

To run Ghost Operator, you need to spin up both the **Python Backend** and the **Node.js Bridge**.

### 1. Python FastAPI Backend

**Requirements:** Python 3.11+, Playwright dependencies

```bash
cd app

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # (On Windows use: .venv\Scripts\activate)

# Install requirements
pip install -r requirements.txt
playwright install chromium

# Setup environment variables
cp .env.example .env
# Make sure to edit .env to include your GROQ_API_KEY
```

**Start the Server:**
```bash
# Standard startup
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Or use the pre-loading entry point
uvicorn main_with_tools:app --host 0.0.0.0 --port 8000 --reload
```

### 2. Node.js WhatsApp Bridge

**Requirements:** Node.js 18+

```bash
cd node-app

# Install packages
npm install

# Setup environment variables
# Create a .env file containing:
# WS_BASE_URL=ws://localhost:8000/ws
# HEADLESS=false
```

**Start the Bridge:**
```bash
npm start
```

*On the first run, a QR code will be printed to your terminal. Scan it via WhatsApp (Settings → Linked Devices) to authenticate the bot. The session is saved locally in `.wwebjs_auth/`.*

---

## 🖥️ Windows-Specific Notes

Ghost Operator uses **Playwright** heavily for tools like Amazon and LinkedIn. Playwright on Windows requires a `ProactorEventLoop`, while Uvicorn uses `SelectorEventLoop` by default. 

To solve this, the backend utilizes two strategies:
1. `main.py` explicitly sets `asyncio.WindowsProactorEventLoopPolicy()` before startup.
2. Stateful browser sessions (like `amazon-account-worker`) are offloaded to dedicated background daemon threads with their own persistent `ProactorEventLoop`.

---

<div align="center">
  <i>Built with FastAPI, whatsapp-web.js, Playwright, and Groq.</i>
</div>
