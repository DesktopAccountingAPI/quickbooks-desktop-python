#!/usr/bin/env node
// README sample verification. Extracts every fenced code block from README.md and checks it:
//
// - blocks in the repository's language are compiled or type-checked against the SDK (the local
//   build in `mise run check`, or the exact version installed from the public registry in the
//   release's clean-consumer test);
// - blocks marked `run=<scenario>` are also executed against the conformance mock server
//   (conformance/fixtures/consumer.json) and must send exactly the scenario's requests and print
//   its expected output;
// - `json` blocks must parse;
// - shell, console, text and http blocks are commands or wire examples and are listed, not run;
// - any other block must be marked `skip`.
//
// Markers go in the fence info string after the language, which every renderer (GitHub, npm,
// PyPI, NuGet) ignores:  ```ts harness=webhook   ```python run=quickstart   ```xml skip
//
// A fragment is wrapped in a harness from readme-samples/<name>.<ext> (`default` unless the block
// names another; `harness=none` compiles the block as written). The harness line containing
// {{SAMPLE}} is replaced by the sample, indented like the token; {{NAME}} is a unique identifier
// for the sample. Import lines (`import`, `from ... import`, `using X;`) at the start of a line in
// the sample are hoisted to the top of the file and merged with the harness imports.
//
//   node scripts/readme-samples.mjs --lang ts|python|csharp|java [--readme README.md]
//     [--out .readme-samples] [--harness-dir readme-samples] [--conformance conformance]
//     ts:     [--tsc "npx --no-install tsc"]
//     python: [--mypy "uv run --no-sync mypy"] [--python "uv run --no-sync python"]
//     csharp: --project-ref <csproj> | --package <id> --package-version <version>
//     java:   [--classpath target/classes]
//
// No dependencies beyond Node.js 18+.

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { delimiter, join, resolve } from "node:path";

const argv = process.argv.slice(2);
const opt = (name, def) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 ? argv[i + 1] : def;
};
const lang = opt("lang");
const LANG = {
  ts: { fences: ["ts", "typescript", "js", "javascript"], ext: "ts" },
  python: { fences: ["python", "py"], ext: "py" },
  csharp: { fences: ["csharp", "cs", "c#"], ext: "cs" },
  java: { fences: ["java"], ext: "java" },
};
if (!LANG[lang]) {
  console.error("usage: node scripts/readme-samples.mjs --lang ts|python|csharp|java [options]");
  process.exit(2);
}
const readme = resolve(opt("readme", "README.md"));
const out = resolve(opt("out", ".readme-samples"));
const harnessDir = resolve(opt("harness-dir", "readme-samples"));
const conformance = resolve(opt("conformance", "conformance"));
const COMMANDS = new Set(["sh", "shell", "bash", "console", "powershell", "pwsh", "text", "http"]);
const isWin = process.platform === "win32";

// ---------------------------------------------------------------- parse

