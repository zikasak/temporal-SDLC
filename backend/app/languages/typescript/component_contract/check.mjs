// Trusted tool of TypeScript component contracts. A stub is a `.d.ts` file describing a
// module of a TypeScript part of the repository; this tool compares it with the module
// using the part's own TypeScript compiler and tsconfig.json.
//
//   node check.mjs PROJECT COMPONENTS MODULE...
//       PROJECT is the part's directory, with its dependencies installed; COMPONENTS the
//       contract's directory, holding MODULE.d.ts for each MODULE (a path within the part
//       without its suffix, such as src/github). Each stub is copied beside its module,
//       so that its relative imports resolve as the module's do, type-checked with the
//       part's options (a declaration with a body is an error), and compared with it:
//       - every export of the stub must be an export of the module;
//       - an exported function or constant must have a type mutually assignable with the
//         module's;
//       - an exported interface or type alias must be mutually assignable with the
//         module's (generic ones instantiated with `any`);
//       - an exported class must be a class of the module, and each member the stub
//         declares, its constructor included, must be a member of the module's class with
//         a mutually assignable type;
//       - an exported enum must have the module's members with the module's values.
//       Prints one line per difference and exits 1 when there is any. The files it writes
//       beside the modules are removed again.
//
// Only the tool's own output is trusted: the stubs and the code are not, and nothing of
// either is run.

import { createRequire } from "node:module";
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { basename, join, relative, resolve } from "node:path";

const STUB = ".component-stub";
const CHECK = ".component-check";

const [projectArgument, componentsArgument, ...modules] = process.argv.slice(2);
if (!projectArgument || !componentsArgument || modules.length === 0) {
  console.error("usage: node check.mjs PROJECT COMPONENTS MODULE...");
  process.exit(2);
}
const project = resolve(projectArgument);
const components = resolve(componentsArgument);

let ts;
try {
  ts = createRequire(join(project, "package.json"))("typescript");
} catch {
  console.error(`error: ${project} does not install typescript; run npm ci there first`);
  process.exit(2);
}

const errors = [];
const written = [];

function write(file, text) {
  writeFileSync(file, text);
  written.push(file);
}

function flatten(message) {
  return ts.flattenDiagnosticMessageText(message, " ").replace(/\s+/g, " ").trim();
}

/** The part's compiler options and, with project references, those of the referenced
 * project that holds `file`. */
function configs() {
  const host = { ...ts.sys, onUnRecoverableConfigFileDiagnostic: (diagnostic) => {
    throw new Error(flatten(diagnostic.messageText));
  } };
  const root = ts.getParsedCommandLineOfConfigFile(join(project, "tsconfig.json"), {}, host);
  if (!root) throw new Error("tsconfig.json cannot be read");
  const found = [root];
  for (const reference of root.projectReferences ?? []) {
    const path = ts.resolveProjectReferencePath(reference);
    const parsed = existsSync(path) && ts.getParsedCommandLineOfConfigFile(path, {}, host);
    if (parsed) found.push(parsed);
  }
  return found;
}

function options(parsed) {
  return {
    ...parsed.options, noEmit: true, noUnusedLocals: false, noUnusedParameters: false,
    declaration: false, declarationMap: false, emitDeclarationOnly: false, composite: false,
    incremental: false, tsBuildInfoFile: undefined, sourceMap: false,
  };
}

function exportsOf(checker, file) {
  const symbol = file && checker.getSymbolAtLocation(file);
  const exports = new Map();
  for (const item of symbol ? checker.getExportsOfModule(symbol) : []) {
    exports.set(item.getName(),
      item.flags & ts.SymbolFlags.Alias ? checker.getAliasedSymbol(item) : item);
  }
  return exports;
}

function typeParameterCount(symbol) {
  return Math.max(0, ...(symbol.declarations ?? []).map(
    (declaration) => declaration.typeParameters?.length ?? 0));
}

function instantiated(name, symbol) {
  const count = typeParameterCount(symbol);
  return count ? `${name}<${Array(count).fill("any").join(", ")}>` : name;
}

function isPublic(symbol) {
  return !(symbol.declarations ?? []).some((declaration) =>
    (ts.getCombinedModifierFlags(declaration) & (ts.ModifierFlags.Private | ts.ModifierFlags.Protected))
    || (declaration.name && ts.isPrivateIdentifier(declaration.name)));
}

const SELF = "__component";

