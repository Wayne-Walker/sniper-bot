const RESET  = "\x1b[0m";
const GREEN  = "\x1b[32m";
const YELLOW = "\x1b[33m";
const RED    = "\x1b[31m";
const CYAN   = "\x1b[36m";
const GREY   = "\x1b[90m";

function timestamp(): string {
  return new Date().toISOString();
}

export const log = {
  info:    (msg: string) => console.log (`${GREY}[${timestamp()}]${RESET} ${CYAN}INFO${RESET}  ${msg}`),
  success: (msg: string) => console.log (`${GREY}[${timestamp()}]${RESET} ${GREEN}OK${RESET}    ${msg}`),
  warn:    (msg: string) => console.warn(`${GREY}[${timestamp()}]${RESET} ${YELLOW}WARN${RESET}  ${msg}`),
  error:   (msg: string) => console.error(`${GREY}[${timestamp()}]${RESET} ${RED}ERROR${RESET} ${msg}`),
  trade:   (msg: string) => console.log (`${GREY}[${timestamp()}]${RESET} ${GREEN}TRADE${RESET} ${msg}`),
  paper:   (msg: string) => console.log (`${GREY}[${timestamp()}]${RESET} ${YELLOW}PAPER${RESET} ${msg}`),
};
