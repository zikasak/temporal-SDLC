"""The component suite of a delivery whose criteria are routed to seams of more than one
part of its repository, or of a part other than the service: one project per part, the
service's at the root and part P's under `parts/P/`, that every stage after the routing
treats as one suite (plans/part-component-contracts.md, section 3.5).

A test of part P is identified as `P:` and its identity in P's own suite, and the runner
starts the class names of P's report with the same prefix, so a report entry is told to
belong to P by its class name alone."""
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.languages import ComponentSuite

# Where the suite of a part other than the service lives within the composite suite.
PARTS_DIR = 'parts'
# What separates a part's name from the identity of a test in its suite.
PART_SEPARATOR = ':'


@dataclass(frozen=True)
class SuiteMember:
    # The part's name: `service`, or its directory in the repository.
    name: str
    # The member's suite.
    suite: ComponentSuite
    # The service's suite is at the root of the composite suite.
    service: bool = False
    # The label of the part's language, as its author is told it.
    label: str = ''

    @property
    def directory(self) -> PurePosixPath:
        """Where the member's suite is within the composite suite."""
        return PurePosixPath('.') if self.service else PurePosixPath(PARTS_DIR, self.name)

    @property
    def prefix(self) -> str:
        """What starts the identity of each of the member's tests."""
        return '' if self.service else f'{self.name}{PART_SEPARATOR}'


def _within(member: SuiteMember, path: str) -> str:
    return path if member.service else (member.directory / path).as_posix()


@dataclass(frozen=True)
class CompositeSuite(ComponentSuite):
    """The members' suites as one. Its `template` and `runner` are not used: each member
    has its own template, and the composite runner is put together from the members'
    runners (app/test_generation/components.py)."""
    members: tuple[SuiteMember, ...] = field(default=())

    def part_of(self, class_name: str) -> str:
        """The member a report entry of `class_name` belongs to, by its name."""
        for member in sorted((member for member in self.members if not member.service),
                             key=lambda member: len(member.prefix), reverse=True):
            if class_name.startswith(member.prefix):
                return member.name
        service = next((member for member in self.members if member.service), None)
        return service.name if service else self.members[0].name

    def has_tests(self, project: Path) -> bool:
        # Every part of the suite proves criteria of its own.
        return all(member.suite.has_tests(project / member.directory)
                   for member in self.members)


INSTRUCTIONS = """The suite has one project for each part of the repository whose criteria
it proves: {layout}. Each `routes` entry's seam names its part (`part`, absent for the
service), and its tests go in that part's project, written as the part's own instructions
below say, from that part's stubs alone. Paths in those instructions are within the part's
project, and the stubs of part P are in /workspace/contracts/parts/P/components (the
service's in /workspace/contracts/components). Never put a test of one part in another's
project, or import one part's code from another's tests."""


def composite_suite(members: list[SuiteMember]) -> CompositeSuite:
    """The suite of `members`, the service's first when it is one of them."""
    if not members:
        raise ValueError('A composite suite needs a member')
    ordered = tuple(sorted(members, key=lambda member: not member.service))

    def describe(member: SuiteMember) -> str:
        where = ('the root of /workspace/tests' if member.service
                 else f'/workspace/tests/{member.directory.as_posix()}')
        return f'the {"service" if member.service else "part " + member.name} ' \
               f'({member.label or "its language"}) at {where}'

    instructions = '\n\n'.join([
        INSTRUCTIONS.format(layout='; '.join(describe(member) for member in ordered)),
        *(f'Instructions for {"the service" if member.service else "part " + member.name}'
          f'{"" if member.service else " (in its project, " + member.directory.as_posix() + ")"}'
          f':\n{member.suite.instructions}' for member in ordered)])

    def test_cases(project: Path) -> dict[str, str]:
        cases: dict[str, str] = {}
        for member in ordered:
            for case, source in member.suite.test_cases(project / member.directory).items():
                cases[member.prefix + case] = source
        return cases

    def untracked_tests(project: Path) -> list[str]:
        return [f'{"" if member.service else member.name + ": "}{item}'
                for member in ordered
                for item in member.suite.untracked_tests(project / member.directory)]

    def case_id(class_name: str, name: str) -> str:
        for member in sorted((member for member in ordered if not member.service),
                             key=lambda member: len(member.prefix), reverse=True):
            if class_name.startswith(member.prefix):
                return member.prefix + member.suite.case_id(
                    class_name[len(member.prefix):], name)
        service = next((member for member in ordered if member.service), None)
        return service.suite.case_id(class_name, name) if service else f'{class_name}#{name}'

    tools: dict[str, Path] = {}
    for member in ordered:
        for tool in member.suite.tools:
            tools.setdefault(tool.name, tool)
    first = ordered[0]
    return CompositeSuite(
        template=first.suite.template,
        instructions=instructions,
        published=tuple(_within(member, item) for member in ordered
                        for item in member.suite.published),
        test_glob=_within(first, first.suite.test_glob),
        test_cases=test_cases, case_id=case_id, untracked_tests=untracked_tests,
        required=tuple(_within(member, item) for member in ordered
                       for item in member.suite.required),
        runner=(),
        tools=tuple(tools.values()),
        members=ordered,
    )


def suite_members(suite: ComponentSuite) -> tuple[SuiteMember, ...]:
    """The members of a composite suite, or the service's suite as its only member."""
    if isinstance(suite, CompositeSuite):
        return suite.members
    # Named as app.routing.parts names the service.
    return (SuiteMember('service', suite, service=True),)
