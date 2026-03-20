const path = require("node:path");

const pythonInterpreter =
  process.env.PYTHON_INTERPRETER ||
  (process.platform === "win32"
    ? path.join(__dirname, ".venv", "Scripts", "python.exe")
    : path.join(__dirname, ".venv", "bin", "python"));

module.exports = {
  apps: [
    {
      name: "genie-tts",
      cwd: __dirname,
      script: "server.py",
      interpreter: pythonInterpreter,
      autorestart: true,
      watch: false,
    },
  ],
};