import { sendWSMessage } from "../websocket/wsManager.js";
import { getQueue } from "../websocket/queueManager.js";
import { logger } from "../utils/logger.js";
import { formatWhatsAppReply } from "../whatsapp/messageFormatter.js";

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
                const replies = formatWhatsAppReply(response.reply ? response : (response || { error: "⚠️ No response" }));
                logger.info("Reply ready to send", {
                    sender,
                    chunkCount: replies.length,
                    preview: replies[0],
                });

                for (const [index, reply] of replies.entries()) {
                    await client.sendMessage(sender, reply);
                    logger.info("Message chunk sent to WhatsApp", { sender, index: index + 1, total: replies.length });
                }
            });
            logger.info("Message forwarded to WS backend", { sender });
        } catch (err) {
            logger.error("Failed to send message to WS backend", { sender, error: err });
            const fallbackReplies = formatWhatsAppReply({
                title: "Unable to process that just now",
                subtitle: "The service is temporarily unavailable",
                error: "We couldn't complete your request right now.",
                footer: "Please try again in a moment.",
            });

            for (const [index, reply] of fallbackReplies.entries()) {
                await client.sendMessage(sender, reply);
                logger.info("Fallback message chunk sent to WhatsApp", { sender, index: index + 1, total: fallbackReplies.length });
            }
        }
    });
}