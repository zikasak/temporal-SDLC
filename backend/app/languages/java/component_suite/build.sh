#!/usr/bin/env bash
# Trusted host-supplied build of the code a component contract describes: its Maven
# project compiled, without its tests, and the class paths of its dependencies.
# Usage: build.sh PROJECT OUT. Writes to OUT: classes.path (the directory of the compiled
# classes), compile.classpath (compile and provided dependencies) and runtime.classpath
# (compile and runtime dependencies).
set -eu
project="$1"
out="$2"
plugin=org.apache.maven.plugins:maven-dependency-plugin:3.8.1
mkdir -p "$out"
cd "$project"
mvn -B -q -Dmaven.test.skip=true compile \
    "$plugin:build-classpath" -Dmdep.outputFile="$out/compile.classpath" -DincludeScope=compile
mvn -B -q -o "$plugin:build-classpath" \
    -Dmdep.outputFile="$out/runtime.classpath" -DincludeScope=runtime
mvn -B -q org.apache.maven.plugins:maven-help-plugin:3.5.1:evaluate \
    -Dexpression=project.build.outputDirectory -DforceStdout > "$out/classes.path"
if [[ ! -d "$(cat "$out/classes.path")" ]]; then
    echo "The project compiled no classes: $(cat "$out/classes.path")" >&2
    exit 1
fi
