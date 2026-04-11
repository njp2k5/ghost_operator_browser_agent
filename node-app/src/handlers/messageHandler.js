import { sendWSMessage } from "../websocket/wsManager.js";
import { getQueue } from "../websocket/queueManager.js";
import { logger } from "../utils/logger.js";

export function handleIncomingMessage(client, msg) {
    if (msg.fromMe) {
        logger.debug("Ignoring outgoing message from self", { from: msg.from });
        return;
    }
    if (msg.from.endsWith("@g.us")) {
        logger.debug("Ignoring group message", { from: msg.from });
        return;
    }

    const sender = msg.from;
    const text = msg.body;

    logger.info("Incoming WhatsApp message", { sender, text });

    const queue = getQueue(sender);
    logger.info("Queued message for processing", { sender, queueSize: queue.size });

    queue.add(async () => {
        logger.info("Queue task started", { sender });
        try {
            await sendWSMessage(sender, { message: text }, async (response) => {
                const reply = response.reply || response.error || "⚠️ No response";
                logger.info("Reply ready to send", { sender, reply });
                await msg.reply(reply);
                logger.info("Reply sent to WhatsApp", { sender });
            });
            logger.info("Message forwarded to WS backend", { sender });
        } catch (err) {
            logger.error("Failed to send message to WS backend", { sender, error: err });
            await msg.reply("⚠️ Unable to process your message right now.");
        }
    });
}