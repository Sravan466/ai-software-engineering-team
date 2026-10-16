// The compile gate's JavaScript/TypeScript reader. Run by `app.build.check` with node.
//
// Reads {typescript, groups: [{name, files: {rel: content}, report: [rel], paths}]}
// from stdin and writes {diagnostics: [{group, path, line, code, message, syntactic}],
// exports: [{group, path, default, named, types}]} — the second is what each reported
// module really exports (#101), from the same program, re-exports followed.
//
// With `codefixes: true` it also writes `fixes` (#103): TypeScript's own code fixes for
// the diagnostics it has one for, from an allow-list — a name declared but not exported
// (2459) — each as the text edits it makes. Nothing that writes new logic is ever on
// the list. (A misspelt import, 2724, is mended in Python instead: TypeScript's
// `fixSpelling` renames the import and leaves every use of the old name broken.)
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
  2459, // Module '"./x"' declares 'Y' locally, but it is not exported.
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

// The only code fixes the platform applies without a model (#103), by the diagnostic
// each answers and the fix TypeScript names it: an `export` keyword, nothing that
// writes code that wasn't there.
const CODEFIXES = new Map([
  [2459, new Set(['fixImportNonExportedMember'])],
]);

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
const exported = [];
const fixes = [];

// A module's exports, as the next file's write call is told them: the default's local
// name ("" when it has none), named values, and types apart from values.
function exportsOf(checker, sf) {
  const moduleSymbol = checker.getSymbolAtLocation(sf);
  if (!moduleSymbol) return null; // a script, or CommonJS: the light reader answers
  const entry = { default: null, named: [], types: [] };
  for (const s of checker.getExportsOfModule(moduleSymbol)) {
    let target = s;
    if (s.flags & ts.SymbolFlags.Alias) {
      try { target = checker.getAliasedSymbol(s); } catch (err) { target = s; }
      // A re-export of something that doesn't exist (`export { default } from './x'`
      // when x has no default): the checker's own error names it; it is not an export.
      // The checker's "unknown" stand-in has no declaration; every real export has one.
      if (!target.declarations || target.declarations.length === 0) continue;
    }
    // `symbolName`, not `escapedName`: the latter is TypeScript's internal form, which
    // writes `__foo` as `___foo`.
    const name = ts.symbolName(s);
    if (s.escapedName === 'default') {
      const decl = (target.declarations || s.declarations || [])[0];
      const local = decl && ts.getNameOfDeclaration(decl);
      const own = target !== s && target.escapedName !== 'default' ? ts.symbolName(target) : '';
      entry.default = own || (local && local.text) || '';
    } else if (target.flags & ts.SymbolFlags.Value || !(target.flags & ts.SymbolFlags.Type)) {
      entry.named.push(name);
    } else {
      entry.types.push(name);
    }
  }
  return entry;
}

// The specifier of the import declaration at `pos`, when the diagnostic is in one.
function importedAs(sf, pos) {
  if (pos === undefined) return null;
  for (const stmt of sf.statements) {
    if (pos < stmt.getStart(sf) || pos > stmt.end) continue;
    const decl = ts.isImportDeclaration(stmt) || ts.isExportDeclaration(stmt) ? stmt : null;
    const spec = decl && decl.moduleSpecifier;
    return spec && ts.isStringLiteral(spec) ? spec.text : null;
  }
  return null;
}

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
  if (input.codefixes) {
    try {
      const service = ts.createLanguageService({
        getScriptFileNames: () => rootNames,
        getScriptVersion: () => '1',
        getScriptSnapshot: (f) => {
          const text = host.readFile(f);
          return text === undefined ? undefined : ts.ScriptSnapshot.fromString(text);
        },
        getCurrentDirectory: () => root,
        getCompilationSettings: () => options,
        getDefaultLibFileName: (o) => ts.getDefaultLibFilePath(o),
        fileExists: host.fileExists,
        readFile: host.readFile,
        directoryExists: host.directoryExists,
        getDirectories: host.getDirectories,
      }, ts.createDocumentRegistry());
      const relOf = (f) => (f.startsWith(`${root}/`) ? f.slice(root.length + 1) : null);
      for (const rel of group.report) {
        const file = `${root}/${rel}`;
        if (!files.has(file)) continue;
        for (const d of service.getSemanticDiagnostics(file)) {
          const allowed = CODEFIXES.get(d.code);
          if (!allowed || d.start === undefined) continue;
          const actions = service.getCodeFixesAtPosition(file, d.start, d.start + (d.length || 0), [d.code], {}, {});
          const action = actions.find((a) => allowed.has(a.fixName) || allowed.has(a.fixId));
          if (!action) continue;
          const changes = action.changes
            .map((c) => ({ path: relOf(c.fileName), edits: c.textChanges.map((t) => ({ start: t.span.start, length: t.span.length, text: t.newText })) }))
            .filter((c) => c.path !== null);
          if (changes.length) fixes.push({ group: group.name, path: rel, code: d.code, fix: action.fixName, description: action.description, changes });
        }
      }
    } catch (err) {
      // A fix the service can't offer is a problem left for the model, never a crash.
    }
  }
  if (input.fixesOnly) continue;
  let checker = null;
  for (const rel of group.report) {
    const sf = program.getSourceFile(`${root}/${rel}`);
    if (!sf) continue;
    const syntactic = program.getSyntacticDiagnostics(sf);
    if (!syntactic.length && input.exports) {
      try {
        checker = checker || program.getTypeChecker();
        const entry = exportsOf(checker, sf);
        if (entry) exported.push({ group: group.name, path: rel, ...entry });
      } catch (err) {
        // Exports are a courtesy to the next write call; a checker that can't say leaves
        // the light reader's answer in place.
      }
    }
    const semantic = syntactic.length ? [] : program.getSemanticDiagnostics(sf);
    for (const d of syntactic) {
      const pos = d.start !== undefined ? sf.getLineAndCharacterOfPosition(d.start) : null;
      out.push({ group: group.name, path: rel, line: pos ? pos.line + 1 : null, code: d.code, message: text(d.messageText), syntactic: true });
    }
    for (const d of semantic) {
      if (!KEEP.has(d.code)) continue;
      let message = text(d.messageText);
      // The module as this file names it (#101), not the checker's in-memory path: told
      // `"/__build__/frontend/components/TodoList"` has no default export, a model
      // imports that path; told `"../components/TodoList"`, it fixes the import.
      const spec = importedAs(sf, d.start);
      if (spec) message = message.replace(new RegExp(`"${VIRTUAL}/[^"]*"`, 'g'), JSON.stringify(spec));
      const name = /Cannot find name '([^']+)'/.exec(message);
      if (name && AMBIENT.has(name[1])) continue;
      const pos = d.start !== undefined ? sf.getLineAndCharacterOfPosition(d.start) : null;
      out.push({ group: group.name, path: rel, line: pos ? pos.line + 1 : null, code: d.code, message, syntactic: false });
    }
  }
}

process.stdout.write(JSON.stringify({ diagnostics: out, exports: exported, fixes }));
