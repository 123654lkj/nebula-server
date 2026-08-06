#!/usr/bin/env node
/**
 * nebula CLI — setup / start / sync / health / docker
 * 一条命令部署入口（也可 npm run *）
 */
"use strict";
const { spawnSync, execSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");

const ROOT = path.resolve(__dirname, "..");
const isWin = process.platform === "win32";

function log(...a) { console.log(...a); }
function die(msg, code = 1) { console.error(msg); process.exit(code); }

function which(cmd) {
  try {
    execSync(isWin ? `where ${cmd}` : `command -v ${cmd}`, { stdio: "ignore" });
    return true;
  } catch { return false; }
}

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: "inherit", cwd: ROOT, shell: isWin, env: process.env, ...opts });
  if (r.error) die(String(r.error));
  if (r.status !== 0 && opts.allowFail !== true) process.exit(r.status || 1);
  return r.status || 0;
}

function py() {
  if (fs.existsSync(path.join(ROOT, ".venv", isWin ? "Scripts/python.exe" : "bin/python"))) {
    return path.join(ROOT, ".venv", isWin ? "Scripts/python.exe" : "bin/python");
  }
  return which("python3") ? "python3" : "python";
}

function loadEnvFile() {
  const p = path.join(ROOT, ".env");
  if (!fs.existsSync(p)) return;
  for (const line of fs.readFileSync(p, "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m || line.trim().startsWith("#")) continue;
    let v = m[2].trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
    if (process.env[m[1]] === undefined) process.env[m[1]] = v;
  }
}

function cmdDoctor(quiet) {
  const miss = [];
  if (!which("python3") && !which("python")) miss.push("python3");
  if (!quiet) {
    log("node:", process.version);
    log("python:", py());
    log("docker:", which("docker") ? "yes" : "no");
    log("root:", ROOT);
    log("env:", fs.existsSync(path.join(ROOT, ".env")) ? ".env ok" : "missing .env (copy .env.example)");
  }
  if (miss.length) {
    if (!quiet) die("missing: " + miss.join(", "));
    return 1;
  }
  return 0;
}

function cmdSetup() {
  log("==> nebula setup");
  if (!fs.existsSync(path.join(ROOT, ".env"))) {
    fs.copyFileSync(path.join(ROOT, ".env.example"), path.join(ROOT, ".env"));
    log("created .env — 请填入 BAILIAN_API_KEY");
  }
  const venv = path.join(ROOT, ".venv");
  if (!fs.existsSync(venv)) {
    log("creating venv...");
    run(which("python3") ? "python3" : "python", ["-m", "venv", ".venv"]);
  }
  const pip = path.join(ROOT, ".venv", isWin ? "Scripts/pip" : "bin/pip");
  log("installing Python deps...");
  run(pip, ["install", "-q", "-r", "requirements.txt"]);
  fs.mkdirSync(path.join(ROOT, "data"), { recursive: true });
  log("setup done. next: npm start   或   npm run docker:up");
}

function cmdStart() {
  loadEnvFile();
  if (!process.env.BAILIAN_API_KEY) {
    log("⚠ BAILIAN_API_KEY 未设置（可写 .env）。服务可启动，但 embedding/写入会失败。");
  }
  const python = py();
  log("starting", python, "vector_memory_server.py");
  run(python, ["vector_memory_server.py"]);
}

function cmdSync() {
  loadEnvFile();
  process.env.NEBULA_URL = process.env.NEBULA_URL || `http://127.0.0.1:${process.env.NEBULA_PORT || 26670}`;
  process.env.VAULT_ROOT = process.env.VAULT_ROOT || path.join(ROOT, "vault", "notes");
  log("sync vault → nebula", process.env.VAULT_ROOT, "→", process.env.NEBULA_URL);
  run(py(), ["scripts/vault_to_nebula_sync.py"]);
}

function cmdHealth() {
  loadEnvFile();
  const port = process.env.NEBULA_PORT || 26670;
  const url = process.env.NEBULA_URL || `http://127.0.0.1:${port}`;
  try {
    const out = execSync(`curl -sf ${url}/v5/health`, { encoding: "utf8" });
    log(out);
  } catch {
    die("health failed: is server up? try npm start or npm run docker:up");
  }
}

function cmdScore() {
  loadEnvFile();
  run(py(), ["scripts/memory_scorecard.py"]);
}

function cmdDockerUp() {
  if (!which("docker")) die("docker not found");
  if (!fs.existsSync(path.join(ROOT, ".env"))) {
    fs.copyFileSync(path.join(ROOT, ".env.example"), path.join(ROOT, ".env"));
    log("created .env — 填 BAILIAN_API_KEY 后重跑");
  }
  run("docker", ["compose", "up", "-d", "--build"]);
  log("up. health: npm run health   logs: npm run docker:logs");
}

function main() {
  const [,, cmd, ...rest] = process.argv;
  const quiet = rest.includes("--quiet");
  switch (cmd) {
    case "doctor": return process.exit(cmdDoctor(quiet));
    case "setup": return cmdSetup();
    case "start": return cmdStart();
    case "sync": return cmdSync();
    case "health": return cmdHealth();
    case "score": return cmdScore();
    case "docker:up":
    case "up": return cmdDockerUp();
    case "help":
    case undefined:
      log(`星枢 Nebula CLI

用法:
  npx nebula setup        # venv + 依赖 + .env
  npx nebula start        # 前台启动 API
  npx nebula sync         # 黑曜石笔记 → 星枢
  npx nebula health
  npx nebula docker:up    # Docker 一键（推荐）
  npx nebula score

一条命令（Docker）:
  cp .env.example .env && npm run docker:up

一条命令（本机）:
  npm run setup && npm start
`);
      return;
    default:
      die(`unknown command: ${cmd} (try: setup|start|sync|health|docker:up)`);
  }
}

main();
