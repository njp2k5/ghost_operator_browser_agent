"""Quick WebSocket test — simulates what the browser does."""
import asyncio
import json
import sys

import httpx
import websockets


async def main():
    token = sys.argv[1] if len(sys.argv) > 1 else "mnglkf6cul3s"

    # 1. Hit guide page (resets session to PENDING)
    r = httpx.get(f"http://localhost:8000/f/{token}", timeout=10, follow_redirects=True)
    print(f"Guide page HTTP {r.status_code}")

    # 2. Connect WebSocket
    uri = f"ws://localhost:8000/ws/{token}"
    print(f"Connecting to {uri} ...")

    async with websockets.connect(uri) as ws:
        print("WebSocket connected.\n")
        try:
            for i in range(30):
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                data = json.loads(raw)
                t = data.get("type", "?")

                if t == "frame":
                    print(f"  [{i}] frame  ({len(data.get('screenshot',''))} chars)")

                elif t == "step":
                    print(f"  [{i}] STEP {data['step_number']}/{data['total_steps']}  "
                          f"action={data['action']}  instruction={data.get('instruction','')}")
                    print(f"        screenshot={len(data.get('screenshot',''))} chars")
                    # Simulate user pressing Done after a short pause
                    await asyncio.sleep(1)
                    await ws.send(json.dumps({"type": "action_done", "value": None}))
                    print(f"        -> sent action_done")

                elif t == "status":
                    print(f"  [{i}] status: {data.get('message','')}")

                elif t == "error":
                    print(f"  [{i}] ERROR: {data.get('message','(empty)')}")
                    break

                elif t == "complete":
                    print(f"  [{i}] COMPLETE: {data.get('message','')}")
                    break

                else:
                    print(f"  [{i}] {t}: {json.dumps(data)}")

        except asyncio.TimeoutError:
            print("\n  (timed out waiting for next message)")
        except websockets.exceptions.ConnectionClosed as e:
            print(f"\n  Connection closed: {e}")
        except Exception as e:
            print(f"\n  Exception: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
