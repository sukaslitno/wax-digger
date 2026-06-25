#!/usr/bin/env node
/*
 * build.js — produces ready-to-distribute packages for every browser.
 *
 *   node build.js
 *
 * Output (all in dist/):
 *   dist/chrome/                unpacked Chromium build (Chrome/Edge/Brave/Opera/Vivaldi/Arc)
 *   dist/firefox/               unpacked Firefox build (manifest tweaked for Gecko)
 *   dist/wax-digger-chrome.zip  zip for "Load unpacked" / Chrome Web Store
 *   dist/wax-digger-firefox.zip zip for web-ext / AMO signing
 *
 * Zero dependencies — uses Node's fs + the system `zip`.
 */

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const ROOT = __dirname;
const DIST = path.join(ROOT, "dist");

// Files that make up the shippable extension (everything else is tooling).
const SHIP = [
  "manifest.json",
  "background.js",
  "popup.html",
  "popup.css",
  "popup.js",
  "content.js",
  "content.css",
  "icons",
];

function log(msg) {
  console.log(msg);
}

function ensureIcons() {
  const iconDir = path.join(ROOT, "icons");
  const has = fs.existsSync(iconDir) && fs.readdirSync(iconDir).some((f) => f.endsWith(".png"));
  if (!has) {
    log("→ icons missing, generating…");
    execFileSync(process.execPath, [path.join(ROOT, "generate-icons.js")], { stdio: "inherit" });
  }
}

function copyShip(targetDir) {
  fs.rmSync(targetDir, { recursive: true, force: true });
  fs.mkdirSync(targetDir, { recursive: true });
  for (const item of SHIP) {
    const src = path.join(ROOT, item);
    if (!fs.existsSync(src)) throw new Error(`Missing source file: ${item}`);
    fs.cpSync(src, path.join(targetDir, item), { recursive: true });
  }
}

function readVersion() {
  const m = JSON.parse(fs.readFileSync(path.join(ROOT, "manifest.json"), "utf8"));
  return m.version;
}

// Firefox needs an explicit add-on id + a Gecko-compatible background entry.
// (Chrome ignores these keys, but Firefox rejects MV3 service_worker on older
// builds, so we ship an event-page background for maximum compatibility.)
function patchFirefoxManifest(dir) {
  const file = path.join(dir, "manifest.json");
  const m = JSON.parse(fs.readFileSync(file, "utf8"));

  m.browser_specific_settings = {
    gecko: {
      id: "wax-digger@sukaslitno.github.io",
      strict_min_version: "115.0",
    },
  };
  m.background = { scripts: ["background.js"] };

  fs.writeFileSync(file, JSON.stringify(m, null, 2) + "\n");
}

function zipDir(dir, zipPath) {
  fs.rmSync(zipPath, { force: true });
  // zip from inside the build dir so paths are relative (no leading folder)
  execFileSync("zip", ["-r", "-q", "-X", zipPath, "."], { cwd: dir });
}

function main() {
  ensureIcons();
  fs.rmSync(DIST, { recursive: true, force: true });
  fs.mkdirSync(DIST, { recursive: true });

  const version = readVersion();
  log(`Building Wax Digger v${version}`);

  // --- Chrome / Chromium ---
  const chromeDir = path.join(DIST, "chrome");
  copyShip(chromeDir);
  const chromeZip = path.join(DIST, "wax-digger-chrome.zip");
  zipDir(chromeDir, chromeZip);
  log(`✓ ${path.relative(ROOT, chromeZip)}`);

  // --- Firefox ---
  const firefoxDir = path.join(DIST, "firefox");
  copyShip(firefoxDir);
  patchFirefoxManifest(firefoxDir);
  const firefoxZip = path.join(DIST, "wax-digger-firefox.zip");
  zipDir(firefoxDir, firefoxZip);
  log(`✓ ${path.relative(ROOT, firefoxZip)}`);

  log("\nDone. Unpacked builds in dist/chrome and dist/firefox; zips alongside.");
}

main();
