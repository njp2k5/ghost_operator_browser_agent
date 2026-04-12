"""
FuncLink launcher — sets the correct Windows event loop policy
BEFORE uvicorn creates its event loop, then starts the server.

Usage:  python run.py
        python run.py --reload
"""
import asyncio
import sys

# Must happen BEFORE uvicorn touches asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    reload_flag = "--reload" in sys.argv
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=reload_flag,
    )
