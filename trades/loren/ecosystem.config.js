module.exports = {
  apps: [{
    name: 'loren-trader',
    script: './web_ui.py',
    interpreter: './venv/bin/python',
    cwd: '/root/TelVictory/trades/loren',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    env: {
      NODE_ENV: 'production',
      PORT: 5050,
      FLASK_PORT: 5050
    },
    log_file: './logs/combined.log',
    out_file: './logs/out.log',
    error_file: './logs/error.log',
    time: true
  }]
};
