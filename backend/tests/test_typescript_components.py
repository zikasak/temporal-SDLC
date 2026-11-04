"""Component contracts of TypeScript parts of a repository: `.d.ts` stubs read on the host,
the parts found beside the service, and the trusted check.mjs that compares stubs with the
code using the part's own compiler."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.implementation.activities import is_service_file
from app.languages import get_language, get_part_language
from app.languages.typescript import components as typescript
from app.routing.parts import SERVICE_PART, component_parts

CONTRACT = get_part_language('typescript').component_contract
FRONTEND = Path(__file__).resolve().parents[2] / 'frontend'

# The seams of this repository's web client that plans/part-component-contracts.md routes
# TR-6 and FR-115 to: the GitHub App manifest, and which flows the list shows as waiting.
GITHUB_STUB = '''/**
 * Leaves for GitHub's confirmation page of an app described by this instance's manifest:
 * posts a form with a hidden `manifest` input whose JSON subscribes the app to
 * `issue_comment`, `pull_request_review_comment` and `pull_request` events.
 */
export declare function createGitHubApp(name: string, organization: string): void;
'''
FLOWS_STUB = '''import type { RepositorySelection } from "./github";

// The flow list's own types, which the module does not export: the shapes its export uses.
type Flow = {
  workflow_id: string;
  run_id: string;
  workflow_type: string;
  status: string;
  parent_workflow_id: string | null;
  start_time: string;
  close_time: string | null;
  repository: RepositorySelection | null;
  branch: string | null;
  onboarding_id: string | null;
};

/** Something a flow waits for from the user, and the session view that asks for it. */
type Attention = {
  workflow_id: string
  scope: string
  label: string
  link: string
}

/**
 * What the session `body` of `flow` waits for from the user. A closed flow waits for
 * nothing: its delivery's unresolved findings are only marked, never listed as waiting.
 */
export declare function sessionAttention(flow: Flow, body: unknown): Attention[];
'''


def test_the_stubs_of_the_web_clients_seams_are_read_as_their_exports():
    assert CONTRACT.module(Path('src/github.d.ts')) == 'src/github'
    assert CONTRACT.read_stub('src/github', GITHUB_STUB) == {'createGitHubApp'}
    # The types the stub declares without `export` are not symbols of the module.
    assert CONTRACT.read_stub('src/Flows', FLOWS_STUB) == {'sessionAttention'}
    statements = CONTRACT.statements(FLOWS_STUB)
    assert statements[0] == '||import type { RepositorySelection } from "./github"'
    assert statements[-1] == (
        '|What the session `body` of `flow` waits for from the user. A closed flow waits for '
        'nothing: its delivery\'s unresolved findings are only marked, never listed as '
        'waiting.|export declare function sessionAttention ( flow : Flow , body : unknown ) '
        ': Attention [ ]')
    # Layout and comments are not a change; documentation is.
    assert CONTRACT.statements(FLOWS_STUB.replace('\n  ', '\n      ').replace(
        '// The flow list', '// Other words')) == statements
    assert CONTRACT.statements(FLOWS_STUB.replace('waits for\n * nothing', 'waits for\n * all')
                               ) != statements


DECLARATIONS = '''/** How a room is. */
export declare enum Mode { Open = "open", Closed = -1 }
export interface Room { id: string; readonly closed?: boolean
  get(key: string): number
  [k: string]: unknown
  (x: number): string
  new (x: number): Room
}
export type Pair<T = string> = { left: T; right: T };
export type Key = `room-${string}`
export declare abstract class Rooms<T extends object = {}> extends Base implements Room {
  static readonly LIMIT: 3;
  private secret;
  constructor(name: string, limit?: number);
  /** Closes the room. */
  close(room: Room, merged: boolean): Room | { a: (b: string) => void };
  get count(): number;
  delete(): void
}
export declare const VERSION = 2, NAME: string;
import type { X } from "./x";
export declare function f<T>(x: T extends string ? { a: T } : never): asserts x is string;
'''


def test_a_stub_declares_exports_with_members_of_classes_interfaces_and_enums():
    assert CONTRACT.read_stub('m', DECLARATIONS) == {
        'Mode', 'Mode.Open', 'Mode.Closed', 'Room', 'Room.id', 'Room.closed', 'Room.get',
        'Pair', 'Key', 'Rooms', 'Rooms.LIMIT', 'Rooms.secret', 'Rooms.constructor',
        'Rooms.close', 'Rooms.count', 'Rooms.delete', 'VERSION', 'NAME', 'f'}
    # One statement per declaration and member, so a published stub may gain members.
    assert 'Room.||[ k : string ] : unknown' in CONTRACT.statements(DECLARATIONS)
    assert 'Rooms.|Closes the room.|close ( room : Room , merged : boolean ) : Room | { a : ' \
           '( b : string ) => void }' in CONTRACT.statements(DECLARATIONS)


@pytest.mark.parametrize('source, error', [
    ('export declare function f(): void { return; }', 'holds no body'),
    ('export declare function f() { }', 'function f has a body'),
    ('export declare class A { m(): void { } }', 'holds no body'),
    ('export declare class A { m() { x() } }', 'the body of m is more than a signature'),
    ('export declare class A { x = compute(); }', 'no initializer of a member'),
    ('export declare class A { static { run(); } }', 'has a body'),
    ('export declare const x = compute();', 'the value of x is not a literal'),
    ('export const x = 1 + 2;', 'the value of x is not a literal'),
    ('export declare const f: () => void = () => {};', 'f has a type and a value'),
    ('import { x } from "y";\nexport declare const a: number;', 'imports types only'),
    ('export default function f(): void;', 'no default export and no re-export'),
    ('export { x } from "./y";', 'no default export and no re-export'),
    ('export * from "./y";', 'no default export and no re-export'),
    ('export declare namespace N { }', "not 'namespace'"),
    ('export type X = { a: string; if (x) { } }', 'holds no code (if)'),
    ('export type X = `a${', 'unterminated template literal'),
    ('export declare const x: number;\n(function(){})()', "not '('"),
    ('type X = string;', 'exports nothing'),
])
def test_a_stub_holds_declarations_only(source, error):
    with pytest.raises(ValueError, match=error.replace('(', r'\(').replace(')', r'\)')):
        CONTRACT.read_stub('m', source)


@pytest.mark.parametrize('path', ['src/x.ts', '../x.d.ts', 'src/../x.d.ts',
                                  'src/x.component-stub.d.ts', 'src/x.component-check.d.ts'])
def test_a_stub_is_a_declaration_file_named_after_a_module_of_the_part(path):
    with pytest.raises(ValueError, match='is not the stub of a module'):
        CONTRACT.module(Path(path))


def write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


TYPESCRIPT_PART = {
    'package.json': json.dumps({'devDependencies': {'typescript': '^5.8.3'}}),
    'package-lock.json': '{}', 'tsconfig.json': '{}', 'src/github.ts': 'export {}',
}


def test_the_parts_of_a_repository_are_the_service_and_the_typescript_projects_beside_it(
        tmp_path):
    write(tmp_path, {
        'requirements.txt': 'fastapi', 'app/main.py': '',
        **{f'frontend/{path}': text for path, text in TYPESCRIPT_PART.items()},
        # A project inside another part, inside the service's sources, a JavaScript
        # project, and the tests and contracts of the repository are not parts.
        **{f'frontend/packages/ui/{path}': text for path, text in TYPESCRIPT_PART.items()},
        **{f'src/web/{path}': text for path, text in TYPESCRIPT_PART.items()},
        'scripts/package.json': json.dumps({'dependencies': {'left-pad': '1'}}),
        'scripts/tsconfig.json': '{}',
        **{f'contract-tests/{path}': text for path, text in TYPESCRIPT_PART.items()},
        **{f'frontend/component-tests/{path}': text for path, text in TYPESCRIPT_PART.items()},
    })
    parts = component_parts(get_language('python'), tmp_path)
    assert [(part.name, part.root, part.language.name) for part in parts] == [
        (SERVICE_PART, '.', 'python'), ('frontend', 'frontend', 'typescript')]
    service, frontend = parts
    assert service.published_dir().as_posix() == 'contracts/components'
    assert frontend.published_dir().as_posix() == 'contracts/parts/frontend/components'
    assert frontend.describe() == {'name': 'frontend', 'root': 'frontend',
                                   'language': 'TypeScript'}


def test_component_tests_and_contracts_of_every_part_are_never_sources():
    python = get_language('python')
    assert is_service_file(python, Path('frontend/src/github.ts'))
    assert not is_service_file(python, Path('frontend/component-tests/tests/a.test.ts'))
    assert not is_service_file(python, Path('component-tests/tests/test_a.py'))
    assert not is_service_file(python, Path('contracts/parts/frontend/components/x.d.ts'))


def test_the_runtime_of_a_typescript_contract_names_an_installable_part(tmp_path):
    write(tmp_path, {f'frontend/{path}': text for path, text in TYPESCRIPT_PART.items()})
    assert CONTRACT.read_runtime({'project': 'frontend'}, tmp_path) == {'project': 'frontend'}
    assert CONTRACT.build_args({'project': 'frontend'}) == {'PROJECT': 'frontend'}
    (tmp_path / 'frontend/package-lock.json').unlink()
    for runtime, error in [({'project': 'frontend'}, 'package-lock.json'),
                           ({'project': '../x'}, 'inside the repository'),
                           ({'project': 'frontend', 'other': 1}, 'exactly project')]:
        with pytest.raises(ValueError, match=error):
            CONTRACT.read_runtime(runtime, tmp_path)


@pytest.fixture(scope='module')
def frontend(tmp_path_factory):
    """A copy of this repository's web client with its dependencies installed. Opt-in:
    it needs Node.js and the npm registry."""
    if os.getenv('RUN_NODE_CONTRACT_TESTS') != '1' or shutil.which('npm') is None:
        pytest.skip('set RUN_NODE_CONTRACT_TESTS=1 with Node.js and npm to run check.mjs')
    project = tmp_path_factory.mktemp('frontend')
    shutil.copytree(FRONTEND, project, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('node_modules', 'dist'))
    subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=project,
                   check=True, capture_output=True)
    return project


def check(project: Path, components: Path, *modules: str) -> subprocess.CompletedProcess:
    return subprocess.run(['node', str(typescript.CHECK_TOOL), str(project), str(components),
                           *modules], capture_output=True, text=True, timeout=120)


def test_check_compares_the_web_clients_stubs_with_its_code(frontend, tmp_path):
    components = tmp_path / 'components'
    write(components, {'src/github.d.ts': GITHUB_STUB, 'src/Flows.d.ts': FLOWS_STUB})
    result = check(frontend, components, 'src/github', 'src/Flows')
    # sessionAttention is a seam the change adds: the module does not export it yet.
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        'error: src/Flows: sessionAttention is not exported by the code']
    flows = frontend / 'src/Flows.tsx'
    source = flows.read_text()
    flows.write_text(source.replace('\nfunction sessionAttention', '\nexport function sessionAttention'))
    try:
        result = check(frontend, components, 'src/github', 'src/Flows')
        assert result.returncode == 0, result.stdout
        write(components, {'src/Flows.d.ts': FLOWS_STUB.replace('body: unknown', 'body: string')
                           .replace('link: string', 'link: number')})
        result = check(frontend, components, 'src/Flows')
        assert result.returncode == 1
        assert "src/Flows: sessionAttention: the stub's type is not assignable to the code's" \
               in result.stdout
        assert "Types of property 'link' are incompatible" in result.stdout
    finally:
        flows.write_text(source)
    # Nothing the check wrote is left in the part.
    assert not list(frontend.glob('src/*.component-*'))


CODE = '''export enum Mode { Open = "open", Closed = "closed" }
export interface Room { id: string; closed: boolean }
export class Rooms {
  static readonly LIMIT = 3;
  private secret = 1;
  constructor(public name: string, limit?: number) { void limit; }
  close(room: Room, merged: boolean): Room { return { ...room, closed: merged || this.secret > 0 }; }
}
'''
STUB = '''export declare enum Mode { Open = "open", Closed = "closed" }
export interface Room { id: string; closed: boolean }
export declare class Rooms {
  static readonly LIMIT: 3;
  constructor(name: string, limit?: number);
  close(room: Room, merged: boolean): Room;
}
'''


@pytest.mark.parametrize('stub, errors', [
    (STUB, []),
    (STUB.replace('"closed" }', '"shut" }'), ['src/rooms: Mode.Closed is "closed" in the code, not "shut"']),
    (STUB.replace('Room;\n', 'Room;\n  reopen(): void;\n'),
     ["src/rooms: Rooms.reopen is not a member of the code's class"]),
    (STUB.replace('closed: boolean }', 'closed: boolean; at: string }'),
     ["src/rooms: Room: the code's type is not assignable to the stub's"]),
    (STUB.replace('limit?: number', 'limit: number'),
     ["src/rooms: Rooms.constructor: the code's type is not assignable to the stub's"]),
    (STUB.replace('Room;\n', 'Room { return room; }\n'),
     ['src/rooms.d.ts:6: An implementation cannot be declared in ambient contexts.']),
])
def test_check_compares_classes_interfaces_and_enums_member_by_member(
        frontend, tmp_path, stub, errors):
    write(frontend, {'src/rooms.ts': CODE})
    components = tmp_path / 'components'
    write(components, {'src/rooms.d.ts': stub})
    try:
        result = check(frontend, components, 'src/rooms')
    finally:
        (frontend / 'src/rooms.ts').unlink()
    assert result.returncode == (1 if errors else 0), result.stdout
    for error in errors:
        assert f'error: {error}' in result.stdout
