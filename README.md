# Ghost Operator — Browser Agent

A full-stack AI-powered WhatsApp assistant that routes natural-language messages to specialised browser automation and data-fetching tools. Users interact entirely through WhatsApp; the system transparently fetches real-time data, automates browser sessions, and returns formatted replies.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Repository Structure](#repository-structure)
- [How It Works](#how-it-works)
  - [Message Flow](#message-flow)
  - [Tool Routing](#tool-routing)
- [Components](#components)
  - [Python Backend (`app/`)](#python-backend-app)
  - [Node.js WhatsApp Bridge (`node-app/`)](#nodejs-whatsapp-bridge-node-app)
- [Available Tools](#available-tools)
- [WebSocket Endpoints](#websocket-endpoints)
- [REST Endpoints](#rest-endpoints)
- [Configuration & Environment Variables](#configuration--environment-variables)
- [Installation](#installation)
  - [Python Backend](#python-backend-setup)
  - [Node.js Bridge](#nodejs-bridge-setup)
- [Running the Project](#running-the-project)
- [FuncLink Integration](#funclink-integration)
- [Windows-Specific Notes](#windows-specific-notes)
- [Dependencies](#dependencies)

---

## Architecture Overview

```
WhatsApp User
      │  (sends message)
      ▼
┌─────────────────────┐
│  Node.js Bridge     │  whatsapp-web.js (Puppeteer)
│  node-app/          │  ← Authenticates via QR scan
│                     │  ← Per-sender message queue
└────────┬────────────┘
         │  WebSocket  (ws://...)
         ▼
┌─────────────────────┐
│  Python FastAPI     │  uvicorn + asyncio
│  app/               │
│  /ws/{sender}       │  ← Main conversational WebSocket
│                     │
│  LLM Tool Router    │  ← Groq API (llama-3.x)
│  Tool Registry      │  ← Pluggable tool system
│  Browser Services   │  ← Playwright automations
└─────────────────────┘
         │
         ├─ Groq LLM (tool routing + response generation)
         ├─ Playwright (Amazon, LinkedIn, IRCTC browser automation)
         ├─ httpx / BeautifulSoup (OLX, Housing, Practo, Hindu News)
         ├─ IRCTC RapidAPI (live train data)
         └─ FuncLink (guided browser sessions)
```

The Node.js bridge is a thin adapter: it receives WhatsApp messages, opens a per-sender persistent WebSocket to the Python backend, and sends replies back to WhatsApp. All intelligence lives in the Python layer.

---

## Repository Structure

```
ghost_operator_browser_agent/
├── app/                          # Python FastAPI backend
│   ├── main.py                   # App entry point, route registration
│   ├── main_with_tools.py        # Alternative entry that preloads tool modules
│   ├── requirements.txt          # Python dependencies
│   │
│   ├── core/
│   │   ├── config.py             # Environment variable loading
│   │   └── websocket_manager.py  # Per-sender WebSocket connection registry
│   │
│   ├── models/
│   │   └── schemas.py            # Pydantic models (ChatMessage)
│   │
│   ├── api/
│   │   ├── ws.py                 # Main /ws/{sender} conversational endpoint + LLM router
│   │   ├── housing_ws.py         # /ws/housing — MagicBricks property search
│   │   ├── hindu_ws.py           # /ws/hindu/news — The Hindu RSS news
│   │   ├── irctc_ws.py           # /ws/irctc — IRCTC train/PNR search
│   │   ├── linkedin_ws.py        # /ws/olx — OLX India listings
│   │   └── practo_ws.py          # /ws/practo — Practo doctor finder
│   │
│   ├── services/
│   │   ├── llm_service.py        # Groq API client wrapper
│   │   ├── memory_service.py     # In-memory per-sender chat history
│   │   ├── funclink_service.py   # FuncLink guided session API client
│   │   ├── irctc_browser_service.py  # Playwright-based IRCTC scraper (public web fallback)
│   │   └── irctc_live_service.py     # RapidAPI-based IRCTC live data client
│   │
│   └── tool_registry/
│       ├── __init__.py           # load_builtin_tools() — auto-imports all tool modules
│       ├── loader.py             # Explicit loader for specific tool modules
│       ├── registry.py           # Central TOOL_REGISTRY dict, register/get/list
│       ├── executor.py           # execute_tool() — safe async dispatch
│       └── tools/
│           ├── amazon_account.py # Playwright Amazon login + order history tool
│           ├── amazon_search.py  # Playwright Amazon product search tool
│           ├── housing_listings.py  # MagicBricks property scraper tool
│           ├── linkedin_leads.py    # LinkedIn people search tool (Playwright)
│           └── practo_doctors.py   # Practo doctor scraper tool (requests + BS4)
│
└── node-app/                     # Node.js WhatsApp bridge
    ├── package.json
    └── src/
        ├── index.js              # Entrypoint — create client & start
        ├── config/
        │   └── env.js            # dotenv loader, WS_BASE_URL, HEADLESS
        ├── handlers/
        │   └── messageHandler.js # Route incoming WhatsApp msgs → WS backend
        ├── utils/
        │   └── logger.js         # Structured timestamped console logger
        ├── websocket/
        │   ├── wsManager.js      # Per-sender WS connections with auto-reconnect
        │   └── queueManager.js   # Per-sender p-queue (concurrency=1, rate-limited)
        └── whatsapp/
            ├── client.js         # whatsapp-web.js Client factory with QR + events
            └── messageFormatter.js  # WhatsApp reply formatter (chunking, callouts)
```

---

## How It Works

### Message Flow

1. **User sends a WhatsApp message** to the bot's number.
2. The **Node.js bridge** (`whatsapp-web.js`) receives the message.
3. It looks up (or creates) a **per-sender WebSocket connection** to `ws://backend/ws/{sender}`.
4. Messages for the same sender are **queued** (concurrency 1, rate-limited to 2/second) to prevent flooding.
5. The message payload `{ message: "..." }` is sent over the WebSocket.
6. The **Python FastAPI backend** receives it on `/ws/{sender}`.
7. The backend prepends the message to the sender's **chat history** and calls the **LLM tool router**.
8. The LLM (Groq `llama-3.x`) decides whether to invoke a tool or respond conversationally.
9. If a tool is selected, it is dispatched via `execute_tool()`.
10. The tool runs (browser automation, HTTP scraping, API call) and returns structured data.
11. The LLM generates a **WhatsApp-friendly reply** from the tool result.
12. The reply is sent back over the WebSocket as `{ "reply": "..." }`.
13. The Node.js bridge receives the JSON reply and **formats it into WhatsApp chunks** (max 3200 chars each), sending each chunk sequentially.

### Tool Routing

The main `/ws/{sender}` handler uses a two-step LLM interaction:

1. **Routing prompt**: Sends the user's message + list of all available tool schemas to the LLM. The LLM returns strict JSON: `{ use_tool, tool, params, reason }`.
2. **Response generation**: After tool execution, sends the structured result back to the LLM to generate a conversational, WhatsApp-formatted reply.

If `use_tool` is `false`, the LLM responds directly from chat history without tool invocation.

---

## Components

### Python Backend (`app/`)

#### `core/config.py`
Loads all configuration from environment variables via `python-dotenv`. Exposes:
- `GROQ_API_KEY`, `MODEL_NAME` — LLM configuration
- `IRCTC_RAPIDAPI_KEY`, `IRCTC_RAPIDAPI_HOST`, and path constants for the IRCTC RapidAPI

#### `core/websocket_manager.py`
`ConnectionManager` — a singleton `manager` object that keeps a dict of active `{sender → WebSocket}` connections. Used by the FuncLink webhook to push proactive messages to connected users.

#### `services/llm_service.py`
Thin wrapper around the [Groq Python SDK](https://github.com/groq/groq-python). Lazily initialises the client on first use. Exposes a synchronous `generate(messages, temperature, max_tokens)` helper.

#### `services/memory_service.py`
In-memory chat history store. Keyed by sender ID. Each entry is a `{"role": ..., "content": ...}` dict compatible with the OpenAI/Groq messages format. **Note**: history is lost on server restart (no persistence layer).

#### `services/funclink_service.py`
Async HTTP client that calls the external **FuncLink** service (`https://funclinkbackend-production.up.railway.app/generate-link`). FuncLink generates a guided browser session URL so the user can perform complex website tasks (hotel booking, flight search, etc.) with step-by-step guidance.

#### `services/irctc_live_service.py`
Calls the **IRCTC RapidAPI** for real-time train and PNR data. Handles station code resolution (name → code), date normalisation (YYYY-MM-DD ↔ DD-MM-YYYY), and response normalisation across different API response shapes.

#### `services/irctc_browser_service.py`
Playwright-based fallback for IRCTC when the live API is unavailable. Scrapes DuckDuckGo for IRCTC-trusted domains (`irctc.co.in`, `confirmtkt.com`, `railyatri.in`, `ixigo.com`, `railmitra.com`). Also contains a headless Playwright flow that navigates the IRCTC site directly for PNR status lookups. Includes a `_run_in_proactor_thread` helper to work around Windows `SelectorEventLoop` limitations.

#### `tool_registry/`
A lightweight, self-registering tool plugin system:

| File | Responsibility |
|------|---------------|
| `registry.py` | Global `TOOL_REGISTRY` dict. `register_tool(definition, run_fn)` adds a tool; `get_tool(name)` retrieves it; `list_tools()` returns all definitions for the LLM prompt. |
| `__init__.py` | `load_builtin_tools()` — iterates `tool_registry/tools/` and imports every non-private module, triggering self-registration via module-level side effects. |
| `loader.py` | Alternative explicit loader used by `main_with_tools.py`. |
| `executor.py` | `execute_tool(name, params)` — validates tool existence, calls `tool["run"](params)`, wraps exceptions, normalises to `{ success, tool, ...result }`. |

Each tool module defines a `tool_definition` dict and a `run(params)` async function, then calls `register_tool(tool_definition, run)` at module level.

---

### Node.js WhatsApp Bridge (`node-app/`)

#### `src/whatsapp/client.js`
Creates a `whatsapp-web.js` `Client` with `LocalAuth` (stores session in `.wwebjs_auth/`). On first run, prints a QR code to the terminal for scanning. Listens to `qr`, `ready`, `message`, and `disconnected` events.

#### `src/handlers/messageHandler.js`
- Filters out self-sent messages and group chats.
- Looks up (or creates) the sender's p-queue and enqueues an async task.
- The task calls `sendWSMessage` with the message text.
- On reply, calls `formatWhatsAppReply` to split long responses into ≤3200-char chunks and sends each chunk via `client.sendMessage`.
- On failure, sends a formatted fallback error message.

#### `src/websocket/wsManager.js`
Manages persistent WebSocket connections keyed by sender ID:
- Opens a new `ws://` connection if none exists or if the existing one is closed/closing.
- Sends a **ping every 20 seconds** to keep connections alive.
- **Auto-reconnects after 2 seconds** on disconnect.
- `sendWSMessage(sender, payload, onMessage)` — ensures the connection is open before sending.

#### `src/websocket/queueManager.js`
`getQueue(sender)` — returns (or creates) a `PQueue` for the sender with `concurrency: 1` and a rate cap of 2 tasks/second. Prevents message flooding and ensures ordered delivery per sender.

#### `src/whatsapp/messageFormatter.js`
Rich reply formatter for WhatsApp:
- Splits replies at `MAX_MESSAGE_LENGTH` (3200 chars) on paragraph boundaries.
- Detects labelled callouts (`tip:`, `note:`, `warning:`, `error:`, etc.) and formats them with emoji icons.
- Handles bullet lists, numbered lists, dividers, and plain text blocks.
- Produces WhatsApp-safe Unicode text (no Markdown that WhatsApp won't render as intended).

#### `src/config/env.js`
Loads `.env` and exports:
- `WS_BASE_URL` — WebSocket base URL for the Python backend (default: `ws://localhost:8000/ws`)
- `HEADLESS` — Boolean, controls whether Puppeteer (whatsapp-web.js) runs headless

#### `src/utils/logger.js`
Simple structured logger with `[ISO-timestamp] [LEVEL]` prefix. Outputs to standard streams (stdout/stderr).

---

## Available Tools

All tools are registered in the tool registry and exposed to the LLM router. The LLM selects the appropriate tool and parameters based on the user's natural-language message.

| Tool | Module | Description |
|------|--------|-------------|
| `amazon_search` | `tools/amazon_search.py` | Searches Amazon India (or any Amazon marketplace) for products using Playwright. Returns title, price, rating, and product URL. |
| `amazon_account` | `tools/amazon_account.py` | Multi-turn guided Amazon login over WhatsApp chat. Handles email → password → OTP/CAPTCHA → order history fetching. Uses a persistent dedicated ProactorEventLoop thread for stateful Playwright sessions. Supports pre-loaded storage state (cookie export) for headless deployments. |
| `housing_listings` | `tools/housing_listings.py` | Scrapes MagicBricks for rental or sale property listings. Accepts city, query keywords, purpose (rent/buy), price range, and BHK filters. Falls back to direct MagicBricks URLs if scraping is blocked. |
| `linkedin_leads` | `tools/linkedin_leads.py` | Searches LinkedIn people results using Playwright. Returns name, headline, and profile URL. (Note: requires LinkedIn to be accessible without login for public search.) |
| `practo_doctors` | `tools/practo_doctors.py` | Scrapes Practo for doctor listings by city, speciality, and locality using `requests` + BeautifulSoup. Ranks results by keyword match score. |
| `olx_search` | Inline in `api/ws.py` (via `api/linkedin_ws.py`) | Searches OLX India via DuckDuckGo `site:olx.in` query (direct OLX access is blocked by Cloudflare). Returns title, price, and listing URL. |
| `irctc_search` | Inline in `api/ws.py` (via `api/irctc_ws.py`) | Handles train searches and PNR status. Routes to RapidAPI live service if configured, falls back to public web search scraping via `IRCTCBrowserService`. |
| `hindu_news` | Inline in `api/ws.py` (via `api/hindu_ws.py`) | Fetches headlines from The Hindu RSS feeds. Supports 20+ sections (national, business, sport, technology, etc.). |
| `housing_search` | Inline in `api/ws.py` | Alias tool — routes WhatsApp housing queries to the `housing_listings` registered tool. |
| `practo_search` | Inline in `api/ws.py` | Alias tool — routes WhatsApp doctor queries to the `practo_doctors` registered tool. |
| `funclink_guide` | Inline in `api/ws.py` | Calls FuncLink to generate a guided browser session URL for any external website task (booking, form-filling, etc.). |

---

## WebSocket Endpoints

| Path | Description |
|------|-------------|
| `/ws/{sender}` | **Main conversational endpoint.** Accepts `{ "message": "..." }` JSON. LLM-routed. Returns `{ "reply": "..." }`. This is the primary endpoint used by the Node.js bridge. |
| `/ws/housing` | Interactive MagicBricks search session. Prompts for city, query, purpose, and limit step-by-step. |
| `/ws/hindu/news` | Interactive The Hindu news session. Prompts for section and article count. |
| `/ws/irctc` | Interactive IRCTC search session. Detects train search vs. PNR status intent automatically. |
| `/ws/olx` | Interactive OLX listings session. Prompts for query and result limit. |
| `/ws/practo` | Interactive Practo doctor search session. Prompts for city, speciality, locality, and limit. |

All WebSocket message exchanges use JSON frames. Interactive sessions (`/ws/housing`, `/ws/hindu/news`, etc.) use staged prompts with a `stage` field in the response to indicate the current phase (e.g., `"init"`, `"results"`, `"error"`, `"done"`).

---

## REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check. Returns `{ "status": "ok" }`. |
| `POST` | `/webhook/funclink` | Receives session-complete callbacks from FuncLink. Pushes a completion message to the user's active WebSocket connection. Payload: `{ user_id, status, task, token }`. |
| `POST` | `/test/tool` | Direct tool execution for testing. Payload: `{ "tool": "tool_name", "params": {...} }`. |

---

## Configuration & Environment Variables

### Python Backend (`app/.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GROQ_API_KEY` | **Yes** | — | Groq API key for LLM inference |
| `MODEL_NAME` | No | `llama-3.1-8b-instant` | Groq model name |
| `IRCTC_RAPIDAPI_KEY` | No | — | RapidAPI key for live IRCTC data. Falls back to web scraping if absent. |
| `IRCTC_RAPIDAPI_HOST` | No | `irctc1.p.rapidapi.com` | RapidAPI host |
| `IRCTC_API_BASE_URL` | No | `https://irctc1.p.rapidapi.com` | IRCTC API base URL |
| `IRCTC_TRAIN_BETWEEN_PATH` | No | `/api/v3/trainBetweenStations` | Endpoint path for train search |
| `IRCTC_PNR_PATH` | No | `/api/v3/getPNRStatus` | Endpoint path for PNR status |
| `IRCTC_STATION_SEARCH_PATH` | No | `/api/v1/searchStation` | Endpoint path for station search |
| `AMAZON_STORAGE_STATE_B64` | No | — | Base64-encoded Playwright storage state JSON for pre-authenticated Amazon sessions |
| `AMAZON_STORAGE_STATE_PATH` | No | — | Path to Playwright storage state JSON file |

### Node.js Bridge (`node-app/.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WS_BASE_URL` | No | `ws://localhost:8000/ws` | WebSocket base URL of the Python backend |
| `HEADLESS` | No | `false` | Set to `true` to run Puppeteer (WhatsApp client) headless (requires pre-existing session) |

---

## Installation

### Python Backend Setup

**Requirements**: Python 3.11+, pip

```bash
cd app

# Create and activate virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Create environment file
copy .env.example .env    # Windows
# cp .env.example .env    # Linux/macOS
# Then edit .env with your API keys
```

### Node.js Bridge Setup

**Requirements**: Node.js 18+, npm

```bash
cd node-app

# Install dependencies
npm install

# Create environment file
# Create node-app/.env:
# WS_BASE_URL=ws://localhost:8000/ws
# HEADLESS=false
```

---

## Running the Project

Start both services. The Python backend must be running before the Node.js bridge connects.

**1. Start the Python FastAPI backend:**

```bash
cd app
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Or using the tool-preloading entry point:

```bash
cd app
uvicorn main_with_tools:app --host 0.0.0.0 --port 8000 --reload
```

**2. Start the Node.js WhatsApp bridge:**

```bash
cd node-app
npm start
```

On first run, a QR code will appear in the terminal. Scan it with WhatsApp (Settings → Linked Devices → Link a Device). The session is saved locally and reused on subsequent runs.

**3. Test the backend directly:**

```bash
# Health check
curl http://localhost:8000/

# Test a tool directly
curl -X POST http://localhost:8000/test/tool \
  -H "Content-Type: application/json" \
  -d '{"tool": "housing_listings", "params": {"city": "Bengaluru", "purpose": "rent", "limit": 3}}'
```

---

## FuncLink Integration

[FuncLink](https://funclinkbackend-production.up.railway.app) is an external service that generates guided browser session links. When a user asks to be walked through a task on an external website (e.g., "guide me through booking a hotel on Booking.com"), the `funclink_guide` tool:

1. POSTs to `/generate-link` with `{ user_id, task, target_url, context }`.
2. Receives a session URL and token.
3. Sends the URL to the user via WhatsApp.

When the FuncLink session completes, it POSTs back to `/webhook/funclink` on the Ghost Operator backend with `{ user_id, status: "complete", task, token }`. The backend then pushes a completion confirmation to the user's active WhatsApp chat.

---

## Windows-Specific Notes

Playwright requires a **ProactorEventLoop** to spawn browser subprocesses on Windows. Uvicorn uses a `SelectorEventLoop` by default, which is incompatible. Two strategies are used:

1. **`main.py` top-level**: Sets `asyncio.WindowsProactorEventLoopPolicy()` before the app is created.
2. **`irctc_browser_service.py`**: `_run_in_proactor_thread()` offloads Playwright coroutines to a dedicated thread with a fresh `ProactorEventLoop`.
3. **`amazon_account.py`**: Maintains a single long-lived `ProactorEventLoop` in a daemon background thread (`amazon-account-worker`). All Amazon browser sessions are dispatched via `run_coroutine_threadsafe` to this dedicated loop so stateful `AmazonSession` objects remain valid across multiple conversational turns.

---

## Dependencies

### Python (`app/requirements.txt`)

| Package | Purpose |
|---------|---------|
| `fastapi` | Async web framework, WebSocket support |
| `uvicorn[standard]` | ASGI server |
| `pydantic` | Data validation and settings |
| `groq` | Groq LLM API client |
| `python-dotenv` | `.env` file loading |
| `playwright` | Browser automation (Chromium) |
| `httpx` | Async HTTP client |
| `beautifulsoup4` | HTML parsing for scraping |
| `requests` | Sync HTTP client (Practo, Housing) |

### Node.js (`node-app/package.json`)

| Package | Purpose |
|---------|---------|
| `whatsapp-web.js` | WhatsApp Web automation via Puppeteer |
| `ws` | WebSocket client for Python backend connection |
| `p-queue` | Per-sender message queue with concurrency control |
| `qrcode-terminal` | QR code display in terminal for WhatsApp auth |
| `dotenv` | Environment variable loading |
