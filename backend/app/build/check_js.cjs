// The compile gate's JavaScript/TypeScript reader. Run by `app.build.check` with node.
//
// Reads {typescript, groups: [{name, files: {rel: content}, report: [rel], paths}]}
// from stdin and writes {diagnostics: [{group, path, line, code, message, syntactic}]}.
//
// Nothing is written to disk and nothing is installed: the files live in memory under
// a virtual root, and only TypeScript's own lib files are read from disk. Packages are
// therefore unresolved — which is the point. The platform checks bare imports against
// the dependencies it can pin; this only has to answer what `next build` would trip
// over in the code itself: does it parse, and does every name it uses exist.
'use strict';

const fs = require('fs');
const path = require('path');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const ts = require(input.typescript);

// What survives filtering. Everything else is a type error, and a type error is not a
// build that cannot run: the scaffold's next.config tells Next not to fail on them.
const KEEP = new Set([
  2304, // Cannot find name 'X'.
  2552, // Cannot find name 'X'. Did you mean 'Y'?
  2305, // Module '"./x"' has no exported member 'Y'.
  2724, // '"./x"' has no exported member named 'Y'. Did you mean 'Z'?
  2613, // Module '"./x"' has no default export.
  2614, // Module '"./x"' has no exported member 'Y'. Did you mean to use 'import Y from'?
  1192, // Module '"./x"' has no default export.
]);

// Globals a runtime or test runner provides, which TS cannot see without @types.
const AMBIENT = new Set([
  'require', 'module', 'exports', '__dirname', '__filename', 'process', 'Buffer', 'global',
  'globalThis', 'setImmediate', 'clearImmediate', 'describe', 'it', 'test', 'expect', 'jest',
  'beforeEach', 'afterEach', 'beforeAll', 'afterAll', 'vi', 'JSX', 'NodeJS', 'cy', 'context',
]);

const VIRTUAL = '/__build__';

function scriptKind(file) {
  if (file.endsWith('.tsx')) return ts.ScriptKind.TSX;
  if (file.endsWith('.ts') || file.endsWith('.mts') || file.endsWith('.cts')) return ts.ScriptKind.TS;
  if (file.endsWith('.json')) return ts.ScriptKind.JSON;
  return ts.ScriptKind.JSX; // .js/.jsx/.mjs/.cjs — JSX is legal in all of them here
}

function text(message) {
  return ts.flattenDiagnosticMessageText(message, ' ');
}

const out = [];
for (const group of input.groups) {
  const root = `${VIRTUAL}/${group.name}`;
  const files = new Map(
    Object.entries(group.files).map(([rel, content]) => [`${root}/${rel}`, content])
  );
  const options = {
    allowJs: true,
    checkJs: true,
    jsx: ts.JsxEmit.Preserve,
    noEmit: true,
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    allowImportingTsExtensions: true,
    resolveJsonModule: true,
    esModuleInterop: true,
    allowSyntheticDefaultImports: true,
    skipLibCheck: true,
    strict: false,
    noImplicitAny: false,
    types: [],
    lib: ['lib.es2022.d.ts', 'lib.dom.d.ts', 'lib.dom.iterable.d.ts'],
    baseUrl: root,
    paths: group.paths || {},
  };
  const host = ts.createCompilerHost(options, true);
  const readLib = host.getSourceFile.bind(host);
  const inBuild = (f) => f === root || f.startsWith(`${root}/`) || f.startsWith(`${VIRTUAL}/`);
  host.getSourceFile = (fileName, languageVersion, onError, shouldCreate) => {
    if (files.has(fileName)) {
      return ts.createSourceFile(fileName, files.get(fileName), languageVersion, true, scriptKind(fileName));
    }
    if (inBuild(fileName)) return undefined;
    return readLib(fileName, languageVersion, onError, shouldCreate);
  };
  host.fileExists = (f) => files.has(f) || (!inBuild(f) && !f.includes('/node_modules/') && ts.sys.fileExists(f));
  host.readFile = (f) => (files.has(f) ? files.get(f) : inBuild(f) ? undefined : ts.sys.readFile(f));
  host.directoryExists = (d) =>
    inBuild(d) ? [...files.keys()].some((k) => k.startsWith(`${d}/`)) : !d.includes('/node_modules') && ts.sys.directoryExists(d);
  host.getDirectories = (d) => (inBuild(d) ? [] : ts.sys.getDirectories(d));
  host.getCurrentDirectory = () => root;
  host.writeFile = () => {};

  const rootNames = [...files.keys()].filter((f) => /\.(m|c)?[jt]sx?$/.test(f));
  let program;
  try {
    program = ts.createProgram({ rootNames, options, host });
  } catch (err) {
    out.push({ group: group.name, path: null, line: null, code: 0, message: String(err), syntactic: false, crashed: true });
    continue;
  }
  for (const rel of group.report) {
    const sf = program.getSourceFile(`${root}/${rel}`);
    if (!sf) continue;
    const syntactic = program.getSyntacticDiagnostics(sf);
    const semantic = syntactic.length ? [] : program.getSemanticDiagnostics(sf);
    for (const d of syntactic) {
      const pos = d.start !== undefined ? sf.getLineAndCharacterOfPosition(d.start) : null;
      out.push({ group: group.name, path: rel, line: pos ? pos.line + 1 : null, code: d.code, message: text(d.messageText), syntactic: true });
    }
    for (const d of semantic) {
      if (!KEEP.has(d.code)) continue;
      const message = text(d.messageText);
      const name = /Cannot find name '([^']+)'/.exec(message);
      if (name && AMBIENT.has(name[1])) continue;
      const pos = d.start !== undefined ? sf.getLineAndCharacterOfPosition(d.start) : null;
      out.push({ group: group.name, path: rel, line: pos ? pos.line + 1 : null, code: d.code, message, syntactic: false });
    }
  }
}

process.stdout.write(JSON.stringify({ diagnostics: out }));
