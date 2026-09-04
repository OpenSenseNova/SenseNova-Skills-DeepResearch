"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  assertVersion,
  pythonExecutable,
  runtimeDirectory,
  runtimeRoot
} = require("../lib/runtime");
const {
  assertNodeVersion,
  findBundledWheel,
  pythonCandidates,
  replaceRuntime
} = require("../scripts/install");

test("runtime paths are versioned and support an explicit home", () => {
  const environment = { DEEPRESEARCH_NPM_RUNTIME_HOME: "/tmp/dr-runtime" };
  assert.equal(runtimeRoot(environment), path.resolve("/tmp/dr-runtime"));
  assert.equal(
    runtimeDirectory("0.1.9", environment),
    path.join(path.resolve("/tmp/dr-runtime"), "0.1.9")
  );
});

test("runtime version rejects path traversal", () => {
  assert.equal(assertVersion("1.2.3-beta.1"), "1.2.3-beta.1");
  assert.throws(() => assertVersion("../../tmp"), /invalid package version/);
});

test("Python executable is platform-specific", () => {
  assert.equal(pythonExecutable("/runtime", "linux"), path.join("/runtime", "bin", "python"));
  assert.equal(
    pythonExecutable("C:\\runtime", "win32"),
    path.join("C:\\runtime", "Scripts", "python.exe")
  );
});

test("Node and Python prerequisites are validated", () => {
  assert.doesNotThrow(() => assertNodeVersion("22.0.0"));
  assert.throws(() => assertNodeVersion("21.9.0"), /Node.js >=22/);
  assert.equal(pythonCandidates({ DEEPRESEARCH_PYTHON: "/opt/python" }, "linux")[0].command, "/opt/python");
  assert.deepEqual(
    pythonCandidates({}, "linux").map((candidate) => candidate.command),
    ["python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"]
  );
  assert.deepEqual(
    pythonCandidates({}, "win32").slice(0, 5).map((candidate) => candidate.prefix[0]),
    ["-3.14", "-3.13", "-3.12", "-3.11", "-3.10"]
  );
});

test("bundled wheel must be unique", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepresearch-npm-wheel-"));
  const vendor = path.join(root, "vendor");
  fs.mkdirSync(vendor);
  assert.throws(() => findBundledWheel(root), /found 0/);
  fs.writeFileSync(path.join(vendor, "one.whl"), "one");
  assert.equal(findBundledWheel(root), path.join(vendor, "one.whl"));
  fs.writeFileSync(path.join(vendor, "two.whl"), "two");
  assert.throws(() => findBundledWheel(root), /found 2/);
  fs.rmSync(root, { recursive: true, force: true });
});

test("runtime replacement preserves the old runtime until staging is ready", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepresearch-npm-runtime-"));
  const current = path.join(root, "current");
  const staging = path.join(root, "staging");
  fs.mkdirSync(current);
  fs.mkdirSync(staging);
  fs.writeFileSync(path.join(current, "marker"), "old");
  fs.writeFileSync(path.join(staging, "marker"), "new");
  replaceRuntime(staging, current);
  assert.equal(fs.readFileSync(path.join(current, "marker"), "utf8"), "new");
  assert.equal(fs.existsSync(staging), false);
  fs.rmSync(root, { recursive: true, force: true });
});
