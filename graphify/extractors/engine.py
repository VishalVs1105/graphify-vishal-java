"""Java tree-sitter declarations, HTTP contracts and call-site evidence."""

from __future__ import annotations
import importlib
import json
import re
from graphify.extractors.base import _file_stem, _make_id, _read_text
from graphify.ids import normalize_id
from graphify.extractors.models import LanguageConfig
from graphify.security import sanitize_ast_metadata as sanitize_metadata
from pathlib import Path

_JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "record_declaration",
        "method_declaration",
        "constructor_declaration",
    }
)


def _java_type_parameters_in_scope(node, source: bytes) -> frozenset[str]:
    """Return Java type-parameter names visible from ``node``."""
    names: set[str] = set()
    scope = node
    while scope is not None:
        if scope.type in _JAVA_TYPE_PARAMETER_SCOPE_DECLARATIONS:
            params = scope.child_by_field_name("type_parameters")
            if params is not None:
                for param in params.children:
                    if param.type != "type_parameter":
                        continue
                    name_node = next(
                        (child for child in param.children if child.type == "type_identifier"), None
                    )
                    if name_node is not None:
                        names.add(_read_text(name_node, source))
        scope = scope.parent
    return frozenset(names)


_JAVA_BUILTIN_TYPES = frozenset(
    {
        "Object",
        "String",
        "CharSequence",
        "StringBuilder",
        "StringBuffer",
        "Number",
        "Byte",
        "Short",
        "Integer",
        "Long",
        "Float",
        "Double",
        "Boolean",
        "Character",
        "Void",
        "Class",
        "Enum",
        "Record",
        "Math",
        "System",
        "Thread",
        "Runnable",
        "Comparable",
        "Iterable",
        "Cloneable",
        "AutoCloseable",
        "Appendable",
        "Readable",
        "Process",
        "ProcessBuilder",
        "Runtime",
        "Package",
        "ThreadLocal",
        "InheritableThreadLocal",
        "Throwable",
        "Exception",
        "RuntimeException",
        "Error",
        "IllegalArgumentException",
        "IllegalStateException",
        "NullPointerException",
        "IndexOutOfBoundsException",
        "ArrayIndexOutOfBoundsException",
        "ClassCastException",
        "NumberFormatException",
        "ArithmeticException",
        "UnsupportedOperationException",
        "InterruptedException",
        "CloneNotSupportedException",
        "SecurityException",
        "StackOverflowError",
        "OutOfMemoryError",
        "AssertionError",
        "Collection",
        "List",
        "ArrayList",
        "LinkedList",
        "Vector",
        "Stack",
        "Set",
        "HashSet",
        "LinkedHashSet",
        "TreeSet",
        "SortedSet",
        "NavigableSet",
        "EnumSet",
        "Map",
        "HashMap",
        "LinkedHashMap",
        "TreeMap",
        "SortedMap",
        "NavigableMap",
        "Hashtable",
        "EnumMap",
        "Properties",
        "Queue",
        "Deque",
        "ArrayDeque",
        "PriorityQueue",
        "Iterator",
        "ListIterator",
        "Comparator",
        "Optional",
        "OptionalInt",
        "OptionalLong",
        "OptionalDouble",
        "Collections",
        "Arrays",
        "Objects",
        "Date",
        "Calendar",
        "Random",
        "UUID",
        "Scanner",
        "StringJoiner",
        "StringTokenizer",
        "BitSet",
        "Spliterator",
        "Locale",
        "NoSuchElementException",
        "ConcurrentModificationException",
        "Stream",
        "IntStream",
        "LongStream",
        "DoubleStream",
        "Collector",
        "Collectors",
        "Function",
        "BiFunction",
        "Consumer",
        "BiConsumer",
        "Supplier",
        "Predicate",
        "BiPredicate",
        "UnaryOperator",
        "BinaryOperator",
        "IntFunction",
        "ToIntFunction",
        "ToLongFunction",
        "ToDoubleFunction",
        "Callable",
        "Future",
        "CompletableFuture",
        "CompletionStage",
        "Executor",
        "ExecutorService",
        "Executors",
        "ScheduledExecutorService",
        "TimeUnit",
        "ConcurrentHashMap",
        "ConcurrentMap",
        "CopyOnWriteArrayList",
        "BlockingQueue",
        "CountDownLatch",
        "Semaphore",
        "CyclicBarrier",
        "AtomicInteger",
        "AtomicLong",
        "AtomicBoolean",
        "AtomicReference",
        "Instant",
        "Duration",
        "Period",
        "LocalDate",
        "LocalTime",
        "LocalDateTime",
        "ZonedDateTime",
        "OffsetDateTime",
        "ZoneId",
        "ZoneOffset",
        "DayOfWeek",
        "Month",
        "Year",
        "Clock",
        "DateTimeFormatter",
        "IOException",
        "UncheckedIOException",
        "FileNotFoundException",
        "File",
        "InputStream",
        "OutputStream",
        "Reader",
        "Writer",
        "BufferedReader",
        "BufferedWriter",
        "InputStreamReader",
        "OutputStreamWriter",
        "FileReader",
        "FileWriter",
        "PrintStream",
        "PrintWriter",
        "ByteArrayInputStream",
        "ByteArrayOutputStream",
        "Serializable",
        "Closeable",
        "Path",
        "Paths",
        "Files",
        "BigDecimal",
        "BigInteger",
    }
)


def _java_collect_type_refs(
    node,
    source: bytes,
    generic: bool,
    out: list[tuple[str, str]],
    skip: frozenset[str] | None = None,
) -> None:
    """Walk a Java type expression; append (name, role) tuples."""
    if node is None:
        return
    if skip is None:
        skip = _java_type_parameters_in_scope(node, source)
    t = node.type
    if t in ("integral_type", "floating_point_type", "boolean_type", "void_type"):
        return
    if t == "type_identifier":
        name = _read_text(node, source)
        if name and name not in skip and (name not in _JAVA_BUILTIN_TYPES):
            out.append((name, "generic_arg" if generic else "type"))
        return
    if t == "scoped_type_identifier":
        text = _read_text(node, source).rsplit(".", 1)[-1]
        if text and text not in _JAVA_BUILTIN_TYPES:
            out.append((text, "generic_arg" if generic else "type"))
        return
    if t == "generic_type":
        for c in node.children:
            if c.type in ("type_identifier", "scoped_type_identifier"):
                text = _read_text(c, source).rsplit(".", 1)[-1]
                if (
                    text
                    and text not in _JAVA_BUILTIN_TYPES
                    and (c.type == "scoped_type_identifier" or text not in skip)
                ):
                    out.append((text, "generic_arg" if generic else "type"))
                break
        for c in node.children:
            if c.type == "type_arguments":
                for arg in c.children:
                    if arg.is_named:
                        _java_collect_type_refs(arg, source, True, out, skip)
        return
    if t == "array_type":
        for c in node.children:
            if c.is_named:
                _java_collect_type_refs(c, source, generic, out, skip)
        return
    if node.is_named:
        for c in node.children:
            if c.is_named:
                _java_collect_type_refs(c, source, generic, out, skip)


def _java_receiver_type_name(type_node, source: bytes) -> str | None:
    """Return the concrete declared type usable for Java receiver resolution."""
    if type_node is None:
        return None
    t = type_node.type
    if t == "type_identifier":
        name = _read_text(type_node, source)
    elif t == "scoped_type_identifier":
        name = _read_text(type_node, source).rsplit(".", 1)[-1]
    elif t == "generic_type":
        base = next(
            (
                child
                for child in type_node.children
                if child.type in ("type_identifier", "scoped_type_identifier")
            ),
            None,
        )
        return _java_receiver_type_name(base, source)
    else:
        return None
    if (
        not name
        or name in _JAVA_BUILTIN_TYPES
        or name in _java_type_parameters_in_scope(type_node, source)
    ):
        return None
    return name


