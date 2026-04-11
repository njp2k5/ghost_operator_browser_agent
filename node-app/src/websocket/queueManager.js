import PQueue from "p-queue";
import { logger } from "../utils/logger.js";

const queues = {};

export function getQueue(sender) {
    if (!queues[sender]) {
        logger.info("Creating new send queue", { sender });
        queues[sender] = new PQueue({
            concurrency: 1,
            interval: 1000,
            intervalCap: 2,
        });
    }
    return queues[sender];
}