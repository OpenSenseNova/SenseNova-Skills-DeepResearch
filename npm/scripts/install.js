#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const packageManifest = require("../package.json");
const {
  pythonExecutable,
  runtimeDirectory,
  runtimeRoot
} = require("../lib/runtime");

function execute(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    shell: false,
    ...options
  });
  if (result.error) throw result.error;
  return result;
}

function assertNodeVersion(version = process.versions.node) {
  const major = Number.parseInt(String(version).split(".", 1)[0], 10);
  if (!Number.isInteger(major) || major < 22) {
    throw new Error(`Node.js >=22 is required; found ${version}`);
  }
}

function pythonCandidates(environment = process.env, platform = process.platform) {
  const candidates = [];
  if (environment.DEEPRESEARCH_PYTHON) {
    candidates.push({ command: environment.DEEPRESEARCH_PYTHON, prefix: [] });
  }
  if (platform === "win32") {
    candidates.push(
      { command: "py", prefix: ["-3.14"] },
      { command: "py", prefix: ["-3.13"] },
      { command: "py", prefix: ["-3.12"] },
      { command: "py", prefix: ["-3.11"] },
      { command: "py", prefix: ["-3.10"] },
      { command: "py", prefix: ["-3"] },
      { command: "python3.14", prefix: [] },
      { command: "python3.13", prefix: [] },
      { command: "python3.12", prefix: [] },
      { command: "python3.11", prefix: [] },
      { command: "python3.10", prefix: [] },
      { command: "python", prefix: [] },
      { command: "python3", prefix: [] }
    );
  } else {
    candidates.push(
      { command: "python3.14", prefix: [] },
      { command: "python3.13", prefix: [] },
      { command: "python3.12", prefix: [] },
      { command: "python3.11", prefix: [] },
      { command: "python3.10", prefix: [] },
      { command: "python3", prefix: [] },
      { command: "python", prefix: [] }
    );
  }
  return candidates;
}

function probePython(candidate) {
  const code = [
    "import json, sys",
    "print(json.dumps({'version': list(sys.version_info[:3]), 'executable': sys.executable}))"
  ].join("; ");
  const result = execute(candidate.command, [...candidate.prefix, "-c", code]);
  if (result.status !== 0) return null;
  try {
    const value = JSON.parse(result.stdout.trim());
    if (!Array.isArray(value.version) || value.version.length !== 3) return null;
    return { ...candidate, ...value };
  } catch (_error) {
    return null;
  }
}

function findPython(environment = process.env, platform = process.platform) {
  for (const candidate of pythonCandidates(environment, platform)) {
    try {
      const value = probePython(candidate);
      if (value && (value.version[0] > 3 || (value.version[0] === 3 && value.version[1] >= 10))) {
        return value;
      }
    } catch (_error) {
      // Continue through PATH candidates and report one actionable error below.
    }
  }
  throw new Error(
    "Python >=3.10 was not found. Install Python 3.10+ or set " +
      "DEEPRESEARCH_PYTHON to its executable path, then run `npm rebuild`."
  );
}

function findBundledWheel(packageRoot = path.resolve(__dirname, "..")) {
  const vendor = path.join(packageRoot, "vendor");
  const wheels = fs.existsSync(vendor)
    ? fs.readdirSync(vendor).filter((name) => name.endsWith(".whl"))
    : [];
  if (wheels.length !== 1) {
    throw new Error(`expected exactly one bundled wheel in ${vendor}; found ${wheels.length}`);
  }
  return path.join(vendor, wheels[0]);
}

function runChecked(command, args, label) {
  const result = execute(command, args, { stdio: "inherit" });
  if (result.status !== 0) {
    throw new Error(`${label} failed with exit code ${result.status}`);
  }
}

function replaceRuntime(staging, destination) {
  const backup = `${destination}.backup-${process.pid}`;
  let backedUp = false;
  try {
    if (fs.existsSync(destination)) {
      fs.renameSync(destination, backup);
      backedUp = true;
    }
    fs.renameSync(staging, destination);
    if (backedUp) fs.rmSync(backup, { recursive: true, force: true });
  } catch (error) {
    if (!fs.existsSync(destination) && backedUp && fs.existsSync(backup)) {
      fs.renameSync(backup, destination);
    }
    throw error;
  }
}

function install() {
  assertNodeVersion();
  const selected = findPython();
  const wheel = findBundledWheel();
  const root = runtimeRoot();
  const destination = runtimeDirectory(packageManifest.version);
  const staging = path.join(root, `.install-${packageManifest.version}-${process.pid}`);

  fs.mkdirSync(root, { recursive: true });
  fs.rmSync(staging, { recursive: true, force: true });
  console.log(
    `[deepresearch] installing ${packageManifest.version} with Python ` +
      `${selected.version.join(".")} (${selected.executable})`
  );

  try {
    runChecked(
      selected.command,
      [...selected.prefix, "-m", "venv", staging],
      "virtual environment creation"
    );
    const stagingPython = pythonExecutable(staging);
    runChecked(
      stagingPython,
      [
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--upgrade",
        wheel
      ],
      "wheel installation"
    );
    const check = execute(stagingPython, [
      "-c",
      `import deepresearch_cli; assert deepresearch_cli.__version__ == ${JSON.stringify(packageManifest.version)}`
    ]);
    if (check.status !== 0) {
      throw new Error("installed Python package version does not match the npm package");
    }
    replaceRuntime(staging, destination);
  } catch (error) {
    fs.rmSync(staging, { recursive: true, force: true });
    throw error;
  }

  console.log(`[deepresearch] ready: ${destination}`);
  console.log("[deepresearch] Camofox remains optional; install it with `deepresearch browser setup`.");
}

function main() {
  try {
    install();
  } catch (error) {
    console.error(`[deepresearch] installation failed: ${error.message}`);
    process.exitCode = 1;
  }
}

if (require.main === module) main();

module.exports = {
  assertNodeVersion,
  findBundledWheel,
  findPython,
  install,
  probePython,
  pythonCandidates,
  replaceRuntime
};
