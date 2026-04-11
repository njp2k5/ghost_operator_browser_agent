import { createWhatsAppClient } from "./whatsapp/client.js";
import { handleIncomingMessage } from "./handlers/messageHandler.js";
import { logger } from "./utils/logger.js";

logger.info("Application startup initiated");
const client = createWhatsAppClient(handleIncomingMessage);
logger.info("WhatsApp client instance created");

client.initialize();
logger.info("WhatsApp client initialize() called");