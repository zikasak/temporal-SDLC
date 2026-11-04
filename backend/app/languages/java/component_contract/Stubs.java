// Trusted tool of Java component contracts. A stub is a Java source whose methods and
// constructors end in `;` instead of a body; this tool reads stubs with javac's own parser.
//
//   java Stubs.java skeletons COMPONENTS OUT
//       Writes every stub under COMPONENTS to OUT as compilable Java: a method or
//       constructor that needs a body throws UnsupportedOperationException, and a field
//       that needs a value gets its type's default. Tests can then be compiled against the
//       stubs without the code they describe.
//   java Stubs.java check COMPONENTS CLASSES DEPENDENCIES MODULE...
//       Compiles the skeletons against the code's dependencies alone (DEPENDENCIES is a
//       file holding their class path), so a stub that names a type of the code it does not
//       stub is an error, then compares each stubbed type MODULE (a fully qualified name)
//       with the compiled code in CLASSES. Prints one line per difference and exits 1 when
//       there is any.
//
// Only the tool's own output is trusted: the stubs and the code are not, and nothing of
// either is run. Types are compared without their type annotations, annotations only when
// they are kept in class files, and parameter names never.

import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.PrimitiveTypeTree;
import com.sun.source.tree.Tree;
import com.sun.source.tree.VariableTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;

