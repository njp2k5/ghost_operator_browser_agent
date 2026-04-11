import whatsappPkg from "whatsapp-web.js";
import qrcode from "qrcode-terminal";
import { HEADLESS } from "../config/env.js";
import { logger } from "../utils/logger.js";

const { Client, LocalAuth } = whatsappPkg;

export function createWhatsAppClient(onMessage) {
    logger.info("Creating WhatsApp client");
    const client = new Client({
        authStrategy: new LocalAuth(),
        puppeteer: {
            headless: HEADLESS,
            args: ["--no-sandbox", "--disable-setuid-sandbox"],
        },
    });

    client.on("qr", (qr) => {
        logger.info("Scan QR:");
        qrcode.generate(qr, { small: true });
    });

    client.on("ready", () => {
        logger.info("WhatsApp client is ready");
    });

    client.on("message", (msg) => {
        logger.info("WhatsApp incoming message event", { from: msg.from, body: msg.body || "<no body>" });
        onMessage(client, msg);
    });

    client.on("disconnected", (reason) => {
        logger.error("WhatsApp client disconnected", reason);
    });

    return client;
}