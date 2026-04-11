from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.websocket_manager import manager
from services.llm_service import llm_service
from services.memory_service import memory_service

router = APIRouter()

@router.websocket("/ws/{sender}")
async def websocket_endpoint(websocket: WebSocket, sender: str):
    await manager.connect(sender, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            message = data["message"]

            # store user msg
            memory_service.append(sender, "user", message)

            history = memory_service.get_history(sender)

            # prepend system
            messages = [{"role": "system", "content": "WhatsApp AI assistant"}] + history

            # LLM call
            reply = llm_service.generate(messages)

            # store reply
            memory_service.append(sender, "assistant", reply)

            # send response
            await manager.send(sender, reply)

    except WebSocketDisconnect:
        manager.disconnect(sender)