/** The lines of a module's check file and what each line checks. */
class Checks {
  constructor(module, stubSpecifier, realSpecifier) {
    this.module = module;
    this.lines = [
      `import type * as Stub from ${JSON.stringify(stubSpecifier)};`,
      `import type * as Real from ${JSON.stringify(realSpecifier)};`,
      `type ${SELF}Constructor<T> = T extends abstract new (...args: infer P) => any ? P : never;`,
    ];
    this.labels = new Map();
    this.count = 0;
  }

  /** `stub` and `real`, two types, are assignable to each other. */
  mutual(label, stub, real) {
    const index = this.count++;
    this.add(`declare const ${SELF}Stub${index}: ${stub};`, label);
    this.add(`export const ${SELF}Real${index}: ${real} = ${SELF}Stub${index};`,
      `${label}: the stub's type is not assignable to the code's`);
    this.add(`declare const ${SELF}Code${index}: ${real};`, label);
    this.add(`export const ${SELF}Check${index}: ${stub} = ${SELF}Code${index};`,
      `${label}: the code's type is not assignable to the stub's`);
  }

  add(line, label) {
    this.labels.set(this.lines.length, label);
    this.lines.push(line);
  }

  text() {
    return this.lines.join("\n") + "\n";
  }
}

function compareEnum(module, name, stub, real, checker) {
  if (!(real.flags & ts.SymbolFlags.Enum)) {
    errors.push(`error: ${module}: ${name} is not an enum of the code`);
    return;
  }
  const values = (symbol) => new Map([...(symbol.exports ?? new Map()).values()]
    .filter((member) => member.flags & ts.SymbolFlags.EnumMember)
    .map((member) => [member.getName(), checker.getConstantValue(member.valueDeclaration)]));
  const stubbed = values(stub);
  const actual = values(real);
  for (const [member, value] of stubbed) {
    if (!actual.has(member)) errors.push(`error: ${module}: ${name}.${member} is not a member of the code's enum`);
    else if (actual.get(member) !== value) {
      errors.push(`error: ${module}: ${name}.${member} is ${JSON.stringify(actual.get(member))} in the code, not ${JSON.stringify(value)}`);
    }
  }
  for (const member of actual.keys()) {
    if (!stubbed.has(member)) errors.push(`error: ${module}: ${name}.${member} of the code's enum is missing from the stub`);
  }
}

function compareClass(checks, name, stub, real, checker) {
  const module = checks.module;
  if (!(real.flags & ts.SymbolFlags.Class)) {
    errors.push(`error: ${module}: ${name} is not a class of the code`);
    return;
  }
  const stubType = instantiated(`Stub.${name}`, stub);
  const realType = instantiated(`Real.${name}`, real);
  for (const member of stub.members?.values() ?? []) {
    const memberName = member.getName();
    if (memberName === "__constructor") {
      checks.mutual(`${name}.constructor`, `${SELF}Constructor<typeof Stub.${name}>`,
        `${SELF}Constructor<typeof Real.${name}>`);
      continue;
    }
    if (!isPublic(member) || memberName.startsWith("__")) continue;
    if (!real.members?.has(memberName) && !checker.getPropertyOfType(checker.getDeclaredTypeOfSymbol(real), memberName)) {
      errors.push(`error: ${module}: ${name}.${memberName} is not a member of the code's class`);
      continue;
    }
    const key = JSON.stringify(memberName);
    checks.mutual(`${name}.${memberName}`, `${stubType}[${key}]`, `${realType}[${key}]`);
  }
  for (const member of stub.exports?.values() ?? []) {
    const memberName = member.getName();
    if (memberName === "prototype" || !isPublic(member)) continue;
    if (!real.exports?.has(memberName)) {
      errors.push(`error: ${module}: ${name}.${memberName} is not a static member of the code's class`);
      continue;
    }
    const key = JSON.stringify(memberName);
    checks.mutual(`${name}.${memberName}`, `(typeof Stub.${name})[${key}]`,
      `(typeof Real.${name})[${key}]`);
  }
}

