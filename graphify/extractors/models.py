from __future__ import annotations
from typing import Callable
from dataclasses import dataclass


@dataclass
class LanguageConfig:
    ts_module: str
    ts_language_fn: str = "language"
    class_types: frozenset = frozenset()
    function_types: frozenset = frozenset()
    import_types: frozenset = frozenset()
    call_types: frozenset = frozenset()
    name_field: str = "name"
    name_fallback_child_types: tuple = ()
    body_field: str = "body"
    body_fallback_child_types: tuple = ()
    call_function_field: str = "function"
    call_accessor_node_types: frozenset = frozenset()
    call_accessor_field: str = "attribute"
    call_accessor_object_field: str = ""
    function_boundary_types: frozenset = frozenset()
    import_handler: Callable | None = None
    resolve_function_name_fn: Callable | None = None
    function_label_parens: bool = True
    extra_walk_fn: Callable | None = None
