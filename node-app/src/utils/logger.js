function formatLog(level, args) {
  const timestamp = new Date().toISOString();
  const message = args.map((item) => {
    if (item instanceof Error) {
      return item.stack || item.message;
    }
    if (typeof item === "object") {
      try {
        return JSON.stringify(item);
      } catch {
        return String(item);
      }
    }
    return String(item);
  }).join(" ");

  return `[${timestamp}] [${level}] ${message}`;
}

export const logger = {
  info: (...args) => console.log(formatLog("INFO", args)),
  warn: (...args) => console.warn(formatLog("WARN", args)),
  error: (...args) => console.error(formatLog("ERROR", args)),
  debug: (...args) => console.debug(formatLog("DEBUG", args)),
};
