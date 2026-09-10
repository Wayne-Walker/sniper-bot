import axios from "axios";
import { config } from "../config";
import { log } from "./logger";

export async function sendAlert(message: string): Promise<void> {
  if (!config.telegram.botToken || !config.telegram.chatId) return;

  try {
    await axios.post(
      `https://api.telegram.org/bot${config.telegram.botToken}/sendMessage`,
      { chat_id: config.telegram.chatId, text: message, parse_mode: "Markdown" }
    );
  } catch {
    log.warn("Telegram alert failed");
  }
}
