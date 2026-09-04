"use strict";

const os = require("node:os");
const path = require("node:path");

function assertVersion(version) {
  if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version)) {
    throw new Error(`invalid package version: ${version}`);
  }
  return version;
}

function userHome(environment = process.env) {
  return environment.HOME || environment.USERPROFILE || os.homedir();
}

function runtimeRoot(environment = process.env) {
  if (environment.DEEPRESEARCH_NPM_RUNTIME_HOME) {
    return path.resolve(environment.DEEPRESEARCH_NPM_RUNTIME_HOME);
  }
  return path.join(userHome(environment), ".deepresearch-cli", "npm-runtime");
}

function runtimeDirectory(version, environment = process.env) {
  return path.join(runtimeRoot(environment), assertVersion(version));
}

function pythonExecutable(runtimePath, platform = process.platform) {
  return platform === "win32"
    ? path.join(runtimePath, "Scripts", "python.exe")
    : path.join(runtimePath, "bin", "python");
}

module.exports = {
  assertVersion,
  pythonExecutable,
  runtimeDirectory,
  runtimeRoot,
  userHome
};
