/*
 * Offline Chromium workflow tests. Starts and stops its own loopback server,
 * uses an isolated temporary workspace, and never downloads live public data.
 *
 * Run: node tests/browser_smoke.cjs
 * Optional: PYTHON_EXECUTABLE, CHROME_EXECUTABLE,
 *           CODEX_PRIMARY_RUNTIME_NODE_MODULES (managed-runtime fallback only).
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

function loadPlaywright() {
  try {
    return require("playwright");
  } catch (error) {
    if (process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES) {
      return require(path.join(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES, "playwright"));
    }
    throw new Error("Install the browser test dependency with npm install --no-save playwright", { cause: error });
  }
}

const root = path.resolve(__dirname, "..");
const results = path.join(root, "test-results");
const python = process.env.PYTHON_EXECUTABLE || "python";
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "eljeffe-browser-"));
fs.mkdirSync(results, { recursive: true });
fs.rmSync(path.join(results, "workbench-failure.png"), { force: true });

let server = null;
let serverClosed = null;
let serverOutput = "";
let browser = null;
let page = null;
let cleaning = null;
const pageErrors = [];
const externalRequests = [];

function withTimeout(promise, milliseconds, description) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(description)), milliseconds);
    }),
  ]).finally(() => clearTimeout(timer));
}

async function startServer() {
  server = spawn(python, ["-m", "eljeffe", "--data-dir", temp, "serve", "--port", "0"], {
    cwd: root,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  serverClosed = new Promise((resolve) => server.once("close", resolve));
  server.stdout.setEncoding("utf8");
  server.stderr.setEncoding("utf8");
  server.stderr.on("data", (chunk) => {
    serverOutput = (serverOutput + chunk).slice(-16000);
  });
  const ready = new Promise((resolve, reject) => {
    let output = "";
    server.once("error", reject);
    server.once("exit", (code, signal) => {
      reject(new Error(`Python server exited before startup (code ${code}, signal ${signal}).\n${serverOutput}`));
    });
    server.stdout.on("data", (chunk) => {
      output += chunk;
      serverOutput = (serverOutput + chunk).slice(-16000);
      // Wait for the full output line, avoiding a partially received port.
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+(?=\r?\n)/);
      if (match) resolve(match[0]);
    });
  });
  return withTimeout(ready, 20000, "Python loopback server did not announce its URL within 20 seconds");
}

async function cleanup() {
  if (cleaning) return cleaning;
  cleaning = (async () => {
    if (browser) {
      await browser.close().catch(() => {});
      browser = null;
    }
    if (server) {
      if (server.exitCode === null && server.signalCode === null) server.kill("SIGTERM");
      try {
        await withTimeout(serverClosed, 4000, "Server termination timed out");
      } catch {
        if (server.exitCode === null && server.signalCode === null) server.kill("SIGKILL");
        await withTimeout(serverClosed, 3000, "Server could not be stopped");
      }
      server = null;
    }
    fs.rmSync(temp, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  })();
  return cleaning;
}

for (const [signal, code] of [["SIGINT", 130], ["SIGTERM", 143]]) {
  process.once(signal, () => {
    cleanup().finally(() => process.exit(code));
  });
}

async function waitForRecordCount(expected) {
  await page.waitForFunction((count) => (
    document.querySelectorAll("#records [data-record-id]").length === count
  ), expected);
  assert.equal(await page.locator("#records [data-record-id]").count(), expected,
    `Expected ${expected} displayed record cards or table rows`);
}

async function search(query, expected) {
  await page.locator("#search").fill(query);
  await page.locator("#search-form button[type=submit]").click();
  await waitForRecordCount(expected);
}

async function closeDetail() {
  await page.locator("#close-detail").click();
  await page.locator("#detail-dialog").waitFor({ state: "hidden" });
}

async function stats(base) {
  const response = await page.request.get(base + "/api/stats");
  assert.equal(response.status(), 200, "Workspace stats endpoint must respond successfully");
  return response.json();
}

async function waitForActiveRecords(expected) {
  await page.waitForFunction(async (count) => {
    const response = await fetch("/api/stats");
    return response.ok && (await response.json()).active === count;
  }, expected, { polling: 100 });
}

async function importFile(name, text) {
  await page.locator("[data-view=sources]").click();
  await page.locator("#view-sources").waitFor({ state: "visible" });
  await page.locator("#import-file").setInputFiles({
    name,
    mimeType: "application/x-ndjson",
    buffer: Buffer.from(text, "utf8"),
  });
  await page.locator("#import-button").click();
}

function validateZip(filename) {
  const content = fs.readFileSync(filename);
  assert.ok(content.length > 4, "Export must contain bytes");
  assert.equal(content.readUInt32LE(0), 0x04034b50, "Export must be a ZIP archive");
  const verification = spawnSync(python, ["-c", [
    "import hashlib,json,sys,zipfile",
    "with zipfile.ZipFile(sys.argv[1]) as archive:",
    "    assert set(archive.namelist()) == {'eljeffe.ftm.json','manifest.json'}",
    "    raw = archive.read('eljeffe.ftm.json')",
    "    manifest = json.loads(archive.read('manifest.json'))",
    "    entities = [json.loads(line) for line in raw.splitlines() if line]",
    "    assert len(entities) == manifest['count'] == 5",
    "    assert hashlib.sha256(raw).hexdigest() == manifest['sha256']",
    "    assert manifest['sources'], 'Export must retain source provenance'",
    "    assert {'demo-company-002','demo-sanction-003'} <= {e['id'] for e in entities}",
    "    assert all(isinstance(v,list) and all(isinstance(x,str) for x in v) for e in entities for v in e['properties'].values())",
    "    assert all(not any(k.startswith('_') for k in e) for e in entities)",
    "    print(json.dumps({'count':len(entities),'sha256':manifest['sha256']}))",
  ].join("\n"), filename], { cwd: root, encoding: "utf8", windowsHide: true });
  assert.equal(verification.status, 0, `ZIP/FtM verification failed: ${verification.stderr}`);
  return JSON.parse(verification.stdout);
}

async function assertNoHorizontalOverflow(description) {
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  assert.ok(dimensions.document <= dimensions.viewport + 1 && dimensions.body <= dimensions.viewport + 1,
    `${description} overflows horizontally: ${JSON.stringify(dimensions)}`);
}

async function run() {
  const base = await startServer();
  const { chromium } = loadPlaywright();
  browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}),
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    acceptDownloads: true,
    timezoneId: "America/New_York",
    locale: "en-US",
  });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin === base) return route.continue();
    externalRequests.push(url.origin + url.pathname);
    return route.abort("blockedbyclient");
  });
  page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto(base, { waitUntil: "networkidle" });

  // A fresh installation must be empty until the user explicitly loads data.
  await page.locator("#empty-state").waitFor({ state: "visible" });
  assert.equal((await stats(base)).active, 0, "Demo data must never load automatically");
  assert.equal(await page.locator("#stat-active").innerText(), "0");
  assert.match(await page.locator("#empty-title").innerText(), /source/i);
  console.log("PASS: fresh workspace is empty and explicitly offers the fictional demo");

  await page.locator("#demo-button").click();
  await waitForActiveRecords(5);
  await waitForRecordCount(5);
  await page.locator("#demo-banner").waitFor({ state: "visible" });
  assert.match(await page.locator("#records").innerText(), /FICTIONAL DEMO/);
  assert.equal((await stats(base)).sources.length, 1);
  console.log("PASS: user-initiated fictional demo loads five records");

  await page.locator("#kind-filter").selectOption("wanted");
  await waitForRecordCount(1);
  assert.match(await page.locator("#records").innerText(), /Alex Morgan/);
  assert.doesNotMatch(await page.locator("#records").innerText(), /Jordan Rivera|Taylor Chen/);
  await page.locator("#kind-filter").selectOption("sanction");
  await waitForRecordCount(2);
  assert.match(await page.locator("#records").innerText(), /Northstar Example Trading/);
  await page.locator("#kind-filter").selectOption("pep");
  await waitForRecordCount(1);
  assert.match(await page.locator("#records").innerText(), /Jordan Rivera/);
  await page.locator("#clear-filters").click();
  await waitForRecordCount(5);
  console.log("PASS: wanted, sanctions, and PEP filters preserve their distinct meanings");

  await search("Morgan", 1);
  assert.match(await page.locator("#records").innerText(), /Alex Morgan/);
  await page.locator("#records .open-record").first().click();
  await page.locator("#detail-dialog").waitFor({ state: "visible" });
  await page.waitForFunction(() => document.querySelector("#detail-title")?.textContent.includes("Alex Morgan"));
  assert.match(await page.locator("#detail-title").innerText(), /Alex Morgan/);
  assert.match(await page.locator("#detail-content").innerText(), /Fictional demonstration|fictional_demo/);
  await page.locator("#detail-dialog .details-raw summary").click();
  const rawEntity = JSON.parse(await page.locator("#detail-dialog .details-raw pre").innerText());
  assert.equal(rawEntity.id, "demo-person-001");
  assert.deepEqual(rawEntity.properties.topics, ["wanted"]);
  assert.ok(Array.isArray(rawEntity.properties.name));
  await closeDetail();
  await search("no-such-fictional-record-8675309", 0);
  await page.locator("#empty-state").waitFor({ state: "visible" });
  await page.locator("#clear-filters").click();
  await waitForRecordCount(5);
  console.log("PASS: search, empty search results, record detail, and raw FtM inspection");

  await page.locator("#table-button").click();
  assert.equal(await page.locator("#table-button").getAttribute("aria-pressed"), "true");
  await waitForRecordCount(5);
  await page.locator("#cards-button").click();
  assert.equal(await page.locator("#cards-button").getAttribute("aria-pressed"), "true");
  await waitForRecordCount(5);

  await page.locator("[data-view=sources]").click();
  await page.locator("#view-sources").waitFor({ state: "visible" });
  assert.match(await page.locator("#sources-list").innerText(), /Fictional demonstration/);
  assert.match(await page.locator("#sources-list").innerText(), /Fictional test data/);
  assert.ok(await page.locator("#sync-dataset option[value=us_ofac_sdn]").count());
  await page.locator("#sync-provider").selectOption("fbi");
  await page.locator("#dataset-label").waitFor({ state: "hidden" });
  await page.locator("#sync-provider").selectOption("opensanctions");
  await page.locator("#dataset-label").waitFor({ state: "visible" });
  await page.locator("[data-view=records]").click();
  await page.locator("#view-records").waitFor({ state: "visible" });
  console.log("PASS: source provenance and provider configuration are visible without network sync");

  const downloadPromise = page.waitForEvent("download");
  await page.locator("#export-button").click();
  const download = await downloadPromise;
  assert.match(download.suggestedFilename(), /\.zip$/);
  const archivePath = path.join(results, "workbench-export.zip");
  await download.saveAs(archivePath);
  const exportResult = validateZip(archivePath);
  console.log(`PASS: ZIP export contains ${exportResult.count} FtM records and a verified provenance checksum`);

  if (await page.locator("#notification").isVisible()) await page.locator("#dismiss-notification").click();
  await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
  await page.waitForFunction(() => window.scrollY === 0);
  await assertNoHorizontalOverflow("Desktop records");
  await page.screenshot({ path: path.join(results, "workbench-desktop.png"), fullPage: true });

  // Payloads stay text throughout import, search, and detail rendering. Checking
  // the actual DOM catches unsafe HTML even if CSP would block script execution.
  const hostileName = '<img src="https://example.invalid/smoke-pixel" onerror="window.__smokeInjected=true"> Example fixture';
  const hostile = {
    id: "smoke-hostile-record",
    schema: "Person",
    properties: {
      name: [hostileName],
      notes: ["<script>window.__smokeInjected=true</script>"],
      sourceUrl: ["javascript:window.__smokeInjected=true"],
    },
  };
  await importFile("hostile.ftm.json", JSON.stringify(hostile) + "\n");
  await waitForActiveRecords(6);
  await page.waitForFunction(() => !document.querySelector("#sync-button").disabled);
  await page.locator("[data-view=records]").click();
  await page.locator("#clear-filters").click();
  await search("smoke-hostile-record", 1);
  assert.equal(await page.locator("#records .record-name").innerText(), hostileName);
  await page.locator("#records .open-record").first().click();
  await page.locator("#detail-dialog").waitFor({ state: "visible" });
  await page.waitForFunction((name) => document.querySelector("#detail-title")?.textContent === name, hostileName);
  assert.equal(await page.locator("#detail-title").innerText(), hostileName);
  assert.equal(await page.locator("[onerror], [onload]").count(), 0, "Imported text must not create event handler attributes");
  assert.equal(await page.locator('a[href^="javascript:"]').count(), 0, "Unsafe source URLs must not become clickable links");
  assert.equal(await page.evaluate(() => Boolean(window.__smokeInjected)), false);
  await closeDetail();
  console.log("PASS: imported HTML-like names, script text, and unsafe URLs remain inert");

  await importFile("invalid.ftm.json", '{"id":"truncated"');
  await page.locator("#notification.error").waitFor({ state: "visible" });
  assert.match(await page.locator("#notification-text").innerText(), /invalid|failed|json|line/i);
  assert.equal((await stats(base)).active, 6, "Invalid import must not modify existing records");
  console.log("PASS: invalid import shows an actionable error and preserves the database");

  await page.locator("[data-view=records]").click();
  await page.locator("#clear-filters").click();
  await page.locator("#source-filter").selectOption("demo");
  await waitForRecordCount(5);
  if (await page.locator("#notification").isVisible()) await page.locator("#dismiss-notification").click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
  await page.waitForFunction(() => window.scrollY === 0);
  await assertNoHorizontalOverflow("Mobile records");
  await page.screenshot({ path: path.join(results, "workbench-mobile.png"), fullPage: true });
  await page.locator("[data-view=sources]").click();
  await page.locator("#view-sources").waitFor({ state: "visible" });
  await assertNoHorizontalOverflow("Mobile source and import forms");
  console.log("PASS: mobile records and source forms fit a 390-pixel viewport");

  assert.deepEqual(pageErrors, [], `Browser script errors: ${pageErrors.join("; ")}`);
  assert.deepEqual(externalRequests, [], `The offline workflow attempted external requests: ${externalRequests.join(", ")}`);
  console.log("Browser smoke tests passed; server, browser, and temporary workspace are cleaned up automatically.");
}

run().catch(async (error) => {
  console.error(error.stack || error.message || error);
  if (page && !page.isClosed()) {
    await page.screenshot({ path: path.join(results, "workbench-failure.png"), fullPage: true }).catch(() => {});
  }
  if (server && server.exitCode !== null) console.error(serverOutput);
  process.exitCode = 1;
}).finally(async () => {
  try {
    await cleanup();
  } catch (error) {
    console.error("Browser test cleanup failed:", error.message);
    process.exitCode = 1;
  }
});
