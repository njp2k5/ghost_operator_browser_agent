import WebSocket from "ws";
import { WS_BASE_URL } from "../config/env.js";
import { logger } from "../utils/logger.js";

const connections = {};

function createWS(sender, onMessage) {
    logger.info("Creating WebSocket connection", { sender, url: `${WS_BASE_URL}/${encodeURIComponent(sender)}` });
    let openResolve;
    let openReject;

    const ws = new WebSocket(`${WS_BASE_URL}/${encodeURIComponent(sender)}`);
    const openPromise = new Promise((resolve, reject) => {
        openResolve = resolve;
        openReject = reject;
    });

    ws.on("open", () => {
        logger.info("WS open", { sender });
        openResolve(ws);
    });

    ws.on("message", async (data) => {
        try {
            const parsed = JSON.parse(data.toString());
            logger.info("WS message received", { sender, data: parsed });
            onMessage(parsed, sender);
        } catch (err) {
            logger.error("WS parse error", err);
        }
    });

    ws.on("close", () => {
        logger.warn("WS closed", { sender, state: ws.readyState });
        if (ws.readyState !== WebSocket.OPEN) {
            openReject(new Error("WebSocket closed before open"));
        }
        setTimeout(() => {
            logger.info("Reconnecting WS", { sender, delayMs: 2000 });
            connections[sender] = createWS(sender, onMessage);
        }, 2000);
    });

    ws.on("error", (err) => {
        logger.error("WS error", { sender, message: err.message });
        if (ws.readyState !== WebSocket.OPEN) {
            openReject(err);
        }
    });

    return { ws, openPromise };
}

function getWS(sender, onMessage) {
    const existing = connections[sender];

    if (!existing || existing.ws.readyState === WebSocket.CLOSED || existing.ws.readyState === WebSocket.CLOSING) {
        connections[sender] = createWS(sender, onMessage);
    }

    return connections[sender];
}

async function ensureOpen(connection) {
    if (connection.ws.readyState === WebSocket.OPEN) {
        logger.debug("WS already open", { sender: connection.ws.url });
        return;
    }
    if (connection.ws.readyState === WebSocket.CONNECTING) {
        logger.info("Waiting for WS connection to open", { sender: connection.ws.url });
        await connection.openPromise;
        logger.info("WS connection open now", { sender: connection.ws.url });
        return;
    }
    throw new Error("WebSocket is not open or connecting");
}

export async function sendWSMessage(sender, payload, onMessage) {
    logger.info("Preparing to send WS message", { sender, payload });
    const connection = getWS(sender, onMessage);
    await ensureOpen(connection);
    connection.ws.send(JSON.stringify(payload));
    logger.info("Sent WS message", { sender, payload });
}

export { getWS };