def _java_declarator_names(declaration_node, source: bytes) -> list[str]:
    names: list[str] = []
    for child in declaration_node.children:
        if child.type != "variable_declarator":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is not None:
            name = _read_text(name_node, source)
            if name:
                names.append(name)
    return names


def _java_lambda_parameters(lambda_node, source: bytes) -> list[tuple[str, str | None]]:
    parameters = lambda_node.child_by_field_name("parameters")
    if parameters is None:
        return []
    if parameters.type == "identifier":
        return [(_read_text(parameters, source), None)]
    if parameters.type == "inferred_parameters":
        return [
            (_read_text(child, source), None)
            for child in parameters.children
            if child.type == "identifier"
        ]
    bindings: list[tuple[str, str | None]] = []
    for parameter in parameters.children:
        if parameter.type not in ("formal_parameter", "spread_parameter"):
            continue
        name_node = parameter.child_by_field_name("name")
        if name_node is not None:
            bindings.append(
                (
                    _read_text(name_node, source),
                    _java_receiver_type_name(parameter.child_by_field_name("type"), source),
                )
            )
    return bindings


def _java_method_receiver_types(
    method_node, source: bytes, field_types: dict[str, str]
) -> dict[str, str]:
    """Build the receiver type table visible to one Java method.

    Current-class fields are the base scope, and parameters shadow them for the
    full method. Conflicting local declarations are omitted because raw call
    facts do not retain lexical scope.
    """
    method_types: dict[str, str] = {}
    ambiguous: set[str] = set()

    def bind(name: str, type_name: str | None) -> None:
        if not name or not type_name or name in ambiguous:
            return
        previous = method_types.get(name)
        if previous is not None and previous != type_name:
            method_types.pop(name, None)
            ambiguous.add(name)
        else:
            method_types[name] = type_name

    params = method_node.child_by_field_name("parameters")
    if params is not None:
        for param in params.children:
            if param.type not in ("formal_parameter", "spread_parameter"):
                continue
            type_name = _java_receiver_type_name(param.child_by_field_name("type"), source)
            name_node = param.child_by_field_name("name")
            if name_node is not None:
                bind(_read_text(name_node, source), type_name)
    body = method_node.child_by_field_name("body")
    stack = list(body.children) if body is not None else []
    while stack:
        node = stack.pop()
        if node.type in (
            "class_declaration",
            "class_body",
            "interface_declaration",
            "record_declaration",
            "enum_declaration",
            "annotation_type_declaration",
        ):
            continue
        if node.type == "lambda_expression":
            for name, type_name in _java_lambda_parameters(node, source):
                if type_name is None or field_types.get(name) not in (None, type_name):
                    method_types.pop(name, None)
                    ambiguous.add(name)
                else:
                    bind(name, type_name)
        if node.type == "local_variable_declaration":
            type_name = _java_receiver_type_name(node.child_by_field_name("type"), source)
            for name in _java_declarator_names(node, source):
                if field_types.get(name) not in (None, type_name):
                    method_types.pop(name, None)
                    ambiguous.add(name)
                else:
                    bind(name, type_name)
        stack.extend(node.children)
    table = dict(field_types)
    table.update(method_types)
    for name in ambiguous:
        table.pop(name, None)
    table.update({f"this.{name}": type_name for name, type_name in field_types.items()})
    return table


def _java_annotation_names(declaration_node, source: bytes) -> list[str]:
    """Collect annotation names from a Java declaration's `modifiers` child."""
    names: list[str] = []
    modifiers = None
    for child in declaration_node.children:
        if child.type == "modifiers":
            modifiers = child
            break
    if modifiers is None:
        return names
    for anno in modifiers.children:
        if anno.type not in ("marker_annotation", "annotation"):
            continue
        name_node = anno.child_by_field_name("name")
        if name_node is None:
            for sub in anno.children:
                if sub.type in ("identifier", "scoped_identifier", "type_identifier"):
                    name_node = sub
                    break
        if name_node is not None:
            text = _read_text(name_node, source).rsplit(".", 1)[-1]
            if text:
                names.append(text)
    return names


_JAVA_HTTP_METHOD_ANNOTATIONS = {
    "getmapping": "GET",
    "postmapping": "POST",
    "putmapping": "PUT",
    "deletemapping": "DELETE",
    "patchmapping": "PATCH",
    "getexchange": "GET",
    "postexchange": "POST",
    "putexchange": "PUT",
    "deleteexchange": "DELETE",
    "patchexchange": "PATCH",
}
_JAVA_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


def _java_declaration_annotations(declaration_node) -> list[object]:
    """Return Java annotation nodes attached directly to one declaration."""
    for child in declaration_node.children:
        if child.type == "modifiers":
            return [
                item for item in child.children if item.type in ("marker_annotation", "annotation")
            ]
    return []


def _java_annotation_name(annotation, source: bytes) -> str:
    name_node = annotation.child_by_field_name("name")
    if name_node is None:
        name_node = next(
            (
                child
                for child in annotation.children
                if child.type in ("identifier", "scoped_identifier", "type_identifier")
            ),
            None,
        )
    return (_read_text(name_node, source).rsplit(".", 1)[-1] if name_node else "").strip()


def _java_string_literal(node, source: bytes) -> str:
    raw = _read_text(node, source).strip()
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        value = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
    return str(value)


def _java_string_values(node, source: bytes) -> list[str]:
    values: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "string_literal":
            values.append(_java_string_literal(current, source))
            continue
        stack.extend(reversed(current.children))
    return values


def _java_annotation_values(annotation, source: bytes) -> dict[str, list[str]]:
    """Return string annotation arguments grouped by key; bare args use ``value``."""
    arguments = annotation.child_by_field_name("arguments")
    if arguments is None:
        arguments = next(
            (child for child in annotation.children if child.type == "annotation_argument_list"),
            None,
        )
    result: dict[str, list[str]] = {}
    if arguments is None:
        return result
    for child in arguments.children:
        if not child.is_named:
            continue
        if child.type == "element_value_pair":
            key_node = child.child_by_field_name("key")
            value_node = child.child_by_field_name("value")
            key = _read_text(key_node, source).strip() if key_node is not None else "value"
            values = _java_string_values(value_node or child, source)
        else:
            key = "value"
            values = _java_string_values(child, source)
        if values:
            result.setdefault(key, []).extend(values)
    return result


def _java_mapping_paths(values: dict[str, list[str]], *keys: str) -> list[str]:
    paths: list[str] = []
    for key in keys:
        paths.extend(values.get(key, []))
    return list(dict.fromkeys((path.strip() for path in paths if path.strip()))) or [""]


def _java_has_unresolved_path_argument(
    annotation, source: bytes, values: dict[str, list[str]], *keys: str
) -> bool:
    """Detect an explicit annotation path expression with no string literal.

    ``@GetMapping`` legitimately means the empty relative path, while
    ``@GetMapping(Routes.GET_ALL)`` is unresolved and must not be represented as
    ``/``.  The latter previously fabricated route matches between unrelated
    constant-based enterprise endpoints.
    """
    if any((values.get(key) for key in keys)):
        return False
    arguments = annotation.child_by_field_name("arguments")
    if arguments is None:
        arguments = next(
            (child for child in annotation.children if child.type == "annotation_argument_list"),
            None,
        )
    if arguments is None:
        return False
    named = [child for child in arguments.children if child.is_named]
    if not named:
        return False
    if any((child.type != "element_value_pair" for child in named)):
        return True
    raw = _read_text(arguments, source)
    key_pattern = "|".join((re.escape(key) for key in keys))
    return bool(re.search(f"\\b(?:{key_pattern})\\s*=", raw))