import java.io.IOException;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.lang.annotation.RetentionPolicy;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collection;
import java.util.EnumSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import javax.lang.model.element.AnnotationMirror;
import javax.lang.model.element.Element;
import javax.lang.model.element.ElementKind;
import javax.lang.model.element.ExecutableElement;
import javax.lang.model.element.Modifier;
import javax.lang.model.element.RecordComponentElement;
import javax.lang.model.element.TypeElement;
import javax.lang.model.element.TypeParameterElement;
import javax.lang.model.element.VariableElement;
import javax.lang.model.type.ArrayType;
import javax.lang.model.type.DeclaredType;
import javax.lang.model.type.ExecutableType;
import javax.lang.model.type.IntersectionType;
import javax.lang.model.type.TypeKind;
import javax.lang.model.type.TypeMirror;
import javax.lang.model.type.TypeVariable;
import javax.lang.model.type.UnionType;
import javax.lang.model.type.WildcardType;
import javax.lang.model.util.Elements;
import javax.lang.model.util.Types;
import javax.tools.Diagnostic;
import javax.tools.DiagnosticCollector;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.SimpleJavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public final class Stubs {
    private static final String THROW = "{ throw new UnsupportedOperationException(); }";
    private static final Set<Modifier> TYPE_MODIFIERS = EnumSet.of(
            Modifier.PUBLIC, Modifier.PROTECTED, Modifier.PRIVATE, Modifier.STATIC,
            Modifier.ABSTRACT, Modifier.FINAL, Modifier.SEALED, Modifier.NON_SEALED);
    private static final Set<Modifier> METHOD_MODIFIERS = EnumSet.of(
            Modifier.PUBLIC, Modifier.PROTECTED, Modifier.PRIVATE, Modifier.STATIC,
            Modifier.ABSTRACT, Modifier.DEFAULT, Modifier.FINAL);
    private static final Set<Modifier> FIELD_MODIFIERS = EnumSet.of(
            Modifier.PUBLIC, Modifier.PROTECTED, Modifier.PRIVATE, Modifier.STATIC,
            Modifier.FINAL);

    private static final JavaCompiler COMPILER = ToolProvider.getSystemJavaCompiler();

    private Stubs() {
    }

    public static void main(String[] args) throws IOException {
        if (args.length == 3 && args[0].equals("skeletons")) {
            for (Map.Entry<String, String> stub : skeletons(Path.of(args[1])).entrySet()) {
                Path target = Path.of(args[2]).resolve(stub.getKey());
                Files.createDirectories(target.getParent());
                Files.writeString(target, stub.getValue());
            }
            return;
        }
        if (args.length >= 4 && args[0].equals("check")) {
            List<String> errors = check(Path.of(args[1]), Path.of(args[2]),
                    Files.readString(Path.of(args[3])).strip(),
                    List.of(args).subList(4, args.length));
            errors.forEach(System.out::println);
            System.exit(errors.isEmpty() ? 0 : 1);
        }
        System.err.println("Usage: Stubs skeletons COMPONENTS OUT | "
                + "Stubs check COMPONENTS CLASSES DEPENDENCIES MODULE...");
        System.exit(2);
    }

    /** A stub's source by its path relative to the contract. */
    private static Map<String, String> stubs(Path components) throws IOException {
        Map<String, String> stubs = new TreeMap<>();
        try (Stream<Path> files = Files.walk(components)) {
            for (Path file : files.filter(path -> path.toString().endsWith(".java")
                    && Files.isRegularFile(path)).toList()) {
                stubs.put(components.relativize(file).toString(), Files.readString(file));
            }
        }
        return stubs;
    }

    private static JavaFileObject source(String path, String text) {
        return new SimpleJavaFileObject(URI.create("string:///" + path), JavaFileObject.Kind.SOURCE) {
            @Override
            public CharSequence getCharContent(boolean ignoreEncodingErrors) {
                return text;
            }
        };
    }

    private static String describe(Diagnostic<? extends JavaFileObject> diagnostic) {
        String where = diagnostic.getSource() == null ? ""
                : diagnostic.getSource().getName().replaceFirst("^/", "") + ":"
                + diagnostic.getLineNumber() + ": ";
        return "error: " + where + diagnostic.getMessage(Locale.ROOT).lines().findFirst().orElse("");
    }

    private static JavacTask task(String classPath, List<JavaFileObject> sources,
                                  DiagnosticCollector<JavaFileObject> diagnostics) {
        StandardJavaFileManager files = COMPILER.getStandardFileManager(
                diagnostics, Locale.ROOT, StandardCharsets.UTF_8);
        List<String> options = new ArrayList<>(List.of("-proc:none", "-implicit:none", "-Xlint:none"));
        options.add("-classpath");
        options.add(classPath.isEmpty() ? Path.of("/nonexistent").toString() : classPath);
        return (JavacTask) COMPILER.getTask(new PrintWriter(new StringWriter()), files, diagnostics,
                options, null, sources);
    }

    // ---- skeletons ----

    static Map<String, String> skeletons(Path components) throws IOException {
        Map<String, String> stubs = stubs(components);
        Map<String, String> skeletons = new TreeMap<>();
        List<String> errors = new ArrayList<>();
        for (Map.Entry<String, String> stub : stubs.entrySet()) {
            try {
                skeletons.put(stub.getKey(), skeleton(stub.getKey(), stub.getValue()));
            } catch (IllegalArgumentException exc) {
                errors.add(exc.getMessage());
            }
        }
        if (!errors.isEmpty()) {
            errors.forEach(System.out::println);
            System.exit(1);
        }
        return skeletons;
    }

    private record Edit(long position, long length, String text) {
    }

    /** The stub as compilable Java: nothing it declares changes, only bodies are added. */
    static String skeleton(String path, String text) throws IOException {
        DiagnosticCollector<JavaFileObject> diagnostics = new DiagnosticCollector<>();
        JavacTask task = task("", List.of(source(path, text)), diagnostics);
        List<CompilationUnitTree> units = new ArrayList<>();
        task.parse().forEach(units::add);
        List<String> errors = diagnostics.getDiagnostics().stream()
                .filter(item -> item.getKind() == Diagnostic.Kind.ERROR).map(Stubs::describe).toList();
        if (!errors.isEmpty()) {
            throw new IllegalArgumentException(String.join("\n", errors));
        }
        CompilationUnitTree unit = units.get(0);
        SourcePositions positions = Trees.instance(task).getSourcePositions();
        List<Edit> edits = new ArrayList<>();
        new TreePathScanner<Void, Void>() {
            @Override
            public Void visitMethod(MethodTree method, Void unused) {
                Tree owner = getCurrentPath().getParentPath().getLeaf();
                Set<Modifier> flags = method.getModifiers().getFlags();
                boolean needsBody = switch (owner.getKind()) {
                    case ANNOTATION_TYPE -> false;
                    case INTERFACE -> flags.contains(Modifier.DEFAULT) || flags.contains(Modifier.STATIC)
                            || flags.contains(Modifier.PRIVATE);
                    default -> !flags.contains(Modifier.ABSTRACT) && !flags.contains(Modifier.NATIVE);
                };
                long end = positions.getEndPosition(unit, method);
                if (method.getBody() == null && needsBody && end > 0 && text.charAt((int) end - 1) == ';') {
                    edits.add(new Edit(end - 1, 1, " " + THROW));
                }
                // A body the stub has anyway is left for javac and the host to reject.
                return null;
            }

            @Override
            public Void visitVariable(VariableTree variable, Void unused) {
                Tree owner = getCurrentPath().getParentPath().getLeaf();
                if (!(owner instanceof ClassTree type) || variable.getInitializer() != null) {
                    return null;
                }
                Set<Modifier> flags = variable.getModifiers().getFlags();
                boolean constant = type.getKind() == Tree.Kind.INTERFACE
                        || type.getKind() == Tree.Kind.ANNOTATION_TYPE;
                // A record's only instance fields are its components, in its header.
                boolean component = type.getKind() == Tree.Kind.RECORD && !flags.contains(Modifier.STATIC);
                long end = positions.getEndPosition(unit, variable);
                if ((constant || flags.contains(Modifier.FINAL)) && !component && end > 0) {
                    // The declaration's end may take in the `;` or `,` after its name.
                    long at = ";,".indexOf(text.charAt((int) end - 1)) >= 0 ? end - 1 : end;
                    edits.add(new Edit(at, 0, " = " + defaultValue(variable.getType())));
                }
                return null;
            }
        }.scan(unit, null);
        StringBuilder skeleton = new StringBuilder(text);
        edits.stream().sorted((a, b) -> Long.compare(b.position(), a.position())).forEach(edit ->
                skeleton.replace((int) edit.position(), (int) (edit.position() + edit.length()), edit.text()));
        return skeleton.toString();
    }

    private static String defaultValue(Tree type) {
        if (!(type instanceof PrimitiveTypeTree primitive)) {
            return "null";
        }
        return switch (primitive.getPrimitiveTypeKind()) {
            case BOOLEAN -> "false";
            case CHAR -> "'\\0'";
            case BYTE -> "(byte) 0";
            case SHORT -> "(short) 0";
            case LONG -> "0L";
            case FLOAT -> "0F";
            case DOUBLE -> "0D";
            default -> "0";
        };
    }

    // ---- check ----

    static List<String> check(Path components, Path classes, String dependencies,
                              List<String> modules) throws IOException {
        Map<String, String> skeletons = skeletons(components);
        List<JavaFileObject> sources = new ArrayList<>();
        skeletons.forEach((path, text) -> sources.add(source(path, text)));
        DiagnosticCollector<JavaFileObject> diagnostics = new DiagnosticCollector<>();
        // Against the dependencies alone: the stubs have to be complete without the code.
        JavacTask stubTask = task(dependencies, sources, diagnostics);
        stubTask.analyze();
        List<String> errors = new ArrayList<>(diagnostics.getDiagnostics().stream()
                .filter(item -> item.getKind() == Diagnostic.Kind.ERROR).map(Stubs::describe)
                .collect(Collectors.toCollection(LinkedHashSet::new)));
        if (!errors.isEmpty()) {
            errors.add(0, "error: the stubs do not compile against the code's dependencies; "
                    + "stub every type of the code they name");
            return errors;
        }
        String classPath = dependencies.isEmpty() ? classes.toString()
                : classes + java.io.File.pathSeparator + dependencies;
        JavacTask codeTask = task(classPath, List.of(), new DiagnosticCollector<>());
        Comparison comparison = new Comparison(stubTask.getElements(), stubTask.getTypes(),
                codeTask.getElements(), codeTask.getTypes());
        for (String module : modules) {
            TypeElement stub = comparison.stubs.getTypeElement(module);
            if (stub == null || !(stub.getEnclosingElement() instanceof javax.lang.model.element.PackageElement)) {
                comparison.errors.add("error: no stub declares " + module
                        + " as the top-level type of its file");
                continue;
            }
            TypeElement code = comparison.codeElements.getTypeElement(module);
            if (code == null) {
                comparison.errors.add("error: " + module + " is not present at runtime");
                continue;
            }
            comparison.compare(module, stub, code);
        }
        return comparison.errors;
    }

    /** Compares the stubs' declarations with the code's, element by element. */
    private static final class Comparison {
        final Elements stubs;
        final Types stubTypes;
        final Elements codeElements;
        final Types codeTypes;
        final List<String> errors = new ArrayList<>();

        Comparison(Elements stubs, Types stubTypes, Elements code, Types codeTypes) {
            this.stubs = stubs;
            this.stubTypes = stubTypes;
            this.codeElements = code;
            this.codeTypes = codeTypes;
        }

        private void differ(String name, String what, Object stub, Object code) {
            errors.add("error: " + name + " is inconsistent: " + what + " differs (stub: "
                    + stub + "; code: " + code + ")");
        }

        private void same(String name, String what, Object stub, Object code) {
            if (!stub.equals(code)) {
                differ(name, what, stub, code);
            }
        }

        void compare(String name, TypeElement stub, TypeElement code) {
            same(name, "kind", stub.getKind(), code.getKind());
            same(name, "modifiers", modifiers(stub, TYPE_MODIFIERS), modifiers(code, TYPE_MODIFIERS));
            same(name, "type parameters", typeParameters(stub.getTypeParameters()),
                    typeParameters(code.getTypeParameters()));
            String superclass = type(stub.getSuperclass());
            if (stub.getKind() == ElementKind.CLASS && !superclass.equals("java.lang.Object")) {
                same(name, "superclass", superclass, type(code.getSuperclass()));
            }
            Set<String> supertypes = supertypes(code.asType(), new LinkedHashSet<>());
            for (TypeMirror item : stub.getInterfaces()) {
                if (!supertypes.contains(type(item))) {
                    errors.add("error: " + name + " is inconsistent: the code does not implement "
                            + type(item));
                }
            }
            annotations(name, stub, code);
            if (stub.getKind() == ElementKind.RECORD) {
                same(name, "record components", components(stub.getRecordComponents()),
                        components(code.getRecordComponents()));
            }
            for (Element member : stub.getEnclosedElements()) {
                if (stubs.getOrigin(member) != Elements.Origin.EXPLICIT || generated(stub, member)) {
                    continue;
                }
                switch (member.getKind()) {
                    case METHOD, CONSTRUCTOR -> executable(name, (ExecutableElement) member, code);
                    case FIELD -> field(name, (VariableElement) member, code);
                    case ENUM_CONSTANT -> constant(name, member, code);
                    case CLASS, INTERFACE, ENUM, RECORD, ANNOTATION_TYPE -> nested(name, (TypeElement) member, code);
                    default -> {
                        // Record components are compared with their record's header.
                    }
                }
            }
        }

        /** Names of two compilations, which never equal each other as Name objects. */
        private static boolean named(Element one, Element other) {
            return one.getSimpleName().toString().equals(other.getSimpleName().toString());
        }

        /** A member every record has, declared in its stub or not: its components' fields
         * and accessors, equals, hashCode and toString. */
        private static boolean generated(TypeElement owner, Element member) {
            if (owner.getKind() != ElementKind.RECORD) {
                return false;
            }
            String name = member.getSimpleName().toString();
            if (member.getKind() == ElementKind.FIELD) {
                return !member.getModifiers().contains(Modifier.STATIC);
            }
            if (member.getKind() != ElementKind.METHOD) {
                return false;
            }
            List<? extends VariableElement> parameters = ((ExecutableElement) member).getParameters();
            boolean accessor = parameters.isEmpty() && owner.getRecordComponents().stream()
                    .anyMatch(component -> component.getSimpleName().toString().equals(name));
            return accessor || parameters.isEmpty() && (name.equals("hashCode") || name.equals("toString"))
                    || name.equals("equals") && parameters.size() == 1
                    && type(parameters.get(0).asType()).equals("java.lang.Object");
        }

        private void nested(String owner, TypeElement stub, TypeElement code) {
            String name = owner + "." + stub.getSimpleName();
            for (Element member : code.getEnclosedElements()) {
                if (member instanceof TypeElement type && named(type, stub)) {
                    compare(name, stub, type);
                    return;
                }
            }
            errors.add("error: " + name + " is not present at runtime");
        }

        private void constant(String owner, Element stub, TypeElement code) {
            boolean present = code.getEnclosedElements().stream().anyMatch(member ->
                    member.getKind() == ElementKind.ENUM_CONSTANT && named(member, stub));
            if (!present) {
                errors.add("error: " + owner + "." + stub.getSimpleName() + " is not present at runtime");
            }
        }

        private void field(String owner, VariableElement stub, TypeElement code) {
            String name = owner + "." + stub.getSimpleName();
            for (Element member : code.getEnclosedElements()) {
                if (member.getKind() == ElementKind.FIELD && named(member, stub)) {
                    same(name, "type", type(stub.asType()), type(member.asType()));
                    same(name, "modifiers", modifiers(stub, FIELD_MODIFIERS), modifiers(member, FIELD_MODIFIERS));
                    annotations(name, stub, member);
                    return;
                }
            }
            errors.add("error: " + name + " is not present at runtime");
        }

        private void executable(String owner, ExecutableElement stub, TypeElement code) {
            boolean constructor = stub.getKind() == ElementKind.CONSTRUCTOR;
            // As members of the stubbed type, so an inherited one has its type arguments.
            ExecutableType stubType = (ExecutableType) stubTypes.asMemberOf(
                    (DeclaredType) stub.getEnclosingElement().asType(), stub);
            List<String> erased = stubType.getParameterTypes().stream()
                    .map(item -> type(stubTypes.erasure(item))).toList();
            String name = owner + "." + (constructor ? stub.getEnclosingElement().getSimpleName()
                    : stub.getSimpleName()) + "(" + String.join(", ", erased) + ")";
            // A method may be inherited; a constructor never is.
            Collection<? extends Element> members = constructor ? code.getEnclosedElements()
                    : codeElements.getAllMembers(code);
            for (Element member : members) {
                if (member.getKind() != stub.getKind() || !named(member, stub)) {
                    continue;
                }
                ExecutableElement candidate = (ExecutableElement) member;
                ExecutableType candidateType = (ExecutableType) codeTypes.asMemberOf(
                        (DeclaredType) code.asType(), candidate);
                if (!candidateType.getParameterTypes().stream()
                        .map(item -> type(codeTypes.erasure(item))).toList().equals(erased)) {
                    continue;
                }
                same(name, "modifiers", modifiers(stub, METHOD_MODIFIERS), modifiers(candidate, METHOD_MODIFIERS));
                same(name, "type parameters", typeParameters(stub.getTypeParameters()),
                        typeParameters(candidate.getTypeParameters()));
                same(name, "parameter types", types(stubType.getParameterTypes()),
                        types(candidateType.getParameterTypes()));
                same(name, "varargs", stub.isVarArgs(), candidate.isVarArgs());
                same(name, "return type", type(stubType.getReturnType()), type(candidateType.getReturnType()));
                same(name, "thrown types", Set.copyOf(types(stubType.getThrownTypes())),
                        Set.copyOf(types(candidateType.getThrownTypes())));
                annotations(name, stub, candidate);
                return;
            }
            errors.add("error: " + name + " is not present at runtime");
        }

        private void annotations(String name, Element stub, Element code) {
            Set<String> present = code.getAnnotationMirrors().stream().map(Object::toString)
                    .collect(Collectors.toSet());
            for (AnnotationMirror annotation : stub.getAnnotationMirrors()) {
                if (retention(annotation) == RetentionPolicy.SOURCE) {
                    // Gone from the class file: nothing can tell whether the code has it.
                    continue;
                }
                if (!present.contains(annotation.toString())) {
                    errors.add("error: " + name + " is inconsistent: the code is not annotated "
                            + annotation);
                }
            }
        }

        private RetentionPolicy retention(AnnotationMirror annotation) {
            java.lang.annotation.Retention retention = annotation.getAnnotationType().asElement()
                    .getAnnotation(java.lang.annotation.Retention.class);
            return retention == null ? RetentionPolicy.CLASS : retention.value();
        }

        private Set<String> supertypes(TypeMirror type, Set<String> found) {
            for (TypeMirror item : codeTypes.directSupertypes(type)) {
                if (found.add(type(item))) {
                    supertypes(item, found);
                }
            }
            return found;
        }

        private static Set<Modifier> modifiers(Element element, Set<Modifier> compared) {
            Set<Modifier> modifiers = EnumSet.noneOf(Modifier.class);
            element.getModifiers().stream().filter(compared::contains).forEach(modifiers::add);
            return modifiers;
        }

        private static List<String> components(List<? extends RecordComponentElement> components) {
            return components.stream().map(item -> type(item.asType()) + " " + item.getSimpleName()).toList();
        }

        private static List<String> typeParameters(List<? extends TypeParameterElement> parameters) {
            return parameters.stream().map(item -> item.getSimpleName() + " extends "
                    + types(item.getBounds())).toList();
        }

        private static List<String> types(List<? extends TypeMirror> types) {
            return types.stream().map(Stubs::type).toList();
        }
    }

    /** A type as Java names it, fully qualified and without type annotations. */
    static String type(TypeMirror type) {
        return switch (type.getKind()) {
            case DECLARED -> {
                DeclaredType declared = (DeclaredType) type;
                String name = ((TypeElement) declared.asElement()).getQualifiedName().toString();
                yield declared.getTypeArguments().isEmpty() ? name
                        : name + "<" + declared.getTypeArguments().stream().map(Stubs::type)
                        .collect(Collectors.joining(", ")) + ">";
            }
            case ARRAY -> type(((ArrayType) type).getComponentType()) + "[]";
            case TYPEVAR -> ((TypeVariable) type).asElement().getSimpleName().toString();
            case WILDCARD -> {
                WildcardType wildcard = (WildcardType) type;
                yield wildcard.getExtendsBound() != null ? "? extends " + type(wildcard.getExtendsBound())
                        : wildcard.getSuperBound() != null ? "? super " + type(wildcard.getSuperBound()) : "?";
            }
            case INTERSECTION -> ((IntersectionType) type).getBounds().stream().map(Stubs::type)
                    .collect(Collectors.joining(" & "));
            case UNION -> ((UnionType) type).getAlternatives().stream().map(Stubs::type)
                    .collect(Collectors.joining(" | "));
            case ERROR -> "<unresolved " + type + ">";
            default -> type.getKind() == TypeKind.NONE ? "none" : type.getKind().name().toLowerCase(Locale.ROOT);
        };
    }
}
