module.exports = {
  apps: [
    {
      name:       "hl-momentum-paper",
      script:     "main.py",
      interpreter: "/Users/ww/Projects/sniper-bot/venv/bin/python",
      env: {
        PAPER_TRADE: "true",
      },
      out_file:        "logs/pm2-out.log",
      error_file:      "logs/pm2-error.log",
      merge_logs:      true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      autorestart:     true,
      max_restarts:    10,
      min_uptime:      "10s",
      restart_delay:   5000,
    },
    {
      name:       "hl-momentum-live",
      script:     "main.py",
      interpreter: "/Users/ww/Projects/sniper-bot/venv/bin/python",
      env: {
        PAPER_TRADE: "false",
      },
      out_file:        "logs/pm2-out.log",
      error_file:      "logs/pm2-error.log",
      merge_logs:      true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      autorestart:     true,
      max_restarts:    10,
      min_uptime:      "10s",
      restart_delay:   5000,
    },
  ],
};