def _java_http_class_info(
    declaration_node, source: bytes, *, class_name: str, declaration_type: str
) -> dict[str, object]:
    """Extract controller/client role and base routes from a Java type."""
    annotations = _java_declaration_annotations(declaration_node)
    names = {_java_annotation_name(annotation, source).casefold() for annotation in annotations}
    role = ""
    if names & {"restcontroller", "controller"}:
        role = "inbound"
    elif names & {"feignclient", "registerrestclient"}:
        role = "outbound"
    elif declaration_type == "interface_declaration" and class_name.casefold().endswith(
        ("client", "repository", "gateway", "connector", "adapter", "api")
    ):
        role = "outbound"
    base_paths: list[str] = []
    base_paths_resolved = True
    for annotation in annotations:
        name = _java_annotation_name(annotation, source).casefold()
        values = _java_annotation_values(annotation, source)
        if name == "requestmapping":
            if _java_has_unresolved_path_argument(annotation, source, values, "path", "value"):
                base_paths_resolved = False
                continue
            base_paths.extend(_java_mapping_paths(values, "path", "value"))
        elif name == "feignclient":
            if _java_has_unresolved_path_argument(annotation, source, values, "path"):
                base_paths_resolved = False
                continue
            base_paths.extend(values.get("path", []))
        elif name == "httpexchange":
            if _java_has_unresolved_path_argument(annotation, source, values, "url", "value"):
                base_paths_resolved = False
                continue
            base_paths.extend(_java_mapping_paths(values, "url", "value"))
            if not role and declaration_type == "interface_declaration":
                role = "outbound"
    return {
        "role": role,
        "base_paths": list(dict.fromkeys(base_paths)) or [""],
        "base_paths_resolved": base_paths_resolved,
    }


def _java_http_method_mappings(declaration_node, source: bytes) -> list[dict[str, str]]:
    """Extract Spring/Feign HTTP verb and path pairs from one Java method."""
    mappings: list[dict[str, str]] = []
    for annotation in _java_declaration_annotations(declaration_node):
        name = _java_annotation_name(annotation, source).casefold()
        values = _java_annotation_values(annotation, source)
        verb = _JAVA_HTTP_METHOD_ANNOTATIONS.get(name)
        paths: list[str] = []
        if verb:
            if _java_has_unresolved_path_argument(
                annotation, source, values, "path", "value", "url"
            ):
                continue
            paths = _java_mapping_paths(values, "path", "value", "url")
        elif name == "requestmapping":
            if _java_has_unresolved_path_argument(annotation, source, values, "path", "value"):
                continue
            raw = _read_text(annotation, source)
            verbs = list(
                dict.fromkeys(
                    re.findall(
                        "RequestMethod\\s*\\.\\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)",
                        raw,
                        flags=re.IGNORECASE,
                    )
                )
            )
            verbs = [item.upper() for item in verbs] or ["*"]
            paths = _java_mapping_paths(values, "path", "value")
            for request_verb in verbs:
                for path in paths:
                    mappings.append({"method": request_verb, "path": path})
            continue
        elif name == "requestline":
            for request_line in values.get("value", []):
                parts = request_line.strip().split(None, 1)
                if parts and parts[0].upper() in _JAVA_HTTP_VERBS:
                    mappings.append(
                        {
                            "method": parts[0].upper(),
                            "path": parts[1].strip() if len(parts) > 1 else "",
                        }
                    )
            continue
        elif name == "httpexchange":
            if _java_has_unresolved_path_argument(annotation, source, values, "url", "value"):
                continue
            method_values = [value.upper() for value in values.get("method", [])]
            verbs = [value for value in method_values if value in _JAVA_HTTP_VERBS] or ["*"]
            paths = _java_mapping_paths(values, "url", "value")
            for request_verb in verbs:
                for path in paths:
                    mappings.append({"method": request_verb, "path": path})
            continue
        if verb:
            mappings.extend(({"method": verb, "path": path} for path in paths))
    return mappings


