import dotenv from "dotenv";

dotenv.config();

export const WS_BASE_URL = process.env.WS_BASE_URL || "ws://localhost:8000/ws";
export const HEADLESS = process.env.HEADLESS === "true";
