#!/usr/bin/env bash
# Trusted host-supplied build of the component tests in the current directory: their own
# dependencies resolved, the tests compiled against the service's classes and
# dependencies, and the console launcher's arguments that run them, written to
# RUNNER/console.args. A test that does not compile fails the build.
# Usage: build-suite.sh RUNNER, where RUNNER holds the launcher and service/ (build.sh).
set -eu
runner="$1"
plugin=org.apache.maven.plugins:maven-dependency-plugin:3.8.1
launcher="$runner/junit-platform-console-standalone.jar"
mvn -B -q "$plugin:build-classpath" -Dmdep.outputFile="$runner/suite.classpath" \
    -DincludeScope=test
# The service's classes and dependencies come first, so the code under test runs with
# the versions it declares: a library the suite also lists is dropped from the suite's
# entries. JUnit itself comes with the launcher.
classpath="$(
    {
        cat "$runner/service/classes.path"; echo
        tr ':' '\n' < "$runner/service/runtime.classpath"; echo
        tr ':' '\n' < "$runner/service/compile.classpath"; echo
        tr ':' '\n' < "$runner/suite.classpath" \
            | grep -v -E '/(org/junit|org/opentest4j|org/apiguardian)/' || true
    } | awk -v repository="$HOME/.m2/repository/" '
        NF == 0 { next }
        {
            key = $0
            # A jar of the local repository (group/artifact/version/artifact-version[-classifier].jar)
            # is the same library whatever its version.
            if (index($0, repository) == 1) {
                n = split(substr($0, length(repository) + 1), parts, "/")
                if (n >= 4) {
                    prefix = parts[n - 2] "-" parts[n - 1]
                    key = substr($0, 1, length($0) - length(parts[n - 1]) - length(parts[n]) - 1) \
                        substr(parts[n], length(prefix) + 1)
                }
            }
            if (!(key in seen)) { seen[key] = 1; print }
        }' | paste -sd: -
)"
rm -rf target/test-classes
mkdir -p target/test-classes
find src/test/java -name '*.java' -type f > "$runner/sources.txt"
if [[ ! -s "$runner/sources.txt" ]]; then
    echo "The suite has no test sources under src/test/java" >&2
    exit 1
fi
javac -proc:none -parameters -encoding UTF-8 -d target/test-classes \
    -cp "$classpath:$launcher" @"$runner/sources.txt"
if [[ -d src/test/resources ]]; then
    cp -R src/test/resources/. target/test-classes/
fi
tests="$(pwd)/target/test-classes"
cat > "$runner/console.args" <<ARGS
execute
--disable-banner
--disable-ansi-colors
--exclude-engine=junit-vintage
--exclude-engine=junit-platform-suite
--details=tree
--fail-if-no-tests
"--class-path=$tests:$classpath"
"--scan-class-path=$tests"
"--reports-dir=$(pwd)/target/surefire-reports"
ARGS
