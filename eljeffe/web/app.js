/* EL JEFFE local browser workbench. Source data is always rendered as text. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {config: null, stats: null, items: [], total: 0, offset: 0, limit: 24, mode: "cards", view: "records", busy: false, loading: false, searchId: 0, detailId: 0, detail: null};
const labels = {wanted: "Wanted", sanction: "Sanctions", pep: "Politically exposed", missing: "Missing person", other: "Other / relationship"};
const formatter = new Intl.NumberFormat();

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined && text !== null) result.textContent = String(text);
  return result;
}
function count(value) { return formatter.format(Number(value) || 0); }
function displayDate(value) {
  if (!value) return "Not reported";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, {year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
}
function safeURL(value) {
  try { const url = new URL(String(value)); return ["https:", "http:"].includes(url.protocol) ? url.href : null; } catch { return null; }
}
function externalLink(value, text) {
  const url = safeURL(value);
  if (!url) return node("span", "", text || value);
  const link = node("a", "", text || value);
  link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
  return link;
}
function localFileLink(value, text) {
  if (typeof value !== "string" || !value.startsWith("/files/") || value.includes("\\")) return null;
  const link = node("a", "", text);
  link.href = value; link.setAttribute("download", "");
  return link;
}
function notify(message, error = false) {
  $("notification-text").textContent = message;
  $("notification").classList.toggle("error", error);
  $("notification").setAttribute("role", error ? "alert" : "status");
  $("notification").setAttribute("aria-live", error ? "assertive" : "polite");
  $("notification").hidden = false;
}
function errorMessage(error) { return error instanceof Error ? error.message : String(error); }
async function api(path, payload) {
  const options = {cache: "no-store", headers: {Accept: "application/json"}};
  if (payload !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = state.config.csrfToken;
    options.body = JSON.stringify(payload);
  }
  let response;
  try { response = await fetch(path, options); } catch { throw new Error("Cannot reach the local workbench. Check that the EL JEFFE server is running, then refresh."); }
  let data;
  try { data = await response.json(); } catch { throw new Error(`The server returned an unreadable response (${response.status}).`); }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}
function filters() {
  return new URLSearchParams({q: $("search").value.trim(), source: $("source-filter").value, kind: $("kind-filter").value, schema: $("schema-filter").value, inactive: $("inactive-filter").checked ? "1" : "0"});
}
function sourceLabel(source) {
  const item = (state.stats?.sources || []).find((entry) => entry.id === source);
  return item?.label || item?.metadata?.label || source || "Unknown source";
}
function sourceIsDemo(source) { return source === "demo" || (state.stats?.sources || []).find((entry) => entry.id === source)?.metadata?.demo === true; }
function badges(entity) {
  const wrap = node("div", "card-badges");
  const categories = entity.categories?.length ? entity.categories : [entity.kind || "other"];
  for (const category of categories) wrap.append(node("span", `badge ${["wanted", "sanction", "pep", "missing"].includes(category) ? category : ""}`, labels[category] || category));
  if (sourceIsDemo(entity.source)) wrap.append(node("span", "badge demo", "FICTIONAL DEMO"));
  if (entity.active === false) wrap.append(node("span", "badge historical", "HISTORICAL"));
  return wrap;
}
function first(entity, ...keys) {
  for (const key of keys) { const value = entity.properties?.[key]; if (value?.length) return Array.isArray(value) ? value.join(" · ") : String(value); }
  return "Not reported";
}
function fact(label, value) {
  const wrap = node("div"); wrap.append(node("dt", "", label), node("dd", "", value)); return wrap;
}
function recordButton(entity, className, text) {
  const button = node("button", className, text);
  button.type = "button";
  button.addEventListener("click", () => openDetail(entity.id, entity.source));
  return button;
}
function card(entity) {
  const article = node("article", "record-card"); article.dataset.recordId = entity.id;
  const top = node("div", "card-top");
  const initials = (entity.caption || entity.id || "?").split(/\s+/).slice(0, 2).map((part) => Array.from(part)[0] || "").join("").toUpperCase();
  const avatar = node("div", "avatar", entity.schema === "Sanction" ? "§" : initials); avatar.setAttribute("aria-hidden", "true");
  top.append(avatar, badges(entity));
  const body = node("div", "card-body");
  body.append(recordButton(entity, "record-name", entity.caption || entity.id));
  const schema = node("div", "record-schema"); schema.append(node("span", "", entity.schema), node("span", "", "·"), node("span", "", entity.active === false ? "Historical observation" : "In latest snapshot")); body.append(schema);
  const facts = node("dl", "card-facts");
  if (entity.schema === "Sanction") facts.append(fact("AUTHORITY", first(entity, "authority")), fact("LISTED", first(entity, "listingDate")));
  else if (["Company", "Organization", "LegalEntity"].includes(entity.schema)) facts.append(fact("JURISDICTION", first(entity, "jurisdiction", "country")), fact("REGISTRATION", first(entity, "registrationNumber", "idNumber")));
  else facts.append(fact("COUNTRY / NATIONALITY", first(entity, "nationality", "country", "jurisdiction")), fact(entity.schema === "Person" ? "DATE OF BIRTH" : "IDENTIFIER", first(entity, entity.schema === "Person" ? "birthDate" : "registrationNumber", "idNumber")));
  body.append(facts);
  const footer = node("footer", "card-footer");
  const source = node("span", "source-label", `◉ ${sourceLabel(entity.source)}`); source.title = entity.source;
  footer.append(source, recordButton(entity, "open-record", "View record ↗"));
  article.append(top, body, footer);
  return article;
}
function recordTable(items) {
  const table = node("table", "record-table");
  const head = node("thead"), row = node("tr");
  for (const label of ["NAME / ENTITY", "CATEGORY", "SOURCE", "LOCAL OBSERVATION"]) { const th = node("th", "", label); th.scope = "col"; row.append(th); }
  head.append(row); table.append(head);
  const body = node("tbody");
  for (const entity of items) {
    const tr = node("tr"); tr.dataset.recordId = entity.id;
    const name = node("td"); name.append(recordButton(entity, "record-name", entity.caption || entity.id), node("div", "record-schema", entity.schema));
    const category = node("td"); category.append(badges(entity));
    tr.append(name, category, node("td", "", sourceLabel(entity.source)), node("td", "muted", displayDate(entity.observed_at)));
    body.append(tr);
  }
  table.append(body); return table;
}
function renderRecords() {
  const container = $("records"); container.replaceChildren();
  container.classList.toggle("table-view", state.mode === "table");
  if (state.mode === "table" && state.items.length) container.append(recordTable(state.items));
  else state.items.forEach((entity) => container.append(card(entity)));
  $("result-count").textContent = `${count(state.total)} ${state.total === 1 ? "record" : "records"}`;
  $("result-context").textContent = $("inactive-filter").checked ? "Includes historical observations" : "Latest local observations";
  $("historical-note").hidden = !$("inactive-filter").checked;
  $("export-button").disabled = state.busy || state.total === 0 || $("inactive-filter").checked;
  $("export-button").title = $("inactive-filter").checked ? "Clear Include historical records before exporting a current snapshot" : "Download FtM JSONL and its attribution manifest together";
  $("empty-state").hidden = state.total !== 0;
  const firstRun = !(state.stats?.total > 0);
  $("empty-title").textContent = firstRun ? "Start with a source." : "No matching records.";
  $("empty-eyebrow").textContent = firstRun ? "READY WHEN YOU ARE" : "REFINE YOUR SEARCH";
  $("empty-description").textContent = firstRun ? "Bring public records into your local workspace, then search, explore connections and export in FollowTheMoney format." : "Try another name or identifier, choose a different source, or clear the filters to explore the workspace.";
  $("empty-connect").hidden = !firstRun; $("demo-button").hidden = !firstRun; $("empty-reset").hidden = firstRun; $("empty-note").hidden = !firstRun;
  $("pagination").hidden = !state.total;
  $("page-label").textContent = `${count(state.offset + 1)}–${count(Math.min(state.offset + state.items.length, state.total))} of ${count(state.total)} records`;
  $("previous-page").disabled = state.offset === 0 || state.loading;
  $("next-page").disabled = state.offset + state.limit >= state.total || state.loading;
}
function fillSelect(select, items, placeholder) {
  const previous = select.value;
  select.replaceChildren(new Option(placeholder, ""));
  for (const [value, label] of items) select.append(new Option(label, value));
  if (Array.from(select.options).some((option) => option.value === previous)) select.value = previous;
}
function renderStats() {
  const stats = state.stats;
  $("stat-active").textContent = count(stats.active); $("stat-wanted").textContent = count(stats.kinds?.wanted); $("stat-sanction").textContent = count(stats.kinds?.sanction);
  $("stat-sources").textContent = count(stats.sources?.length); $("nav-count").textContent = count(stats.active);
  $("stat-updated").textContent = stats.last_sync ? `Last local sync ${displayDate(stats.last_sync)}` : "No local snapshots yet";
  fillSelect($("source-filter"), (stats.sources || []).map((source) => [source.id, source.label || source.metadata?.label || source.id]), "All sources");
  fillSelect($("schema-filter"), Object.keys(stats.schemas || {}).sort().map((schema) => [schema, schema]), "All schemas");
  $("demo-banner").hidden = !(stats.sources || []).some((source) => sourceIsDemo(source.id));
  renderSources();
}
async function loadRecords(reset = false) {
  if (reset) state.offset = 0;
  const requestId = ++state.searchId;
  const params = filters(); params.set("limit", state.limit); params.set("offset", state.offset);
  state.loading = true; $("records").setAttribute("aria-busy", "true");
  $("previous-page").disabled = true; $("next-page").disabled = true;
  try {
    const result = await api(`/api/entities?${params}`);
    if (requestId !== state.searchId) return;
    state.items = result.items; state.total = result.total; state.offset = result.offset;
    if (state.total && state.offset >= state.total) { state.offset = Math.max(0, Math.floor((state.total - 1) / state.limit) * state.limit); return loadRecords(); }
    state.loading = false; renderRecords();
  } catch (error) { if (requestId === state.searchId) { notify(errorMessage(error), true); $("result-count").textContent = "Could not load records"; } }
  finally { if (requestId === state.searchId) { state.loading = false; $("records").removeAttribute("aria-busy"); } }
}
async function refresh() {
  state.stats = await api("/api/stats"); renderStats();
  await loadRecords();
  if (state.view === "sources") await loadHistory();
}
function setView(view) {
  if (!["records", "sources", "guide"].includes(view)) view = "records";
  state.view = view;
  for (const name of ["records", "sources", "guide"]) $("view-" + name).hidden = name !== view;
  for (const button of document.querySelectorAll("[data-view]")) {
    const selected = button.dataset.view === view;
    button.classList.toggle("selected", selected);
    if (selected) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  }
  $("breadcrumb").textContent = {records: "RECORDS", sources: "DATA SOURCES", guide: "WORKBENCH GUIDE"}[view];
  history.replaceState(null, "", `#${view}`);
  if (view === "sources" && state.config) loadHistory().catch((error) => notify(errorMessage(error), true));
}
function resetFilters() {
  $("search").value = ""; $("source-filter").value = ""; $("kind-filter").value = ""; $("schema-filter").value = ""; $("inactive-filter").checked = false;
  loadRecords(true);
}
function setBusy(value) {
  state.busy = value;
  for (const id of ["sync-button", "demo-button", "source-demo-button"]) $(id).disabled = value || !state.config;
  $("import-button").disabled = value || !state.config || !$("import-file").files.length;
  $("export-button").disabled = value || !state.total || $("inactive-filter").checked;
  document.querySelectorAll("[data-download-files]").forEach((button) => { button.disabled = value; });
}
async function loadDemo() {
  if (state.busy) return; setBusy(true);
  try { await api("/api/demo", {}); await refresh(); setView("records"); resetFilters(); $("source-filter").value = "demo"; await loadRecords(true); notify("Fictional demonstration records loaded. Every example is labeled as demo data."); }
  catch (error) { notify(errorMessage(error), true); }
  finally { setBusy(false); }
}
function datasetDescription() {
  if (!state.config) return;
  const isOpenSanctions = $("sync-provider").value === "opensanctions";
  $("dataset-label").hidden = !isOpenSanctions;
  $("dataset-description").textContent = isOpenSanctions ? (state.config.sourcePresets.find((preset) => preset.id === $("sync-dataset").value)?.description || "") : "Fetch the full FBI Wanted API snapshot. The API also includes missing persons and other notice categories.";
  if (isOpenSanctions && $("sync-dataset").value === "default") $("dataset-description").textContent += " This is a large collection and can take considerable time and disk space. You can continue browsing while it downloads.";
}
async function runJob(path, payload, title) {
  if (state.busy) return;
  setBusy(true); $("job-title").textContent = title; $("job-message").textContent = "Starting…"; $("job-status").hidden = false;
  const inlineStatus = path === "/api/files" ? $("detail-file-status") : null;
  if (inlineStatus) { inlineStatus.hidden = false; inlineStatus.textContent = "Downloading official source files…"; }
  try {
    const result = await api(path, payload);
    let failures = 0;
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 1200));
      let job;
      try { job = await api(`/api/jobs/${encodeURIComponent(result.job.id)}`); failures = 0; }
      catch (error) { failures += 1; if (failures >= 5) throw new Error(`Lost contact with this job. It may still be running. Refresh the workspace to check its result. ${errorMessage(error)}`); $("job-message").textContent = "Reconnecting to local job…"; continue; }
      $("job-message").textContent = job.message || "Working…";
      if (inlineStatus?.isConnected) inlineStatus.textContent = job.message || "Downloading…";
      if (job.status === "failed") throw new Error(job.error || job.message || "The source update failed.");
      if (job.status === "complete") {
        await refresh();
        if (path === "/api/files" && state.detail?.id === payload.id && $("detail-dialog").open) await openDetail(payload.id, payload.source);
        notify(path === "/api/files" ? `File download finished. ${count(job.result?.downloaded)} files available; inspect each file's status below.` : "Source sync completed. The latest local snapshot is ready to explore.");
        break;
      }
    }
  } catch (error) { notify(errorMessage(error), true); if (inlineStatus?.isConnected) { inlineStatus.textContent = errorMessage(error); inlineStatus.setAttribute("role", "alert"); } }
  finally { $("job-status").hidden = true; setBusy(false); }
}
async function importFile(event) {
  event.preventDefault(); if (state.busy) return;
  const file = $("import-file").files[0]; if (!file) { notify("Choose a FollowTheMoney JSONL file first.", true); return; }
  if (file.size > 20 * 1024 * 1024) { notify("This file exceeds the browser import limit of 20 MiB. Use the CLI import command for a larger file.", true); return; }
  setBusy(true);
  try {
    const content = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
    const result = await api("/api/import", {filename: file.name, content});
    await refresh(); resetFilters(); $("source-filter").value = result.source || ""; setView("records"); await loadRecords(true);
    notify(`Imported ${file.name}. The original file and its source attribution are preserved in the local workspace.`);
    $("import-form").reset(); $("import-file-label").textContent = "No file selected";
  } catch (error) { notify(error instanceof TypeError ? "The file is not valid UTF-8 text. Choose an uncompressed UTF-8 JSONL file." : errorMessage(error), true); }
  finally { setBusy(false); }
}
function renderSources() {
  const sources = state.stats.sources || []; const list = $("sources-list"); list.replaceChildren(); $("source-count").textContent = count(sources.length);
  if (!sources.length) list.append(node("div", "source-empty", "No sources connected yet. Sync a public source, import an FtM file, or load the fictional demo."));
  for (const source of sources) {
    const metadata = source.metadata || {}; const card = node("article", "source-card");
    const header = node("header"); header.append(node("h3", "", source.label || metadata.label || source.id));
    header.append(node("span", `badge ${metadata.demo ? "demo" : ""}`, metadata.demo ? "FICTIONAL DEMO" : source.complete ? "COMPLETE SNAPSHOT" : "PARTIAL SNAPSHOT"));
    card.append(header, node("div", "source-id mono", source.id));
    const dl = node("dl");
    dl.append(fact("LOCAL RECORDS", `${count(source.active_count)} current · ${count(source.entity_count)} total`), fact("SNAPSHOT STORED", displayDate(source.updated_at)), fact("FETCHED FROM SOURCE", displayDate(metadata.fetched_at)), fact("LICENSE", metadata.license || metadata.licence || "Not supplied — check the source"));
    if (metadata.attribution) dl.append(fact("ATTRIBUTION", typeof metadata.attribution === "string" ? metadata.attribution : JSON.stringify(metadata.attribution)));
    card.append(dl);
    const actions = node("div", "source-actions"); const view = node("button", "text-button", "Explore source records ↗");
    view.addEventListener("click", () => { resetFilters(); $("source-filter").value = source.id; setView("records"); loadRecords(true); }); actions.append(view);
    if (safeURL(metadata.source_url)) actions.append(externalLink(metadata.source_url, "Source website ↗"));
    card.append(actions); list.append(card);
  }
}
async function loadHistory() {
  const result = await api("/api/history"); const list = $("history-list"); list.replaceChildren();
  if (!result.items.length) { list.append(node("p", "muted", "No completed imports yet.")); return; }
  for (const item of result.items.slice(0, 20)) {
    const row = node("div", "history-row");
    row.append(node("span", "", sourceLabel(item.source)), node("span", "muted", `${count(item.count)} records · ${item.complete ? "complete snapshot" : "partial snapshot"}`), node("time", "", displayDate(item.completed_at || item.started_at)));
    list.append(row);
  }
}
function propertyList(properties, prettify = false) {
  const dl = node("dl", "property-list");
  for (const [key, values] of Object.entries(properties || {})) {
    const row = node("div", "property-row"), dd = node("dd");
    for (const value of Array.isArray(values) ? values : [values]) {
      const text = typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "");
      const line = node("div", "property-value");
      if (safeURL(text)) line.append(externalLink(text)); else line.textContent = text;
      dd.append(line);
    }
    row.append(node("dt", "", prettify ? key.replace(/([A-Z])/g, " $1").replace(/^./, (letter) => letter.toUpperCase()) : key), dd); dl.append(row);
  }
  return dl;
}
function detailSection(title) { const section = node("section", "detail-section"); section.append(node("h3", "", title)); return section; }
function relatedButton(entity) {
  const button = recordButton(entity, "related-record", "");
  button.append(node("strong", "", entity.caption || entity.id), node("span", "badge", entity.schema || "Entity"));
  if (entity.active === false) button.append(node("span", "badge historical", "Historical / absent from latest snapshot"));
  else button.append(node("span", "badge", "In latest local snapshot"));
  return button;
}
function renderDetail(result) {
  const entity = result.entity; state.detail = entity;
  const container = $("detail-content"); container.replaceChildren();
  const heading = node("header", "detail-heading");
  const tagGroup = badges(entity); tagGroup.className = "detail-badges";
  const title = node("h2", "", entity.caption || entity.id); title.id = "detail-title";
  heading.append(tagGroup, title, node("div", "mono", `${entity.schema} · ${entity.id}`)); container.append(heading);
  if (sourceIsDemo(entity.source)) container.append(node("p", "detail-notice", "Fictional demonstration record. This example does not describe a real person, company or official listing."));
  if (entity.active === false) container.append(node("p", "detail-notice", "Historical record: absent from the latest complete local snapshot. This does not by itself establish why the record was removed or its present legal status."));
  if (result.resolved_from) container.append(node("p", "detail-notice", `The source identifies ${result.resolved_from} as a superseded identifier for this record.`));
  if (entity.replaced_by?.length) { const section = detailSection("Source-declared successor records"); entity.replaced_by.forEach((successor) => section.append(relatedButton(successor))); container.append(section); }
  const source = detailSection("Source & observation"); const metadata = entity.source_metadata || {};
  const provenance = {"Source": sourceLabel(entity.source), "Source ID": entity.source, "Snapshot membership": entity.active === false ? "Historical — absent from latest snapshot" : "Present in latest local snapshot", "Observed locally": displayDate(entity.observed_at), "Source snapshot stored": displayDate(entity.source_updated_at), "Source fetched": displayDate(metadata.fetched_at)};
  if (entity.datasets?.length) provenance.Datasets = entity.datasets;
  if (metadata.license || metadata.licence) provenance.License = metadata.license || metadata.licence;
  if (metadata.attribution) provenance.Attribution = metadata.attribution;
  if (metadata.source_url) provenance["Source website"] = metadata.source_url;
  source.append(propertyList(provenance)); container.append(source);
  const properties = detailSection(`FollowTheMoney properties · ${Object.keys(entity.properties || {}).length}`);
  properties.append(propertyList(entity.properties, true)); container.append(properties);
  const related = detailSection(`Related records · ${count(result.related_total ?? (result.related || []).length)}`);
  related.append(node("p", "help-text", "Relationships declared by this source. Historical relationships are labeled separately."));
  if (result.related_truncated) related.append(node("p", "help-text", `Showing first ${count((result.related || []).length)} of ${count(result.related_total)} related records. Export the whole source to preserve all relationships.`));
  if (result.related?.length) result.related.forEach((record) => related.append(relatedButton({...record, source: record.source || entity.source})));
  else related.append(node("p", "help-text", "No connected records are present in this local source snapshot."));
  container.append(related);
  const files = detailSection("Source files & attachments");
  if (entity.files?.length) {
    for (const file of entity.files) {
      const row = node("div", "file-row");
      const title = file.name || file.filename || file.title || file.caption || file.url || "Source attachment";
      row.append(node("strong", "", title));
      const status = file.status || (file.local_url ? "available locally" : "source link");
      row.append(node("p", "", `${status}${file.size ? ` · ${count(file.size)} bytes` : ""}`));
      if (file.error) row.append(node("p", "", file.error));
      const local = localFileLink(file.local_url, "Download local file ↓"); if (local) row.append(local);
      if (safeURL(file.url)) row.append(externalLink(file.url, "View source file ↗"));
      if (file.sha256) row.append(node("p", "file-hash", `SHA-256 ${file.sha256}`));
      files.append(row);
    }
  } else files.append(node("p", "help-text", "No attachments are listed for this record."));
  const officialWantedURL = (value) => {
    try { const url = new URL(value); return url.protocol === "https:" && ["fbi.gov", "www.fbi.gov"].includes(url.hostname) && url.pathname.startsWith("/wanted/"); } catch { return false; }
  };
  const officialFileURL = (file) => {
    try { const url = new URL(file.url); return url.protocol === "https:" && ["fbi.gov", "www.fbi.gov", "api.fbi.gov"].includes(url.hostname); } catch { return false; }
  };
  const canDownload = entity.source === "fbi" || entity.source.startsWith("fbi:") || (entity.properties?.sourceUrl || []).some(officialWantedURL) || (entity.files || []).some(officialFileURL);
  if (canDownload) {
    const download = node("button", "button", "↓ Download official source files"); download.dataset.downloadFiles = "true"; download.disabled = state.busy;
    download.addEventListener("click", () => runJob("/api/files", {id: entity.id, source: entity.source}, "Downloading official source files")); files.append(download);
    files.append(node("p", "help-text", "Files are saved locally from supported official source hosts. Check per-file results after the download."));
    const status = node("p", "detail-notice"); status.id = "detail-file-status"; status.hidden = true; status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite"); files.append(status);
  } else files.append(node("p", "help-text", "Automatic downloads support FBI Wanted API records and records with an official FBI wanted-page source link."));
  container.append(files);
  const raw = node("details", "details-raw"); raw.append(node("summary", "", "Inspect raw FtM record"));
  const ftm = {}; for (const key of ["id", "schema", "caption", "properties", "datasets", "referents", "target", "first_seen", "last_seen", "last_change"]) if (entity[key] !== undefined) ftm[key] = entity[key];
  raw.append(node("pre", "", JSON.stringify(ftm, null, 2))); container.append(raw);
  container.append(node("p", "detail-notice", state.config.disclaimer));
}
async function openDetail(id, source) {
  const requestId = ++state.detailId;
  const dialog = $("detail-dialog");
  if (!dialog.open) dialog.showModal();
  const loading = node("h2", "loading-line", "Loading record…"); loading.id = "detail-title"; $("detail-content").replaceChildren(loading);
  try {
    const result = await api(`/api/entities/${encodeURIComponent(id)}?${new URLSearchParams({source: source || ""})}`);
    if (requestId === state.detailId) { renderDetail(result); dialog.scrollTop = 0; }
  } catch (error) {
    if (requestId !== state.detailId) return;
    const message = node("h2", "loading-line", errorMessage(error)); message.id = "detail-title"; $("detail-content").replaceChildren(message);
  }
}
async function downloadResponse(path, filename, button) {
  if (button.disabled) return; button.disabled = true;
  try {
    const response = await fetch(path, {cache: "no-store"});
    if (!response.ok) { let data; try { data = await response.json(); } catch { data = {}; } throw new Error(data.error || `Export failed (${response.status}).`); }
    let url; const large = Number(response.headers.get("Content-Length")) > 96 * 1024 * 1024;
    if (large) {
      await response.body.cancel();
      url = path;
    } else url = URL.createObjectURL(await response.blob());
    const link = node("a"); link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove();
    if (!large) setTimeout(() => URL.revokeObjectURL(url), 60000);
    notify(large ? "Large export prepared. Your browser is downloading it directly to disk. Keep this server running until the download finishes." : "Export ready. Your browser is downloading the file.");
  } catch (error) { notify(errorMessage(error), true); }
  finally { button.disabled = button.id === "export-button" ? state.busy || !state.total || $("inactive-filter").checked : false; }
}

$("dismiss-notification").addEventListener("click", () => { $("notification").hidden = true; });
document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
document.querySelectorAll("[data-go]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.go)));
window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
$("search-form").addEventListener("submit", (event) => { event.preventDefault(); loadRecords(true); });
let searchTimer;
$("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadRecords(true), 300); });
for (const id of ["source-filter", "kind-filter", "schema-filter", "inactive-filter"]) $(id).addEventListener("change", () => loadRecords(true));
$("clear-filters").addEventListener("click", resetFilters); $("empty-reset").addEventListener("click", resetFilters);
$("empty-connect").addEventListener("click", () => setView("sources"));
$("refresh-button").addEventListener("click", () => refresh().catch((error) => notify(errorMessage(error), true)));
$("demo-button").addEventListener("click", loadDemo); $("source-demo-button").addEventListener("click", loadDemo);
$("hide-demo").addEventListener("click", () => { const source = state.stats.sources.find((item) => !sourceIsDemo(item.id)); if (source) { resetFilters(); $("source-filter").value = source.id; loadRecords(true); } else setView("sources"); });
$("cards-button").addEventListener("click", () => setMode("cards")); $("table-button").addEventListener("click", () => setMode("table"));
function setMode(mode) { state.mode = mode; for (const value of ["cards", "table"]) { $(value + "-button").classList.toggle("active", mode === value); $(value + "-button").setAttribute("aria-pressed", String(mode === value)); } renderRecords(); }
$("previous-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadRecords(); });
$("next-page").addEventListener("click", () => { state.offset += state.limit; loadRecords(); });
$("sync-provider").addEventListener("change", datasetDescription); $("sync-dataset").addEventListener("change", datasetDescription);
$("sync-form").addEventListener("submit", (event) => { event.preventDefault(); runJob("/api/sync", {source: $("sync-provider").value, dataset: $("sync-dataset").value}, "Syncing public source"); });
$("import-file").addEventListener("change", () => { const file = $("import-file").files[0]; $("import-file-label").textContent = file ? `${file.name} · ${count(file.size)} bytes` : "No file selected"; setBusy(state.busy); });
$("import-form").addEventListener("submit", importFile);
$("close-detail").addEventListener("click", () => $("detail-dialog").close());
$("detail-dialog").addEventListener("click", (event) => { if (event.target === $("detail-dialog")) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right) event.target.close(); } });
$("detail-dialog").addEventListener("close", () => { state.detailId += 1; });
$("export-button").addEventListener("click", () => { const params = filters(); params.set("bundle", "1"); downloadResponse(`/api/export?${params}`, "eljeffe-ftm-export.zip", $("export-button")); });
$("manifest-button").addEventListener("click", () => downloadResponse(`/api/manifest?${filters()}`, "eljeffe.manifest.json", $("manifest-button")));

(async function start() {
  setBusy(false); setView(location.hash.slice(1));
  try {
    state.config = await api("/api/config");
    $("version").textContent = `EL JEFFE v${state.config.version}`;
    $("disclaimer-text").textContent = state.config.disclaimer;
    for (const preset of state.config.sourcePresets) $("sync-dataset").append(new Option(preset.label, preset.id));
    datasetDescription(); await refresh(); setBusy(false);
  } catch (error) { notify(errorMessage(error), true); $("result-count").textContent = "Workspace unavailable"; }
})();
