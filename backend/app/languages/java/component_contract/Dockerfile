# Trusted runner of the component contract's conformance check: the repository's Maven
# project compiled with its dependencies, and the stubs, compiled against those
# dependencies alone and compared by Stubs.java with what javac reads of the compiled
# code. The check runs without a network; only this build downloads anything.
FROM maven:3.9.9-eclipse-temurin-21
RUN useradd --create-home --uid 10001 checker && mkdir /runner && chown checker:checker /runner
USER checker
COPY --chown=checker:checker Stubs.java build.sh /runner/
RUN javac -d /runner/tool /runner/Stubs.java
COPY --chown=checker:checker repository /repository
ARG PROJECT=.
RUN bash /runner/build.sh "/repository/$PROJECT" /runner/service
COPY components /components
COPY check.sh /runner/
WORKDIR /home/checker
ENTRYPOINT ["bash", "/runner/check.sh"]
