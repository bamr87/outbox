// Headless driver for gitorio's REAL compiler. Run by gitorio_compile.sh inside node:22-slim
// with vite-node, cwd = /gitorio/app (a read-only `git archive HEAD` snapshot + npm ci).
//
// Env in:  BP_FILE (blueprint JSON), OUT_DIR (writable), BP_LABEL (path stamped in the header).
// Stdout:  human detail lines go to stderr; the LAST stdout line is one JSON summary.
// Policy:  mirrors gitorio's scripts/regen-examples.ts — any error-level diagnostic means
//          nothing is written (guards are still derived from whatever YAML was emitted).

import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { compile, grantsMerge } from '/gitorio/app/src/compiler/compile.ts';

// gitorio's own YAML dependency (resolved from the snapshot's node_modules).
const YAML = createRequire('/gitorio/app/package.json')('yaml') as {
  parse: (s: string) => unknown;
};

type Level = 'none' | 'read' | 'write';
const RANK: Record<Level, number> = { none: 0, read: 1, write: 2 };

interface Summary {
  ok: boolean;
  workflows: string[];
  errors: string[];
  warnings: string[];
  guards: {
    bot_guard: boolean;
    permissions: Record<string, string>;
    kill_switch_vars: string[];
    never_merges: boolean;
  };
  seconds: number;
}

const t0 = performance.now();
const log = (...a: unknown[]) => console.error('[driver]', ...a);
const summary: Summary = {
  ok: false,
  workflows: [],
  errors: [],
  warnings: [],
  guards: { bot_guard: false, permissions: {}, kill_switch_vars: [], never_merges: true },
  seconds: 0,
};

function finish(code: number): never {
  summary.seconds = Number(((performance.now() - t0) / 1000).toFixed(3));
  console.log(JSON.stringify(summary));
  process.exit(code);
}

const bpFile = process.env.BP_FILE ?? '';
const outDir = process.env.OUT_DIR ?? '';
const label = process.env.BP_LABEL || '.factory/blueprint.json';

let bp: any;
try {
  bp = JSON.parse(readFileSync(bpFile, 'utf8'));
} catch (e) {
  summary.errors.push(`adapter: cannot read blueprint JSON: ${(e as Error).message}`);
  finish(2);
}
// compile() assumes the Blueprint shape; check the minimum so a malformed file is a clean error.
if (bp?.version !== 1 || typeof bp?.meta?.name !== 'string' || !Array.isArray(bp?.nodes) || !Array.isArray(bp?.edges)) {
  summary.errors.push('adapter: not a v1 blueprint (need version:1, meta.name, nodes[], edges[])');
  finish(2);
}

let res: ReturnType<typeof compile>;
try {
  res = compile(bp, { blueprintPath: label });
} catch (e) {
  summary.errors.push(`adapter: compile() threw: ${(e as Error).message}`);
  finish(2);
}

for (const d of res.diagnostics) {
  log(`global [${d.level}]${d.nodeId ? ` (${d.nodeId})` : ''} ${d.message}`);
}
for (const w of res.workflows) {
  for (const d of w.diagnostics) log(`${w.filename} [${d.level}]${d.nodeId ? ` (${d.nodeId})` : ''} ${d.message}`);
}
const diags = [...res.diagnostics, ...res.workflows.flatMap((w) => w.diagnostics)];
summary.errors = diags.filter((d) => d.level === 'error').map((d) => d.message);
summary.warnings = diags.filter((d) => d.level === 'warning').map((d) => d.message);

const emitted = res.workflows.filter((w) => w.yaml.length > 0);
if (res.workflows.length === 0) summary.errors.push('adapter: compiler emitted no workflows (no triggered assembly line)');
log(`compiled ${res.workflows.length} line(s): ${res.workflows.map((w) => `${w.filename}${w.yaml ? '' : ' (REFUSED, no YAML)'}`).join(', ')}`);

// ── guards: derived from the emitted YAML text, not from compiler internals ──
const ALLOWED_RE = /--allowedTools\s+"([^"]*)"/;
for (const w of emitted) {
  const doc = YAML.parse(w.yaml) as any;
  const jobs: Record<string, any> = doc?.jobs ?? {};
  const gateVars: string[] = [];
  let gated = false;
  for (const [jobName, job] of Object.entries(jobs)) {
    const cond = typeof job.if === 'string' ? job.if : '';
    if (cond.includes("github.event.sender.type != 'Bot'")) summary.guards.bot_guard = true;
    if (cond.includes("needs.gate.outputs.go == 'true'")) gated = true;
    for (const [scope, lvl] of Object.entries((job.permissions ?? {}) as Record<string, Level>)) {
      const cur = (summary.guards.permissions[scope] ?? 'none') as Level;
      if ((RANK[lvl] ?? 0) >= RANK[cur]) summary.guards.permissions[scope] = lvl;
    }
    for (const step of (job.steps ?? []) as any[]) {
      if (jobName === 'gate') {
        for (const v of Object.values((step.env ?? {}) as Record<string, string>)) {
          const m = /\$\{\{\s*vars\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/.exec(String(v));
          if (m) gateVars.push(m[1]);
        }
      }
      const run = typeof step.run === 'string' ? step.run : '';
      if (/\bgh\s+pr\s+merge\b|\bgit\s+merge\b|\/merges?\b/.test(run)) {
        summary.guards.never_merges = false;
        log(`${w.filename}: job ${jobName} step "${step.name ?? step.uses}" can MERGE (deterministic): ${run.split('\n')[0]}`);
      }
      const args = typeof step.with?.claude_args === 'string' ? step.with.claude_args : '';
      const tools = (ALLOWED_RE.exec(args)?.[1] ?? '').split(',').map((t) => t.trim()).filter(Boolean);
      const mergeGrants = tools.filter(grantsMerge);
      if (mergeGrants.length) {
        summary.guards.never_merges = false;
        log(`${w.filename}: agent step grants merge: ${mergeGrants.join(', ')}`);
      }
      if (tools.length) log(`${w.filename}: agent allowedTools = ${tools.join(',')}`);
      if (/dispatches/.test(run)) log(`${w.filename}: dispatch step run = ${run}  env = ${JSON.stringify(step.env)}`);
    }
    log(`${w.filename}: job ${jobName} if=${cond || '(none)'} permissions=${JSON.stringify(job.permissions ?? {})}`);
  }
  if (gated) for (const v of gateVars) if (!summary.guards.kill_switch_vars.includes(v)) summary.guards.kill_switch_vars.push(v);
  log(`${w.filename}: on=${JSON.stringify(doc?.on)}`);
}

summary.ok = summary.errors.length === 0 && emitted.length > 0;
if (summary.ok) {
  mkdirSync(outDir, { recursive: true });
  for (const w of emitted) {
    writeFileSync(join(outDir, w.filename), w.yaml);
    summary.workflows.push(w.filename);
  }
} else {
  log('error-level diagnostics present — REFUSING to write any workflow file (regen-examples policy)');
}
finish(summary.ok ? 0 : 1);
