#!/usr/bin/env node
// Conformance mock server for the Desktop Accounting API SDKs.
// Replays conformance/fixtures/scenarios.json over real HTTP and checks every request the SDK
// sends against the scenario's expectations. No dependencies beyond Node.js 18+.
//
//   node conformance/mock-server.mjs [--fixtures path/to/scenarios.json] [--port 0] [--exit-on-stdin-close]
//
// Prints `MOCK_SERVER_URL=http://127.0.0.1:<port>` on its first stdout line, then serves until
// SIGTERM/SIGINT. With --exit-on-stdin-close it also exits when its stdin pipe closes, so it dies
// with the test process that spawned it.
//
//   <url>/s/<scenario>/v1/...            API requests for one scenario (use <url>/s/<scenario> as baseUrl)
//   POST <url>/_control/reset/<scenario> forget previous requests for the scenario
//   GET  <url>/_control/verify/<scenario> -> { ok, errors[], requests }

import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const here = dirname(fileURLToPath(import.meta.url));
const fixtures = JSON.parse(readFileSync(opt("fixtures", join(here, "fixtures", "scenarios.json")), "utf8"));
const scenarios = new Map(fixtures.scenarios.map((s) => [s.name, s]));
const state = new Map();

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function fresh(name) {
  const st = { next: 0, repeated: 0, captures: {}, errors: [], requests: 0 };
  state.set(name, st);
  return st;
}

function isMatcher(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v) && Object.keys(v).some((k) => k.startsWith("$"));
}

/** Checks one header or query value against a literal or matcher. Returns an error string or null. */
function check(where, actual, expected, st) {
  if (!isMatcher(expected)) {
    if (actual === undefined) return `${where}: missing, expected ${JSON.stringify(expected)}`;
    return actual === String(expected) ? null : `${where}: got ${JSON.stringify(actual)}, expected ${JSON.stringify(expected)}`;
  }
  if (expected.$absent) return actual === undefined ? null : `${where}: expected absent, got ${JSON.stringify(actual)}`;
  if (actual === undefined) return `${where}: missing`;
  if (expected.$uuid && !UUID.test(actual)) return `${where}: ${JSON.stringify(actual)} is not a UUID`;
  if (expected.$regex && !new RegExp(expected.$regex).test(actual)) return `${where}: ${JSON.stringify(actual)} does not match /${expected.$regex}/`;
  if (expected.$same !== undefined && st.captures[expected.$same] !== actual) {
    return `${where}: got ${JSON.stringify(actual)}, expected the value captured as ${expected.$same} (${JSON.stringify(st.captures[expected.$same])})`;
  }
  if (expected.$capture !== undefined) st.captures[expected.$capture] = actual;
  return null;
}

function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null || typeof a !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) return a.length === b.length && a.every((x, i) => deepEqual(x, b[i]));
  const ka = Object.keys(a).sort();
  const kb = Object.keys(b).sort();
  return deepEqual(ka, kb) && ka.every((k) => deepEqual(a[k], b[k]));
}

function compare(st, exp, req, url, body) {
  const errors = [];
  const where = `request ${st.requests}`;
  if (req.method !== exp.method) errors.push(`${where}: method ${req.method}, expected ${exp.method}`);
  if (url.pathname !== exp.path) errors.push(`${where}: path ${url.pathname}, expected ${exp.path}`);
  if (exp.query) {
    const keys = new Set([...url.searchParams.keys()]);
    for (const k of keys) if (!(k in exp.query)) errors.push(`${where}: unexpected query parameter ${k}=${url.searchParams.getAll(k).join(",")}`);
    for (const [k, v] of Object.entries(exp.query)) {
      const actual = url.searchParams.getAll(k);
      if (isMatcher(v)) {
        if (actual.length > 1) errors.push(`${where}: query ${k} repeated ${actual.length} times`);
        const e = check(`${where} query ${k}`, actual[0], v, st);
        if (e) errors.push(e);
      } else if (!deepEqual(actual, v)) {
        errors.push(`${where}: query ${k}=${JSON.stringify(actual)}, expected ${JSON.stringify(v)}`);
      }
    }
  } else if ([...url.searchParams.keys()].length > 0 && req.method !== "GET") {
    errors.push(`${where}: unexpected query string ${url.search}`);
  }
  for (const [name, v] of Object.entries(exp.headers ?? {})) {
    const raw = req.headers[name.toLowerCase()];
    const e = check(`${where} header ${name}`, Array.isArray(raw) ? raw.join(", ") : raw, v, st);
    if (e) errors.push(e);
  }
  if (exp.body !== undefined) {
    let parsed;
    try {
      parsed = body.length === 0 ? undefined : JSON.parse(body);
    } catch {
      errors.push(`${where}: body is not JSON: ${body.slice(0, 200)}`);
    }
    if (parsed !== undefined && !deepEqual(parsed, exp.body)) errors.push(`${where}: body ${JSON.stringify(parsed)}, expected ${JSON.stringify(exp.body)}`);
    if (parsed === undefined && body.length === 0) errors.push(`${where}: body missing, expected ${JSON.stringify(exp.body)}`);
  }
  return errors;
}