/** Fenced code blocks: { line, lang, attrs, code }. */
function parseBlocks(text) {
  const blocks = [];
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const m = /^(\s*)(`{3,}|~{3,})(.*)$/.exec(lines[i]);
    if (!m) continue;
    const [, indent, fence, info] = m;
    const words = info.trim().split(/\s+/).filter(Boolean);
    const attrs = {};
    for (const w of words.slice(1)) {
      const [k, v] = w.split("=");
      attrs[k] = v ?? true;
    }
    const code = [];
    let j = i + 1;
    for (; j < lines.length; j++) {
      const c = new RegExp(`^\\s*${fence[0] === "`" ? "`" : "~"}{${fence.length},}\\s*$`);
      if (c.test(lines[j])) break;
      code.push(lines[j].startsWith(indent) ? lines[j].slice(indent.length) : lines[j].trimStart());
    }
    if (j >= lines.length) throw new Error(`README line ${i + 1}: unclosed code fence`);
    blocks.push({ line: i + 1, lang: (words[0] ?? "").toLowerCase(), attrs, code: code.join("\n") });
    i = j;
  }
  return blocks;
}

// ---------------------------------------------------------------- imports

const importRules = {
  ts: (l) => /^import\s/.test(l),
  python: (l) => /^(import\s|from\s+\S+\s+import\s)/.test(l),
  csharp: (l) => /^using\s+(static\s+)?[\w.]+(\s*=\s*[\w.<>, ]+)?\s*;\s*$/.test(l),
  java: (l) => /^import\s/.test(l),
};

/** Splits code into column-0 import statements and the rest. Multi-line TS imports are joined. */
function splitImports(code) {
  const imports = [];
  const rest = [];
  const lines = code.split("\n");
  for (let i = 0; i < lines.length; i++) {
    let l = lines[i];
    if (!importRules[lang](l)) {
      rest.push(l);
      continue;
    }
    if (lang === "ts") while (!/;\s*$/.test(l) && !/from\s+["'][^"']+["']\s*$/.test(l) && i + 1 < lines.length) l += " " + lines[++i].trim();
    if (lang === "python" && /\($/.test(l.trim())) while (!/\)\s*$/.test(l) && i + 1 < lines.length) l += " " + lines[++i].trim();
    imports.push(l.trim());
  }
  return { imports, rest: rest.join("\n") };
}

/** Merges import statements: named imports from the same module are combined, the rest deduplicated. */
function mergeImports(all) {
  const named = new Map();
  const other = [];
  for (const imp of all) {
    let m;
    if (lang === "ts" && (m = /^import\s+(type\s+)?\{([^}]*)\}\s+from\s+(["'][^"']+["']);?$/.exec(imp))) {
      const key = `${m[1] ? "type " : ""}${m[3]}`;
      if (!named.has(key)) named.set(key, new Set());
      for (const n of m[2].split(",").map((s) => s.trim()).filter(Boolean)) named.get(key).add(n);
    } else if (lang === "python" && (m = /^from\s+(\S+)\s+import\s+\(?([^)]*)\)?$/.exec(imp))) {
      if (!named.has(m[1])) named.set(m[1], new Set());
      for (const n of m[2].split(",").map((s) => s.trim()).filter(Boolean)) named.get(m[1]).add(n);
    } else if (!other.includes(imp)) other.push(imp);
  }
  const merged = [...other];
  for (const [key, names] of named) {
    if (lang === "ts") {
      const type = key.startsWith("type ");
      merged.push(`import ${type ? "type " : ""}{ ${[...names].join(", ")} } from ${type ? key.slice(5) : key};`);
    } else merged.push(`from ${key} import ${[...names].join(", ")}`);
  }
  // `from __future__` must come first in Python.
  return merged.sort((a, b) => Number(b.includes("__future__")) - Number(a.includes("__future__")));
}

function harness(name) {
  if (name === "none") return null;
  const file = join(harnessDir, `${name}.${LANG[lang].ext}`);
  if (!existsSync(file)) {
    if (name === "default") return null;
    throw new Error(`harness ${name} not found: ${file}`);
  }
  return readFileSync(file, "utf8").replace(/\r\n/g, "\n");
}

/** Builds the source file of one sample. */
function render(block, name) {
  // JavaScript blocks in a TypeScript README are complete programs (the harnesses are TypeScript).
  const h = harness(block.attrs.harness ?? (lang === "ts" && /^j/.test(block.lang) ? "none" : "default"));
  const sample = splitImports(block.code);
  if (!h) return { source: [...mergeImports(sample.imports), "", sample.rest].join("\n") + "\n", wrapped: false };
  const hs = splitImports(h.replaceAll("{{NAME}}", name));
  const body = hs.rest.split("\n").flatMap((l) => {
    const t = /^(\s*)(?:\/\/\s*|#\s*)?\{\{SAMPLE\}\}\s*$/.exec(l);
    if (!t) return [l];
    return sample.rest.split("\n").map((s) => (s.trim() ? t[1] + s : ""));
  });
  if (!hs.rest.includes("{{SAMPLE}}")) throw new Error(`harness ${block.attrs.harness ?? "default"} has no {{SAMPLE}} line`);
  return { source: [...mergeImports([...hs.imports, ...sample.imports]), "", ...body].join("\n") + "\n", wrapped: true };
}

// ---------------------------------------------------------------- tools

function words(cmd) {
  return cmd.split(" ").filter(Boolean);
}
function run(cmd, args, options = {}) {
  console.log(`+ ${cmd} ${args.join(" ")}`);
  const r = spawnSync(cmd, args, { stdio: options.capture ? ["ignore", "pipe", "pipe"] : "inherit", encoding: "utf8", shell: isWin, env: { ...process.env, ...options.env }, cwd: options.cwd, timeout: 600_000 });
  if (options.capture) {
    process.stdout.write(r.stdout ?? "");
    process.stderr.write(r.stderr ?? "");
  }
  if (r.status !== 0) throw new Error(`${[cmd, ...args].join(" ").slice(0, 160)} failed (exit ${r.status}${r.error ? `, ${r.error.message}` : ""})`);
  return r.stdout ?? "";
}

/** Runs a sample against a conformance mock-server scenario and checks requests and output. */
async function runAgainstMock(scenarioName, cmd, args) {
  const fixtures = join(conformance, "fixtures", "consumer.json");
  const doc = JSON.parse(readFileSync(fixtures, "utf8"));
  const scenario = doc.scenarios.find((s) => s.name === scenarioName);
  if (!scenario) throw new Error(`scenario ${scenarioName} is not in ${fixtures}`);
  const child = spawn(process.execPath, [join(conformance, "mock-server.mjs"), "--fixtures", fixtures], { stdio: ["ignore", "pipe", "inherit"] });
  try {
    const url = await new Promise((res, rej) => {
      child.stdout.once("data", (d) => res(String(d).trim().split("\n")[0].split("=")[1]));
      child.once("exit", () => rej(new Error("mock server exited")));
    });
    await fetch(`${url}/_control/reset/${scenarioName}`, { method: "POST" });
    const env = { DAAPI_BASE_URL: `${url}/s/${scenarioName}`, DAAPI_SECRET_KEY: doc.apiKey, DAAPI_END_USER_ID: doc.endUserId };
    const stdout = run(cmd, args, { env, capture: true });
    const v = await (await fetch(`${url}/_control/verify/${scenarioName}`)).json();
    if (!v.ok) throw new Error(`requests did not match scenario ${scenarioName}: ${v.errors.join("; ")}`);
    for (const s of scenario.outcome?.stdoutIncludes ?? []) if (!stdout.includes(s)) throw new Error(`output lacks ${JSON.stringify(s)}`);
  } finally {
    child.kill();
  }
}

// ---------------------------------------------------------------- languages

const backends = {
  ts: {
    file: (b, n) => `${n}.${b.lang.startsWith("j") ? (b.code.includes("require(") ? "cjs" : "mjs") : "ts"}`,
    name: (b) => `sample_${b.line}`,
    compile(files) {
      writeFileSync(
        join(out, "tsconfig.json"),
        JSON.stringify(
          {
            compilerOptions: {
              // Strict settings, so a sample compiles in strict consumer projects too.
              target: "es2022", module: "nodenext", moduleResolution: "nodenext", strict: true, noEmit: true,
              exactOptionalPropertyTypes: true, noUncheckedIndexedAccess: true,
              skipLibCheck: true, allowImportingTsExtensions: true, allowJs: true, checkJs: true,
              types: ["node"], lib: ["es2022", "dom"],
            },
            include: files,
          },
          null,
          2,
        ) + "\n",
      );
      const [cmd, ...args] = words(opt("tsc", "npx --no-install tsc"));
      run(cmd, [...args, "-p", join(out, "tsconfig.json")]);
    },
    exec: (file) => [process.execPath, [join(out, file)]],
  },
  python: {
    file: (_b, n) => `${n}.py`,
    name: (b) => `sample_${b.line}`,
    compile(files) {
      const [cmd, ...args] = words(opt("mypy", "uv run --no-sync mypy"));
      run(cmd, [...args, "--strict", "--no-incremental", "--explicit-package-bases", ...files.map((f) => join(out, f))]);
    },
    exec: (file) => {
      const [cmd, ...args] = words(opt("python", "uv run --no-sync python"));
      return [cmd, [...args, join(out, file)]];
    },
  },
  csharp: {
    file: (_b, n) => `${n}.cs`,
    name: (b) => `Sample${b.line}`,
    compile(files, hasProgram) {
      const ref = opt("project-ref")
        ? `<ProjectReference Include="${resolve(opt("project-ref"))}" />`
        : `<PackageReference Include="${opt("package")}" Version="${opt("package-version")}" />`;
      if (!opt("project-ref") && !(opt("package") && opt("package-version"))) throw new Error("csharp needs --project-ref or --package and --package-version");
      // An empty Directory.Build.props stops MSBuild from importing the repository's (or a consumer's) settings.
      writeFileSync(join(out, "Directory.Build.props"), "<Project />\n");
      writeFileSync(
        join(out, "ReadmeSamples.csproj"),
        `<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>${hasProgram ? "Exe" : "Library"}</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <LangVersion>12</LangVersion>
    <Nullable>enable</Nullable>
    <ImplicitUsings>disable</ImplicitUsings>
    <TreatWarningsAsErrors>true</TreatWarningsAsErrors>
    <IsPackable>false</IsPackable>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup>
    <FrameworkReference Include="Microsoft.AspNetCore.App" />
    ${ref}
${files.map((f) => `    <Compile Include="${f}" />`).join("\n")}
  </ItemGroup>
</Project>
`,
      );
      run("dotnet", ["build", join(out, "ReadmeSamples.csproj"), "-c", "Release", "-nologo"]);
    },
    exec: () => ["dotnet", ["run", "--project", join(out, "ReadmeSamples.csproj"), "-c", "Release", "--no-build"]],
  },
  java: {
    file: (_b, n) => join("readmesamples", `${n}.java`),
    name: (b) => `Sample${b.line}`,
    compile(files) {
      const cp = opt("classpath", "target/classes").split(delimiter).map((p) => resolve(p)).join(delimiter);
      run("javac", ["--release", "11", "-Xlint:all", "-Werror", "-encoding", "UTF-8", "-d", join(out, "classes"), "-cp", cp, ...files.map((f) => join(out, f))]);
    },
    exec: (file) => {
      const cp = [join(out, "classes"), ...opt("classpath", "target/classes").split(delimiter).map((p) => resolve(p))].join(delimiter);
      return ["java", ["-cp", cp, file.replace(/\.java$/, "").replace(/[\\/]/g, ".")]];
    },
  },
};

// ---------------------------------------------------------------- main

const backend = backends[lang];
const blocks = parseBlocks(readFileSync(readme, "utf8"));
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
if (lang === "java") mkdirSync(join(out, "readmesamples"), { recursive: true });

const files = [];
const runs = [];
const skipped = [];
const commands = [];
let json = 0;
let programs = 0;
const failures = [];
for (const b of blocks) {
  if (b.attrs.skip) {
    skipped.push(b);
    continue;
  }
  if (COMMANDS.has(b.lang)) {
    commands.push(b);
    continue;
  }
  if (b.lang === "json") {
    try {
      JSON.parse(b.code);
      json++;
    } catch (e) {
      failures.push(`README line ${b.line}: json block does not parse: ${e.message}`);
    }
    continue;
  }
  if (!LANG[lang].fences.includes(b.lang)) {
    failures.push(`README line ${b.line}: \`${b.lang || "(no language)"}\` block is neither checked nor marked skip`);
    continue;
  }
  const name = backend.name(b);
  let { source, wrapped } = render(b, name);
  if (lang === "java") {
    if (!wrapped) {
      const cls = /\bclass\s+(\w+)/.exec(source);
      if (!cls) throw new Error(`README line ${b.line}: harness=none needs a class`);
      source = source.replace(new RegExp(`\\bclass\\s+${cls[1]}\\b`), `class ${name}`);
    }
    source = `package readmesamples;\n\n${source}`;
  }
  if (lang === "csharp" && !wrapped) programs++;
  const file = backend.file(b, name);
  writeFileSync(join(out, file), `${lang === "python" ? "#" : "//"} README.md line ${b.line}\n${source}`);
  files.push(file);
  if (b.attrs.run) runs.push({ block: b, file });
}
if (programs > 1) failures.push("C#: at most one sample may use harness=none (top-level statements); wrap the others");
if (files.length === 0) failures.push(`no ${lang} samples found in ${readme}`);
if (failures.length) {
  for (const f of failures) console.error(`readme-samples: ${f}`);
  process.exit(1);
}

console.log(`readme-samples: ${files.length} ${lang} samples, ${json} json, ${commands.length} command blocks, ${skipped.length} skipped -> ${out}`);
for (const s of skipped) console.log(`  skipped: line ${s.line} (${s.lang})`);
try {
  backend.compile(files, programs > 0);
  for (const r of runs) {
    console.log(`readme-samples: running README line ${r.block.line} against scenario ${r.block.attrs.run}`);
    const [cmd, args] = backend.exec(r.file);
    await runAgainstMock(r.block.attrs.run, cmd, args);
  }
} catch (err) {
  console.error(`readme-samples: FAILED: ${err.message}`);
  console.error(`readme-samples: each file in ${out} starts with the README line of its sample.`);
  process.exit(1);
}
console.log(`readme-samples: OK (${files.length} compiled, ${runs.length} run against the mock server)`);