function check() {
  const found = configs();
  const plans = [];
  for (const module of modules) {
    const stubSource = join(components, `${module}.d.ts`);
    const target = join(project, module);
    if (!existsSync(stubSource)) {
      errors.push(`error: ${module}: the contract has no stub ${module}.d.ts`);
      continue;
    }
    if (relative(project, target).startsWith("..")) {
      errors.push(`error: ${module}: not a module of the part`);
      continue;
    }
    const realSpecifier = `./${basename(module)}`;
    const checkFile = `${target}${CHECK}.ts`;
    // The project whose files hold the module, and the module's file.
    let chosen = null;
    for (const parsed of found) {
      const resolved = ts.resolveModuleName(realSpecifier, checkFile, options(parsed), ts.sys)
        .resolvedModule;
      if (!resolved || resolved.isExternalLibraryImport) continue;
      const file = resolve(resolved.resolvedFileName);
      if (!chosen) chosen = { parsed, file };
      if (parsed.fileNames.some((name) => resolve(name) === file)) {
        chosen = { parsed, file };
        break;
      }
    }
    if (!chosen) {
      errors.push(`error: ${module}: the code has no module ${module}`);
      continue;
    }
    // Copied as a module of the part rather than a declaration file, which the part's
    // skipLibCheck would leave unchecked: a declaration with a body is then an error, and
    // the types it declares without `export` stay its own.
    const stubFile = `${target}${STUB}.ts`;
    write(stubFile, `${readFileSync(stubSource, "utf8")}\nexport {};\n`);
    plans.push({ module, stubFile, checkFile, realSpecifier, ...chosen });
  }
  // One program per project the modules are in.
  for (const parsed of new Set(plans.map((plan) => plan.parsed))) {
    const group = plans.filter((plan) => plan.parsed === parsed);
    const compilerOptions = options(parsed);
    const first = ts.createProgram(group.flatMap((plan) => [plan.stubFile, plan.file]), compilerOptions);
    const checker = first.getTypeChecker();
    const generated = new Map();
    for (const plan of group) {
      const { module } = plan;
      const stubExports = exportsOf(checker, first.getSourceFile(plan.stubFile));
      const realExports = exportsOf(checker, first.getSourceFile(plan.file));
      const checks = new Checks(module, `./${basename(plan.stubFile).replace(/\.ts$/, "")}`,
        plan.realSpecifier);
      if (stubExports.size === 0) errors.push(`error: ${module}: the stub exports nothing`);
      for (const [name, stub] of [...stubExports].sort(([a], [b]) => a.localeCompare(b))) {
        const real = realExports.get(name);
        if (!real) {
          errors.push(`error: ${module}: ${name} is not exported by the code`);
          continue;
        }
        if (stub.flags & ts.SymbolFlags.Enum) compareEnum(module, name, stub, real, checker);
        else if (stub.flags & ts.SymbolFlags.Class) compareClass(checks, name, stub, real, checker);
        else if (stub.flags & ts.SymbolFlags.Value) {
          if (!(real.flags & ts.SymbolFlags.Value)) {
            errors.push(`error: ${module}: ${name} is only a type in the code`);
            continue;
          }
          checks.mutual(name, `typeof Stub.${name}`, `typeof Real.${name}`);
        } else if (stub.flags & ts.SymbolFlags.Type) {
          if (!(real.flags & ts.SymbolFlags.Type)) {
            errors.push(`error: ${module}: ${name} is not a type in the code`);
            continue;
          }
          checks.mutual(name, instantiated(`Stub.${name}`, stub), instantiated(`Real.${name}`, real));
        }
      }
      write(plan.checkFile, checks.text());
      generated.set(resolve(plan.checkFile), checks);
    }
    const files = group.map((plan) => plan.checkFile);
    const program = ts.createProgram(files, compilerOptions);
    const stubs = new Map(group.map((plan) => [resolve(plan.stubFile), plan.module]));
    const diagnostics = [
      ...program.getSyntacticDiagnostics(), ...program.getSemanticDiagnostics(),
      ...program.getGlobalDiagnostics(),
    ];
    for (const diagnostic of diagnostics) {
      if (diagnostic.category !== ts.DiagnosticCategory.Error) continue;
      const file = diagnostic.file && resolve(diagnostic.file.fileName);
      const message = flatten(diagnostic.messageText);
      if (file && stubs.has(file)) {
        const { line } = diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start ?? 0);
        errors.push(`error: ${stubs.get(file)}.d.ts:${line + 1}: ${message}`);
      } else if (file && generated.has(file)) {
        const checks = generated.get(file);
        const { line } = diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start ?? 0);
        errors.push(`error: ${checks.module}: ${checks.labels.get(line) ?? "the stub"}: ${message}`);
      } else if (!file) {
        errors.push(`error: ${message}`);
      }
      // Errors in the code itself are not the contract's.
    }
  }
}

try {
  check();
} catch (error) {
  errors.push(`error: ${error.message}`);
} finally {
  for (const file of written) rmSync(file, { force: true });
}
for (const line of [...new Set(errors)]) console.log(line);
if (errors.length) process.exit(1);
console.log(`The stubs of ${modules.join(", ")} match the code.`);