function send(res, status, headers, payload) {
  res.writeHead(status, headers);
  res.end(payload);
}

const server = createServer((req, res) => {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const body = Buffer.concat(chunks).toString("utf8");
    const url = new URL(req.url, "http://localhost");
    const control = /^\/_control\/(reset|verify)\/([^/]+)$/.exec(url.pathname);
    if (control) {
      const [, action, name] = control;
      if (!scenarios.has(name)) return send(res, 404, { "Content-Type": "application/json" }, JSON.stringify({ error: `unknown scenario ${name}` }));
      if (action === "reset") {
        fresh(name);
        return send(res, 204, {}, "");
      }
      const st = state.get(name) ?? fresh(name);
      const sc = scenarios.get(name);
      const errors = [...st.errors];
      const last = sc.exchanges[sc.exchanges.length - 1];
      const consumed = st.next >= sc.exchanges.length || (last?.repeat && st.next >= sc.exchanges.length - 1 && st.repeated > 0);
      if (!consumed) errors.push(`only ${st.next} of ${sc.exchanges.length} expected requests were sent`);
      return send(res, 200, { "Content-Type": "application/json" }, JSON.stringify({ ok: errors.length === 0, errors, requests: st.requests }));
    }
    const m = /^\/s\/([^/]+)(\/.*)$/.exec(url.pathname);
    if (!m || !scenarios.has(m[1])) return send(res, 404, { "Content-Type": "text/plain" }, `no scenario for ${url.pathname}`);
    const [, name, rest] = m;
    const sc = scenarios.get(name);
    const st = state.get(name) ?? fresh(name);
    st.requests++;
    const apiUrl = new URL(rest + url.search, "http://localhost");
    let ex = sc.exchanges[st.next];
    if (ex) {
      st.next++;
      if (ex.repeat) st.repeated++;
    } else {
      const last = sc.exchanges[sc.exchanges.length - 1];
      if (last?.repeat) {
        ex = last;
        st.repeated++;
      }
    }
    if (!ex) {
      st.errors.push(`request ${st.requests}: unexpected ${req.method} ${apiUrl.pathname}${apiUrl.search} (no exchange left)`);
      const error = { type: "CONFORMANCE_MISMATCH", code: "UNEXPECTED_REQUEST", message: "The mock server did not expect this request.", userFacingMessage: "", httpStatusCode: 500, integrationCode: null, requestId: "req_conformance", cause: "", fixes: [], docsUrl: "", retryable: false, outcome: "not_applied", param: null, details: {} };
      return send(res, 500, { "Content-Type": "application/json", "Daapi-Should-Retry": "false" }, JSON.stringify({ error }));
    }
    st.errors.push(...compare(st, ex.expect, req, apiUrl, body));
    const r = ex.respond;
    const reply = () => {
      if (r.drop) {
        req.socket.destroy();
        return;
      }
      const payload = r.rawBody ?? (r.body === undefined ? "" : JSON.stringify(r.body));
      try {
        send(res, r.status ?? 200, r.headers ?? {}, payload);
      } catch {
        // The client gave up (client-side timeout); nothing to do.
      }
    };
    if (r.delayMs) setTimeout(reply, r.delayMs);
    else reply();
  });
});

server.keepAliveTimeout = 1000;
server.listen(Number(opt("port", "0")), "127.0.0.1", () => {
  const { port } = server.address();
  process.stdout.write(`MOCK_SERVER_URL=http://127.0.0.1:${port}\n`);
});
const stop = () => server.close(() => process.exit(0));
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
if (args.includes("--exit-on-stdin-close")) {
  process.stdin.on("end", () => process.exit(0));
  process.stdin.resume();
}
