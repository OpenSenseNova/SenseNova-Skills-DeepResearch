#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const { spawn } = require("node:child_process");
const packageManifest = require("../package.json");
const { pythonExecutable, runtimeDirectory } = require("../lib/runtime");

const runtimePath = runtimeDirectory(packageManifest.version);
const command = pythonExecutable(runtimePath);

if (!fs.existsSync(command)) {
  console.error(
    `[deepresearch] Python runtime is missing at ${runtimePath}.\n` +
      "Run `npm rebuild -g sensenova-skills-deepresearch` or reinstall the package."
  );
  process.exit(1);
}

const child = spawn(command, ["-m", "deepresearch_cli", ...process.argv.slice(2)], {
  stdio: "inherit",
  windowsHide: false
});

let terminating = false;
for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => {
    if (terminating) return;
    terminating = true;
    if (!child.killed) child.kill(signal);
  });
}

child.on("error", (error) => {
  console.error(`[deepresearch] failed to start the Python CLI: ${error.message}`);
  process.exitCode = 1;
});

child.on("exit", (code, signal) => {
  if (typeof code === "number") {
    process.exitCode = code;
    return;
  }
  const signalExitCodes = { SIGHUP: 129, SIGINT: 130, SIGTERM: 143 };
  process.exitCode = signalExitCodes[signal] || 1;
});