def _join_java_http_path(base: str, route: str) -> str:
    parts = [part.strip("/") for part in (base, route) if part and part.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _java_http_method_metadata(
    declaration_node, source: bytes, class_info: dict[str, object] | None
) -> dict[str, object]:
    mappings = _java_http_method_mappings(declaration_node, source)
    role = str((class_info or {}).get("role", ""))
    if not mappings or not role or (not (class_info or {}).get("base_paths_resolved", True)):
        return {}
    bases = list((class_info or {}).get("base_paths", [""])) or [""]
    routes = [
        {"method": mapping["method"], "path": _join_java_http_path(str(base), mapping["path"])}
        for base in bases
        for mapping in mappings
    ]
    unique_routes = list({(item["method"], item["path"]): item for item in routes}.values())
    return {"java_http_role": role, "java_http_routes": unique_routes}


def _java_compact_source(node, source: bytes, *, limit: int | None = None) -> str:
    """Return a single-line Java fragment without truncating evidence by default."""
    if node is None:
        return ""
    text = re.sub("\\s+", " ", _read_text(node, source)).strip()
    if limit is not None and len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _java_annotation_contract(annotation, source: bytes) -> dict[str, object]:
    name = _java_annotation_name(annotation, source)
    values = _java_annotation_values(annotation, source)
    arguments = annotation.child_by_field_name("arguments") or next(
        (child for child in annotation.children if child.type == "annotation_argument_list"), None
    )
    if arguments is not None:
        for child in arguments.named_children:
            if child.type == "element_value_pair":
                key_node = child.child_by_field_name("key")
                value_node = child.child_by_field_name("value")
                key = _read_text(key_node, source).strip() if key_node is not None else "value"
                raw = _java_compact_source(value_node, source)
            else:
                key = "value"
                raw = _java_compact_source(child, source)
            if raw and (not values.get(key)):
                values.setdefault(key, [raw])
    result: dict[str, object] = {"name": name}
    if values:
        result["values"] = values
    return result


def _java_parameter_contract(parameter, source: bytes) -> dict[str, object]:
    """Extract a Java parameter's declared type and HTTP binding semantics."""
    name_node = parameter.child_by_field_name("name")
    type_node = parameter.child_by_field_name("type")
    name = _read_text(name_node, source) if name_node is not None else ""
    declared_type = _java_compact_source(type_node, source)
    annotations = [
        _java_annotation_contract(annotation, source)
        for annotation in _java_declaration_annotations(parameter)
    ]
    annotation_names = {str(annotation.get("name") or "").casefold() for annotation in annotations}
    binding = "argument"
    for annotation_name, candidate in (
        ("requestbody", "body"),
        ("requestpart", "multipart"),
        ("pathvariable", "path"),
        ("requestparam", "query"),
        ("requestheader", "header"),
        ("cookievalue", "cookie"),
        ("modelattribute", "model"),
    ):
        if annotation_name in annotation_names:
            binding = candidate
            break
    contract: dict[str, object] = {"name": name, "type": declared_type, "binding": binding}
    if annotations:
        contract["annotations"] = annotations
    contract["validated"] = bool(annotation_names & {"valid", "validated"})
    validation_names = {
        "notnull",
        "nonnull",
        "notempty",
        "notblank",
        "null",
        "size",
        "min",
        "max",
        "decimalmin",
        "decimalmax",
        "positive",
        "positiveorzero",
        "negative",
        "negativeorzero",
        "digits",
        "pattern",
        "email",
        "past",
        "pastorpresent",
        "future",
        "futureorpresent",
        "asserttrue",
        "assertfalse",
    }
    constraints = [
        annotation
        for annotation in annotations
        if str(annotation.get("name") or "").casefold() in validation_names
    ]
    if constraints:
        contract["constraints"] = constraints
    for annotation in annotations:
        if str(annotation.get("name") or "").casefold() not in {
            "pathvariable",
            "requestparam",
            "requestheader",
            "cookievalue",
            "requestpart",
            "modelattribute",
        }:
            continue
        values = annotation.get("values")
        if not isinstance(values, dict):
            continue
        external = values.get("name") or values.get("value")
        if isinstance(external, list) and external:
            contract["external_name"] = external[0]
        required = values.get("required")
        if isinstance(required, list) and required:
            contract["required"] = str(required[0]).casefold() != "false"
        default = values.get("defaultvalue")
        if isinstance(default, list) and default:
            contract["default"] = default[0]
    return contract


def _java_contains(outer, inner) -> bool:
    return bool(
        outer is not None
        and inner is not None
        and (outer.start_byte <= inner.start_byte)
        and (inner.end_byte <= outer.end_byte)
    )


def _java_receiver_descriptor(
    receiver, source: bytes
) -> tuple[str | None, str | None, list[dict[str, object]]]:
    """Describe a Java receiver, including chained invocation return hops.

    ``clientFactory.create().charge()`` is represented as base receiver
    ``clientFactory`` plus a ``create/0`` hop.  Corpus-level resolution can then
    follow the declared return type of ``create`` before binding ``charge``.
    """
    if receiver is None:
        return (None, None, [])
    if receiver.type == "parenthesized_expression":
        nested = next((child for child in receiver.named_children), None)
        return _java_receiver_descriptor(nested, source)
    if receiver.type == "identifier":
        return (_read_text(receiver, source), None, [])
    if receiver.type == "this":
        return ("this", None, [])
    if receiver.type == "field_access":
        owner = receiver.child_by_field_name("object")
        field = receiver.child_by_field_name("field")
        if owner is not None and owner.type == "this" and (field is not None):
            return (f"this.{_read_text(field, source)}", None, [])
        text = _java_compact_source(receiver, source)
        return (text or None, None, [])
    if receiver.type == "object_creation_expression":
        type_name = _java_receiver_type_name(receiver.child_by_field_name("type"), source)
        return ("<new>", type_name, [])
    if receiver.type == "cast_expression":
        type_name = _java_receiver_type_name(receiver.child_by_field_name("type"), source)
        return ("<cast>", type_name, [])
    if receiver.type == "method_invocation":
        inner = receiver.child_by_field_name("object")
        if inner is None:
            base_receiver, type_hint, chain = ("this", None, [])
        else:
            base_receiver, type_hint, chain = _java_receiver_descriptor(inner, source)
        name_node = receiver.child_by_field_name("name")
        arguments = receiver.child_by_field_name("arguments")
        if name_node is None:
            return (base_receiver, type_hint, chain)
        hop = {
            "callee": _read_text(name_node, source),
            "argument_count": len(arguments.named_children) if arguments is not None else 0,
        }
        return (base_receiver, type_hint, [*chain, hop])
    text = _java_compact_source(receiver, source)
    return (text or None, None, [])


def _java_statement_exits(statement) -> bool:
    """Whether a Java statement guarantees leaving its current flow path."""
    if statement is None:
        return False
    if statement.type in {
        "return_statement",
        "throw_statement",
        "break_statement",
        "continue_statement",
    }:
        return True
    if statement.type in {"block", "constructor_body"}:
        named = list(statement.named_children)
        return bool(named and _java_statement_exits(named[-1]))
    if statement.type == "if_statement":
        consequence = statement.child_by_field_name("consequence")
        alternative = statement.child_by_field_name("alternative")
        return bool(
            alternative is not None
            and _java_statement_exits(consequence)
            and _java_statement_exits(alternative)
        )
    return False


def _java_preceding_guard_conditions(node, source: bytes) -> list[dict[str, object]]:
    """Infer path predicates created by earlier terminating guard clauses."""
    guards: list[dict[str, object]] = []
    current = node
    while current is not None and current.type not in {
        "method_declaration",
        "constructor_declaration",
    }:
        parent = current.parent
        if parent is not None and parent.type in {"block", "constructor_body"}:
            siblings = list(parent.named_children)
            try:
                position = siblings.index(current)
            except ValueError:
                position = -1
            if position >= 0:
                for previous in siblings[:position]:
                    if previous.type != "if_statement":
                        continue
                    expression = _java_condition_expression(previous, source)
                    if not expression:
                        continue
                    consequence = previous.child_by_field_name("consequence")
                    alternative = previous.child_by_field_name("alternative")
                    consequence_exits = _java_statement_exits(consequence)
                    alternative_exits = _java_statement_exits(alternative)
                    if consequence_exits and (not alternative_exits):
                        guards.append(
                            {
                                "kind": "guard",
                                "branch": "after_guard",
                                "expression": expression,
                                "line": f"L{previous.start_point[0] + 1}",
                            }
                        )
                    elif alternative_exits and (not consequence_exits):
                        guards.append(
                            {
                                "kind": "guard",
                                "branch": "after_else_guard",
                                "expression": expression,
                                "line": f"L{previous.start_point[0] + 1}",
                            }
                        )
        current = parent
    return guards


def _java_condition_expression(node, source: bytes) -> str:
    condition = node.child_by_field_name("condition") or node.child_by_field_name("expression")
    text = _java_compact_source(condition, source)
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def _java_call_conditions(call_node, source: bytes) -> list[dict[str, object]]:
    """Return the control-flow predicates that guard one Java invocation.

    The evidence is syntactic: it records the branch containing the call and
    never claims that the predicate was true at runtime.
    """
    conditions: list[dict[str, object]] = []
    current = call_node.parent
    while current is not None and current.type not in {
        "method_declaration",
        "constructor_declaration",
    }:
        kind = current.type
        line = f"L{current.start_point[0] + 1}"
        if kind == "if_statement":
            expression = _java_condition_expression(current, source)
            consequence = current.child_by_field_name("consequence")
            alternative = current.child_by_field_name("alternative")
            if expression and _java_contains(consequence, call_node):
                conditions.append(
                    {"kind": "if", "branch": "then", "expression": expression, "line": line}
                )
            elif expression and _java_contains(alternative, call_node):
                conditions.append(
                    {"kind": "if", "branch": "else", "expression": expression, "line": line}
                )
        elif kind in {"while_statement", "do_statement"}:
            expression = _java_condition_expression(current, source)
            if expression and _java_contains(current.child_by_field_name("body"), call_node):
                conditions.append(
                    {
                        "kind": "loop",
                        "branch": "do_at_least_once" if kind == "do_statement" else "while",
                        "expression": expression,
                        "line": line,
                    }
                )
        elif kind == "for_statement":
            expression = _java_compact_source(current.child_by_field_name("condition"), source)
            if expression and _java_contains(current.child_by_field_name("body"), call_node):
                conditions.append(
                    {"kind": "loop", "branch": "for", "expression": expression, "line": line}
                )
        elif kind == "binary_expression":
            operator = _read_text(current.child_by_field_name("operator"), source)
            if operator in {"&&", "||"} and _java_contains(
                current.child_by_field_name("right"), call_node
            ):
                conditions.append(
                    {
                        "kind": "short_circuit",
                        "branch": "then" if operator == "&&" else "else",
                        "expression": _java_compact_source(
                            current.child_by_field_name("left"), source
                        ),
                        "line": line,
                    }
                )
        elif kind == "enhanced_for_statement":
            name = _java_compact_source(current.child_by_field_name("name"), source)
            value = _java_compact_source(current.child_by_field_name("value"), source)
            expression = (
                f"{name} in {value}" if name and value else _java_compact_source(current, source)
            )
            if _java_contains(current.child_by_field_name("body"), call_node):
                conditions.append(
                    {"kind": "loop", "branch": "for_each", "expression": expression, "line": line}
                )
        elif kind == "ternary_expression":
            expression = _java_condition_expression(current, source)
            consequence = current.child_by_field_name("consequence")
            alternative = current.child_by_field_name("alternative")
            branch = "then" if _java_contains(consequence, call_node) else "else"
            if expression and (
                _java_contains(consequence, call_node) or _java_contains(alternative, call_node)
            ):
                conditions.append(
                    {"kind": "ternary", "branch": branch, "expression": expression, "line": line}
                )
        elif kind in {"switch_rule", "switch_block_statement_group"}:
            label_node = next(
                (child for child in current.named_children if child.type == "switch_label"), None
            )
            label = _java_compact_source(label_node, source)
            switch_node = current.parent
            while switch_node is not None and switch_node.type != "switch_expression":
                switch_node = switch_node.parent
            selector = (
                _java_condition_expression(switch_node, source) if switch_node is not None else ""
            )
            if label.casefold() == "default":
                expression = f"no explicit {selector or 'switch'} case matches"
                branch = "else"
            else:
                case_value = re.sub("^case\\s+", "", label, flags=re.IGNORECASE)
                expression = f"{selector} is {case_value}" if selector else label
                branch = "case"
            conditions.append(
                {"kind": "switch", "branch": branch, "expression": expression, "line": line}
            )
        elif kind == "catch_clause":
            parameter = current.child_by_field_name("parameter")
            conditions.append(
                {
                    "kind": "catch",
                    "branch": "exception",
                    "expression": _java_compact_source(parameter, source) or "exception",
                    "line": line,
                }
            )
        current = current.parent
    conditions.reverse()
    conditions.extend(_java_preceding_guard_conditions(call_node, source))
    unique: list[dict[str, object]] = []
    for condition in conditions:
        if condition not in unique:
            unique.append(condition)
    unique.sort(
        key=lambda value: (
            int(str(value.get("line") or "L0").lstrip("L") or 0),
            str(value.get("branch") or ""),
        )
    )
    return unique


def _java_method_logic(method_node, source: bytes) -> tuple[list[dict], list[dict]]:
    """Extract method decisions and observable return/throw outcomes."""
    decisions: list[dict] = []
    outcomes: list[dict] = []

    def visit(node) -> None:
        if node is not method_node and node.type in {
            "method_declaration",
            "constructor_declaration",
            "class_declaration",
            "interface_declaration",
            "record_declaration",
            "enum_declaration",
        }:
            return
        line = f"L{node.start_point[0] + 1}"
        if node.type == "if_statement":
            expression = _java_condition_expression(node, source)
            if expression:
                decisions.append({"kind": "if", "expression": expression, "line": line})
                if node.child_by_field_name("alternative") is not None:
                    decisions.append({"kind": "else", "expression": expression, "line": line})
        elif node.type in {"while_statement", "do_statement", "for_statement"}:
            expression = _java_condition_expression(node, source)
            if expression:
                decisions.append({"kind": "loop", "expression": expression, "line": line})
        elif node.type == "enhanced_for_statement":
            name = _java_compact_source(node.child_by_field_name("name"), source)
            value = _java_compact_source(node.child_by_field_name("value"), source)
            decisions.append(
                {
                    "kind": "for_each",
                    "expression": f"{name} in {value}"
                    if name and value
                    else _java_compact_source(node, source),
                    "line": line,
                }
            )
        elif node.type == "ternary_expression":
            expression = _java_condition_expression(node, source)
            if expression:
                decisions.append({"kind": "ternary", "expression": expression, "line": line})
        elif node.type == "switch_expression":
            expression = _java_condition_expression(node, source)
            if expression:
                decisions.append({"kind": "switch", "expression": expression, "line": line})
        elif node.type == "switch_label":
            decisions.append(
                {"kind": "case", "expression": _java_compact_source(node, source), "line": line}
            )
        elif node.type == "catch_clause":
            parameter = node.child_by_field_name("parameter")
            decisions.append(
                {
                    "kind": "catch",
                    "expression": _java_compact_source(parameter, source) or "exception",
                    "line": line,
                }
            )
        elif node.type == "return_statement":
            value = next((child for child in node.named_children), None)
            if value is not None and value.type == "ternary_expression":
                pending_values = [value]
                while pending_values:
                    item = pending_values.pop(0)
                    if item is not None and item.type == "ternary_expression":
                        pending_values.extend(
                            [
                                item.child_by_field_name("consequence"),
                                item.child_by_field_name("alternative"),
                            ]
                        )
                    elif item is not None:
                        outcomes.append(
                            {
                                "kind": "return",
                                "expression": _java_compact_source(item, source),
                                "line": f"L{item.start_point[0] + 1}",
                                "conditions": _java_call_conditions(item, source),
                            }
                        )
                for child in node.children:
                    visit(child)
                return
            if value is not None and value.type == "switch_expression":
                outcome_expression = "selected switch result"
            elif value is not None and value.type == "ternary_expression":
                outcome_expression = "selected conditional result"
            else:
                outcome_expression = _java_compact_source(value, source)
            outcomes.append(
                {
                    "kind": "return",
                    "expression": outcome_expression,
                    "line": line,
                    "conditions": _java_call_conditions(node, source),
                }
            )
        elif node.type == "throw_statement":
            value = next((child for child in node.named_children), None)
            outcomes.append(
                {
                    "kind": "throw",
                    "expression": _java_compact_source(value, source),
                    "line": line,
                    "conditions": _java_call_conditions(node, source),
                }
            )
        for child in node.children:
            visit(child)

    visit(method_node)
    return (decisions, outcomes)


def _java_method_contract_metadata(method_node, source: bytes) -> dict[str, object]:
    """Extract request/response types and business-decision evidence for a method."""
    metadata: dict[str, object] = {}
    parameters_node = method_node.child_by_field_name("parameters")
    parameters: list[dict[str, object]] = []
    if parameters_node is not None:
        for parameter in parameters_node.named_children:
            if parameter.type in {"formal_parameter", "spread_parameter"}:
                parameters.append(_java_parameter_contract(parameter, source))
    metadata["java_parameters"] = parameters
    metadata["java_parameter_count"] = len(parameters)
    metadata["java_varargs"] = bool(
        parameters_node is not None
        and any(item.type == "spread_parameter" for item in parameters_node.named_children)
    )
    method_name_node = method_node.child_by_field_name("name")
    method_name = (
        _read_text(method_name_node, source) if method_name_node is not None else "constructor"
    )
    metadata["java_signature"] = (
        f"{method_name}("
        + ", ".join((str(parameter.get("type") or "?") for parameter in parameters))
        + ")"
    )
    return_node = method_node.child_by_field_name("type")
    if return_node is not None:
        metadata["java_return_type"] = _java_compact_source(return_node, source)
        refs: list[tuple[str, str]] = []
        _java_collect_type_refs(return_node, source, False, refs)
        metadata["java_response_types"] = list(dict.fromkeys((name for name, _role in refs)))
    else:
        metadata["java_return_type"] = "constructor"
        metadata["java_response_types"] = []
    decisions, outcomes = _java_method_logic(method_node, source)
    if decisions:
        metadata["java_decisions"] = decisions
    if outcomes:
        metadata["java_outcomes"] = outcomes
    return metadata


def _find_body(node, config: LanguageConfig):
    """Find the body node using config.body_field, falling back to child types."""
    b = node.child_by_field_name(config.body_field)
    if b:
        return b
    for child in node.children:
        if child.type in config.body_fallback_child_types:
            return child
    return None


def _java_extra_walk(
    node,
    source: bytes,
    file_nid: str,
    stem: str,
    str_path: str,
    nodes: list,
    edges: list,
    seen_ids: set,
    function_bodies: list,
    parent_class_nid: str | None,
    add_node_fn,
    add_edge_fn,
    walk_fn,
) -> bool:
    """Handle enum_constant for Java. Returns True if handled."""
    if node.type == "enum_constant" and parent_class_nid:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return True
        const_name = _read_text(name_node, source)
        line = node.start_point[0] + 1
        const_nid = _make_id(parent_class_nid, const_name)
        add_node_fn(const_nid, const_name, line)
        add_edge_fn(parent_class_nid, const_nid, "case_of", line)
        for child in node.children:
            if child.type == "class_body":
                for member in child.children:
                    walk_fn(member, parent_class_nid=const_nid)
        return True
    return False


def _extract_generic(
    path: Path, config: LanguageConfig, *, source_override: bytes | None = None
) -> dict:
    """Generic AST extractor driven by LanguageConfig.

    ``source_override`` parses the given bytes instead of reading ``path``, while
    still keying nodes/edges off ``path``. Lets container formats (e.g. Vue SFCs)
    mask the wrapper and parse just the embedded ``<script>``.
    """
    try:
        mod = importlib.import_module("tree_sitter_java")
        from tree_sitter import Language, Parser

        lang_fn = getattr(mod, config.ts_language_fn, None)
        if lang_fn is None:
            lang_fn = getattr(mod, "language", None)
        if lang_fn is None:
            return {
                "nodes": [],
                "edges": [],
                "error": f"No language function in {'tree_sitter_java'}",
            }
        language = Language(lang_fn())
    except ImportError:
        return {"nodes": [], "edges": [], "error": f"{'tree_sitter_java'} not installed"}
    except TypeError as e:
        hint = f"tree-sitter version mismatch for {'tree_sitter_java'}: {e}. Try: pip install --upgrade tree-sitter tree-sitter-languages"
        return {"nodes": [], "edges": [], "error": hint}
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}
    try:
        parser = Parser(language)
        source = path.read_bytes() if source_override is None else source_override
        tree = parser.parse(source)
        root = tree.root_node
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}
    stem = _file_stem(path)
    str_path = str(path)
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()
    namespace_stack: list[str] = []
    package_name = ""
    for declaration in root.named_children:
        if declaration.type == "package_declaration":
            package_name = (
                _read_text(declaration, source).strip().removeprefix("package").strip().rstrip(";")
            )
    scope_stack: list[str] = []
    function_bodies: list[tuple[str, object]] = []
    callable_def_nids: set[str] = set()
    callable_class_nids: set[str] = set()
    local_bound_names: dict[str, set[str]] = {}
    java_field_types: dict[str, dict[str, str]] = {}
    java_method_scopes: dict[int, tuple[object, str]] = {}
    java_http_classes: dict[str, dict[str, object]] = {}

    def add_node(
        nid: str,
        label: str,
        line: int,
        *,
        node_type: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if nid in seen_ids:
            return
        seen_ids.add(nid)
        merged = dict(metadata or {})
        if package_name:
            merged.setdefault("namespace", package_name)
        if namespace_stack:
            merged.setdefault("namespace", ".".join(namespace_stack))
        if scope_stack and node_type != "namespace":
            merged.setdefault("scope_chain", list(scope_stack))
        node = {
            "id": nid,
            "label": label,
            "file_type": "code",
            "source_file": str_path,
            "source_location": f"L{line}",
        }
        if node_type:
            node["type"] = node_type
        if merged:
            node["metadata"] = sanitize_metadata(merged)
        nodes.append(node)

    def add_edge(
        src: str,
        tgt: str,
        relation: str,
        line: int,
        confidence: str = "EXTRACTED",
        weight: float = 1.0,
        context: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        edge = {
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": confidence,
            "source_file": str_path,
            "source_location": f"L{line}",
            "weight": weight,
        }
        if context:
            edge["context"] = context
        if metadata:
            edge["metadata"] = sanitize_metadata(metadata)
        edges.append(edge)

    def ensure_named_node(name: str, line: int) -> str:
        nid = _make_id(stem, ".".join(namespace_stack), name)
        if nid in seen_ids:
            return nid
        nid = _make_id(name)
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes.append(
                {
                    "id": nid,
                    "label": name,
                    "file_type": "code",
                    "source_file": "",
                    "source_location": "",
                    "origin_file": str_path,
                }
            )
        return nid

    file_nid = _make_id(str(path))
    add_node(file_nid, path.name, 1)
    if root.has_error:
        parse_errors = []
        pending = [root]
        while pending:
            current = pending.pop()
            if current.type == "ERROR" or current.is_missing:
                parse_errors.append(
                    {
                        "kind": "missing" if current.is_missing else "syntax_error",
                        "line": current.start_point[0] + 1,
                        "column": current.start_point[1] + 1,
                    }
                )
            pending.extend(reversed(current.children))
        nodes[0]["metadata"] = {"java_parse_errors": parse_errors}

    def walk(node, parent_class_nid: str | None = None) -> None:
        t = node.type
        if t in config.import_types:
            if config.import_handler:
                imported_modules = config.import_handler(
                    node, source, file_nid, stem, edges, str_path, scope_stack
                )
                if imported_modules:
                    line = node.start_point[0] + 1
                    for mod_nid, mod_label in imported_modules:
                        if mod_nid not in seen_ids:
                            seen_ids.add(mod_nid)
                            nodes.append(
                                {
                                    "id": mod_nid,
                                    "label": mod_label,
                                    "file_type": "code",
                                    "type": "module",
                                    "source_file": str_path,
                                    "source_location": f"L{line}",
                                }
                            )
            return
        if t in config.class_types:
            name_node = node.child_by_field_name(config.name_field)
            if name_node is None:
                for child in node.children:
                    if child.type in config.name_fallback_child_types:
                        name_node = child
                        break
            if not name_node:
                return
            class_name = _read_text(name_node, source)
            class_nid = _make_id(stem, ".".join(namespace_stack), class_name)
            line = node.start_point[0] + 1
            metadata = None
            java_http_info = _java_http_class_info(
                node, source, class_name=class_name, declaration_type=t
            )
            java_http_classes[class_nid] = java_http_info
            if java_http_info.get("role"):
                metadata = dict(metadata or {})
                metadata["java_http_role"] = java_http_info["role"]
                metadata["java_http_base_paths"] = java_http_info["base_paths"]
            add_node(class_nid, class_name, line, metadata=metadata)
            callable_def_nids.add(class_nid)
            callable_class_nids.add(class_nid)
            if parent_class_nid and parent_class_nid != class_nid:
                add_edge(parent_class_nid, class_nid, "contains", line)
            else:
                add_edge(file_nid, class_nid, "contains", line)

            def _emit_java_parent(base_name: str, rel: str, at_line: int) -> None:
                if not base_name:
                    return
                base_nid = _make_id(stem, base_name)
                if base_nid not in seen_ids:
                    base_nid = _make_id(base_name)
                    if base_nid not in seen_ids:
                        nodes.append(
                            {
                                "id": base_nid,
                                "label": base_name,
                                "file_type": "code",
                                "source_file": "",
                                "source_location": "",
                            }
                        )
                        seen_ids.add(base_nid)
                add_edge(class_nid, base_nid, rel, at_line)

            def _emit_java_parent_type(type_node, rel: str, at_line: int) -> None:
                refs: list[tuple[str, str]] = []
                _java_collect_type_refs(type_node, source, False, refs)
                parent_emitted = False
                for ref_name, role in refs:
                    if role == "type" and (not parent_emitted):
                        _emit_java_parent(ref_name, rel, at_line)
                        parent_emitted = True
                    elif role == "generic_arg":
                        target_nid = ensure_named_node(ref_name, at_line)
                        if target_nid != class_nid:
                            add_edge(
                                class_nid, target_nid, "references", at_line, context="generic_arg"
                            )

            sup = node.child_by_field_name("superclass")
            if sup is not None:
                for sub in sup.children:
                    if sub.is_named:
                        _emit_java_parent_type(sub, "inherits", line)
                        break
            ifs = node.child_by_field_name("interfaces")
            if ifs is not None:
                for sub in ifs.children:
                    if sub.type == "type_list":
                        for tid in sub.children:
                            if tid.is_named:
                                _emit_java_parent_type(tid, "implements", line)
            if t == "interface_declaration":
                for child in node.children:
                    if child.type == "extends_interfaces":
                        for sub in child.children:
                            if sub.type == "type_list":
                                for tid in sub.children:
                                    if tid.is_named:
                                        _emit_java_parent_type(tid, "inherits", line)
            for anno_name in _java_annotation_names(node, source):
                target_nid = ensure_named_node(anno_name, line)
                if target_nid != class_nid:
                    add_edge(class_nid, target_nid, "references", line, context="attribute")
            if t == "record_declaration":
                components = node.child_by_field_name("parameters")
                if components is not None:
                    for component in components.children:
                        if component.type == "formal_parameter":
                            type_node = component.child_by_field_name("type")
                        elif component.type == "spread_parameter":
                            type_node = next(
                                (
                                    child
                                    for child in component.children
                                    if child.is_named
                                    and child.type not in ("modifiers", "variable_declarator")
                                ),
                                None,
                            )
                        else:
                            continue
                        refs: list[tuple[str, str]] = []
                        _java_collect_type_refs(type_node, source, False, refs)
                        component_line = component.start_point[0] + 1
                        for ref_name, role in refs:
                            ctx = "generic_arg" if role == "generic_arg" else "field"
                            target_nid = ensure_named_node(ref_name, component_line)
                            if target_nid != class_nid:
                                add_edge(
                                    class_nid, target_nid, "references", component_line, context=ctx
                                )
            body = _find_body(node, config)
            if body:
                for child in body.children:
                    walk(child, parent_class_nid=class_nid)
            return
        if t == "field_declaration" and parent_class_nid:
            owner_node = next(n for n in nodes if n["id"] == parent_class_nid)
            declared_type = _java_compact_source(node.child_by_field_name("type"), source)
            owner_metadata = owner_node.setdefault("metadata", {})
            declared_fields = owner_metadata.setdefault("java_declared_fields", {})
            for field_name in _java_declarator_names(node, source):
                declared_fields[field_name] = sanitize_metadata(declared_type)
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                receiver_type = _java_receiver_type_name(type_node, source)
                if receiver_type:
                    fields = java_field_types.setdefault(parent_class_nid, {})
                    for field_name in _java_declarator_names(node, source):
                        fields[field_name] = receiver_type
                line = node.start_point[0] + 1
                refs: list[tuple[str, str]] = []
                _java_collect_type_refs(type_node, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "field"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != parent_class_nid:
                        add_edge(parent_class_nid, target_nid, "references", line, context=ctx)
            return
        if t in config.function_types:
            if t == "deinit_declaration":
                func_name: str | None = "deinit"
            elif t == "subscript_declaration":
                func_name = "subscript"
            elif config.resolve_function_name_fn is not None:
                declarator = node.child_by_field_name("declarator")
                func_name = None
                if declarator:
                    func_name = config.resolve_function_name_fn(declarator, source)
            else:
                name_node = node.child_by_field_name(config.name_field)
                if name_node is None:
                    for child in node.children:
                        if child.type in config.name_fallback_child_types:
                            name_node = child
                            break
                func_name = _read_text(name_node, source) if name_node else None
            if not func_name:
                return
            if not normalize_id(func_name):
                return
            line = node.start_point[0] + 1
            func_metadata = None
            if parent_class_nid:
                java_method_metadata = _java_method_contract_metadata(node, source)
                java_http_metadata = _java_http_method_metadata(
                    node, source, java_http_classes.get(parent_class_nid)
                )
                if java_http_metadata:
                    java_method_metadata.update(java_http_metadata)
                func_metadata = java_method_metadata
            if parent_class_nid:
                if func_metadata:
                    func_identity = str(func_metadata.get("java_signature") or func_name)
                    func_nid = _make_id(parent_class_nid, func_identity)
                else:
                    func_nid = _make_id(parent_class_nid, func_name)
                add_node(func_nid, f".{func_name}()", line, metadata=func_metadata)
                add_edge(parent_class_nid, func_nid, "method", line)
            else:
                func_nid = _make_id(stem, func_name)
                add_node(func_nid, f"{func_name}()", line, metadata=func_metadata)
                add_edge(file_nid, func_nid, "contains", line)
            callable_def_nids.add(func_nid)
            params_node = node.child_by_field_name("parameters")
            if params_node is not None:
                for p in params_node.children:
                    if p.type != "formal_parameter":
                        continue
                    type_node = p.child_by_field_name("type")
                    refs = []
                    _java_collect_type_refs(type_node, source, False, refs)
                    for ref_name, role in refs:
                        ctx = "generic_arg" if role == "generic_arg" else "parameter_type"
                        target_nid = ensure_named_node(ref_name, line)
                        if target_nid != func_nid:
                            add_edge(func_nid, target_nid, "references", line, context=ctx)
            return_node = node.child_by_field_name("type")
            if return_node is not None:
                refs = []
                _java_collect_type_refs(return_node, source, False, refs)
                for ref_name, role in refs:
                    ctx = "generic_arg" if role == "generic_arg" else "return_type"
                    target_nid = ensure_named_node(ref_name, line)
                    if target_nid != func_nid:
                        add_edge(func_nid, target_nid, "references", line, context=ctx)
            for anno_name in _java_annotation_names(node, source):
                target_nid = ensure_named_node(anno_name, line)
                if target_nid != func_nid:
                    add_edge(func_nid, target_nid, "references", line, context="attribute")
            body = _find_body(node, config)
            if body:
                if parent_class_nid:
                    java_method_scopes[id(body)] = (node, parent_class_nid)
                function_bodies.append((func_nid, body))
            return
        if _java_extra_walk(
            node,
            source,
            file_nid,
            stem,
            str_path,
            nodes,
            edges,
            seen_ids,
            function_bodies,
            parent_class_nid,
            add_node,
            add_edge,
            walk,
        ):
            return
        if t == "decorated_definition":
            for child in node.children:
                walk(child, parent_class_nid=parent_class_nid)
            return
        for child in node.children:
            walk(child, parent_class_nid=None)

    walk(root)
    label_to_nid: dict[str, str] = {}
    label_to_nid_ci: dict[str, str] = {}
    nid_to_sf: dict[str, str] = {}
    for n in nodes:
        nid_to_sf[n["id"]] = str(n.get("source_file") or "")
        if n.get("type") == "namespace":
            continue
        raw = n["label"]
        normalised = raw.strip("()").lstrip(".")
        label_to_nid[normalised] = n["id"]
        label_to_nid_ci[normalised.lower()] = n["id"]
    java_method_owner: dict[str, str] = {}
    java_local_methods: dict[tuple[str, str, int | None], list[str]] = {}
    node_by_id = {str(node["id"]): node for node in nodes if node.get("id")}
    for edge in edges:
        if edge.get("relation") != "method":
            continue
        owner = str(edge.get("source") or "")
        method = str(edge.get("target") or "")
        method_data = node_by_id.get(method, {})
        label = str(method_data.get("label") or "").lstrip(".").removesuffix("()")
        metadata = method_data.get("metadata")
        arity = (
            metadata.get("java_parameter_count")
            if isinstance(metadata, dict) and isinstance(metadata.get("java_parameter_count"), int)
            else None
        )
        if owner and method and label:
            java_method_owner[method] = owner
            java_local_methods.setdefault((owner, label, arity), []).append(method)
    raw_calls: list[dict] = []
    java_receiver_types = {
        body_id: _java_method_receiver_types(
            method_node, source, java_field_types.get(class_nid, {})
        )
        for body_id, (method_node, class_nid) in java_method_scopes.items()
    }

    def walk_calls(
        node,
        caller_nid: str,
        receiver_types: dict[str, str] | None = None,
        extra_locals: frozenset[str] = frozenset(),
    ) -> None:
        if node.type in config.function_boundary_types:
            return
        if node.type in config.call_types:
            callee_name: str | None = None
            is_member_call: bool = False
            is_this_field_call: bool = False
            member_receiver: str | None = None
            java_receiver_type_hint: str | None = None
            java_receiver_chain: list[dict[str, object]] = []
            java_conditions = _java_call_conditions(node, source)
            java_arguments = node.child_by_field_name("arguments")
            java_argument_count = (
                len(java_arguments.named_children) if java_arguments is not None else None
            )
            java_argument_values = (
                [
                    _java_compact_source(argument, source)
                    for argument in java_arguments.named_children
                ]
                if java_arguments is not None
                else []
            )
            if node.type == "object_creation_expression":
                type_node = node.child_by_field_name("type")
                if type_node is not None:
                    raw = _read_text(type_node, source).split("<", 1)[0].strip()
                    if raw:
                        callee_name = raw.rsplit(".", 1)[-1]
            elif node.type == "method_reference":
                named = list(node.named_children)
                if len(named) >= 2:
                    receiver = named[0]
                    callee_name = _read_text(named[-1], source)
                    is_member_call = True
                    member_receiver, java_receiver_type_hint, java_receiver_chain = (
                        _java_receiver_descriptor(receiver, source)
                    )
                    is_this_field_call = bool(
                        member_receiver and member_receiver.startswith("this.")
                    )
            elif node.type == "method_invocation":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    callee_name = _read_text(name_node, source)
                receiver = node.child_by_field_name("object")
                if receiver is not None:
                    is_member_call = True
                    member_receiver, java_receiver_type_hint, java_receiver_chain = (
                        _java_receiver_descriptor(receiver, source)
                    )
                    is_this_field_call = bool(
                        member_receiver and member_receiver.startswith("this.")
                    )
                else:
                    is_member_call = True
                    member_receiver = "this"
            if callee_name:
                _java_defer = is_member_call
                java_local_target = None
                if member_receiver == "this":
                    owner = java_method_owner.get(caller_nid)
                    candidates = java_local_methods.get(
                        (owner or "", callee_name, java_argument_count), []
                    )
                    if len(candidates) == 1:
                        java_local_target = candidates[0]
                if java_local_target is not None:
                    tgt_nid = java_local_target
                elif _java_defer or (
                    is_member_call
                    and member_receiver
                    and (member_receiver[:1].isupper() or is_this_field_call)
                ):
                    tgt_nid = None
                else:
                    tgt_nid = label_to_nid.get(callee_name)
                if tgt_nid:
                    pair = (caller_nid, tgt_nid)
                    line = node.start_point[0] + 1
                    edges.append(
                        {
                            "source": caller_nid,
                            "target": tgt_nid,
                            "relation": "calls",
                            "context": "method_reference"
                            if node.type == "method_reference"
                            else "call",
                            "confidence": "INFERRED"
                            if node.type == "method_reference"
                            else "EXTRACTED",
                            **(
                                {"confidence_score": 0.8} if node.type == "method_reference" else {}
                            ),
                            "source_file": str_path,
                            "source_location": f"L{line}",
                            "source_column": node.start_point[1] + 1,
                            "weight": 1.0,
                            **({"conditions": java_conditions} if java_conditions else {}),
                            **(
                                {"argument_count": java_argument_count}
                                if java_argument_count is not None
                                else {}
                            ),
                            **({"arguments": java_argument_values} if java_argument_values else {}),
                        }
                    )
                elif callee_name and (not tgt_nid):
                    rc_entry = {
                        "caller_nid": caller_nid,
                        "callee": callee_name,
                        "is_member_call": is_member_call,
                        "source_file": str_path,
                        "source_location": f"L{node.start_point[0] + 1}",
                        "source_column": node.start_point[1] + 1,
                        "receiver": member_receiver,
                    }
                    rc_entry["call_kind"] = (
                        "constructor"
                        if node.type == "object_creation_expression"
                        else "method_reference"
                        if node.type == "method_reference"
                        else "method"
                    )
                    if java_conditions:
                        rc_entry["conditions"] = java_conditions
                    if java_argument_count is not None:
                        rc_entry["argument_count"] = java_argument_count
                    if java_argument_values:
                        rc_entry["arguments"] = java_argument_values
                    rc_entry["lang"] = "java"
                    receiver_type = java_receiver_type_hint or (receiver_types or {}).get(
                        member_receiver or ""
                    )
                    if receiver_type:
                        rc_entry["receiver_type"] = receiver_type
                    if java_receiver_chain:
                        rc_entry["receiver_chain"] = java_receiver_chain
                    raw_calls.append(rc_entry)
        for child in node.children:
            walk_calls(child, caller_nid, receiver_types, extra_locals)

    receiver_types_by_body = java_receiver_types
    for caller_nid, body_node in function_bodies:
        walk_calls(body_node, caller_nid, receiver_types_by_body.get(id(body_node)))
    valid_ids = seen_ids
    clean_edges = []
    for edge in edges:
        src, tgt = (edge["source"], edge["target"])
        if src in valid_ids and (
            tgt in valid_ids or edge["relation"] in ("imports", "imports_from", "re_exports")
        ):
            clean_edges.append(edge)
    result = {"nodes": nodes, "edges": clean_edges, "raw_calls": raw_calls}
    if callable_def_nids:
        for n in nodes:
            if n["id"] in callable_def_nids:
                n["_callable"] = True
                if n["id"] in callable_class_nids:
                    n["_callable_class"] = True
    if java_field_types:
        node_by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
        for class_nid, fields in java_field_types.items():
            class_node = node_by_id.get(class_nid)
            if class_node is None or not fields:
                continue
            metadata = dict(class_node.get("metadata") or {})
            metadata["java_fields"] = sanitize_metadata({"java_fields": fields})["java_fields"]
            class_node["metadata"] = metadata
    return result
