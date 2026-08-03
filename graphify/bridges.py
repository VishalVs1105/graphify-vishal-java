"""Explicit cross-service relationships for merged Graphify graphs."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import networkx as nx


_CONFIDENCES = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}
_JAVA_CLIENT_SUFFIX = "client"
_JAVA_CONTROLLER_SUFFIX = "controller"
_HTTP_PATH_VARIABLE_RE = re.compile(r"\{[^/{}]+\}")


def _normalise_symbol(value: object) -> str:
    text = str(value or "").strip()
    if text.endswith("()"):
        text = text[:-2]
    return text.casefold()


def _resolve_node(G: nx.Graph, *, repo: str, symbol: str) -> str:
    """Resolve one repo-qualified label/local id to exactly one merged node."""
    repo_matches = [
        (node_id, data)
        for node_id, data in G.nodes(data=True)
        if str(data.get("repo", "")) == repo
    ]
    if not repo_matches:
        available = sorted({str(data.get("repo")) for _, data in G.nodes(data=True) if data.get("repo")})
        raise ValueError(
            f"bridge repo {repo!r} was not found; available repos: {', '.join(available) or '(none)'}"
        )

    wanted = _normalise_symbol(symbol)
    matches: list[str] = []
    for node_id, data in repo_matches:
        candidates = {
            _normalise_symbol(data.get("label")),
            _normalise_symbol(data.get("local_id")),
            _normalise_symbol(node_id),
        }
        if wanted in candidates:
            matches.append(str(node_id))

    if not matches:
        raise ValueError(f"bridge symbol {repo}:{symbol} was not found")
    if len(matches) > 1:
        rendered = ", ".join(matches[:8])
        raise ValueError(
            f"bridge symbol {repo}:{symbol} is ambiguous ({len(matches)} matches): {rendered}"
        )
    return matches[0]


def load_bridge_contract(path: str | Path) -> list[dict]:
    """Load and validate ``{"bridges": [...]}`` from a JSON contract file."""
    contract_path = Path(path)
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"bridge contract not found: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid bridge contract JSON at {contract_path}: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("bridges"), list):
        raise ValueError("bridge contract must be a JSON object with a 'bridges' array")

    required = ("source_repo", "source", "target_repo", "target")
    bridges: list[dict] = []
    for index, raw in enumerate(payload["bridges"], 1):
        if not isinstance(raw, dict):
            raise ValueError(f"bridge #{index} must be a JSON object")
        missing = [field for field in required if not str(raw.get(field, "")).strip()]
        if missing:
            raise ValueError(f"bridge #{index} is missing: {', '.join(missing)}")
        confidence = str(raw.get("confidence", "EXTRACTED")).upper()
        if confidence not in _CONFIDENCES:
            raise ValueError(
                f"bridge #{index} has invalid confidence {confidence!r}; "
                f"expected one of {', '.join(sorted(_CONFIDENCES))}"
            )
        try:
            confidence_score = float(
                raw.get("confidence_score", 1.0 if confidence == "EXTRACTED" else 0.85)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bridge #{index} confidence_score must be numeric") from exc
        if not 0.0 <= confidence_score <= 1.0:
            raise ValueError(f"bridge #{index} confidence_score must be between 0 and 1")
        bridges.append({
            "source_repo": str(raw["source_repo"]).strip(),
            "source": str(raw["source"]).strip(),
            "target_repo": str(raw["target_repo"]).strip(),
            "target": str(raw["target"]).strip(),
            "relation": str(raw.get("relation", "calls")).strip() or "calls",
            "confidence": confidence,
            "confidence_score": confidence_score,
        })
    return bridges


def apply_bridge_contract(G: nx.Graph, bridges: list[dict], *, source_file: str) -> int:
    """Resolve and add directed bridge metadata to an otherwise undirected graph."""
    added = 0
    for bridge in bridges:
        source = _resolve_node(
            G, repo=bridge["source_repo"], symbol=bridge["source"]
        )
        target = _resolve_node(
            G, repo=bridge["target_repo"], symbol=bridge["target"]
        )
        if source == target:
            raise ValueError(f"bridge resolves to a self-loop: {source}")
        G.add_edge(
            source,
            target,
            relation=bridge["relation"],
            confidence=bridge["confidence"],
            confidence_score=bridge["confidence_score"],
            source_file=source_file,
            source_location=None,
            weight=1.0,
            cross_service=True,
            _src=source,
            _tgt=target,
        )
        added += 1
    return added


def _normalise_http_path(value: object) -> str:
    path = html.unescape(str(value or "")).strip()
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"/+", "/", "/" + path.strip("/"))
    path = _HTTP_PATH_VARIABLE_RE.sub("{}", path)
    return path.rstrip("/") or "/"


def _java_http_routes(data: dict) -> tuple[str, list[tuple[str, str]]]:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return "", []
    role = str(metadata.get("java_http_role", "")).strip().casefold()
    raw_routes = metadata.get("java_http_routes")
    if role not in {"inbound", "outbound"} or not isinstance(raw_routes, list):
        return "", []
    routes: list[tuple[str, str]] = []
    for raw in raw_routes:
        if not isinstance(raw, dict):
            continue
        method = str(raw.get("method", "*")).strip().upper() or "*"
        path = _normalise_http_path(raw.get("path"))
        routes.append((method, path))
    return role, list(dict.fromkeys(routes))


def infer_java_http_route_bridges(G: nx.Graph) -> int:
    """Link unique outbound Java HTTP methods to matching controller handlers.

    Matching is deterministic and local: both nodes must carry Java annotation
    metadata extracted from source, their normalized paths must match, HTTP verbs
    must agree (``RequestMapping`` without a method is a wildcard), and the target
    must be unique in another repository. Path-variable names are deliberately
    ignored, so ``/soc/{id}`` and ``/soc/{socId}`` describe the same endpoint.
    """
    inbound_by_path: dict[str, list[tuple[str, str, str]]] = {}
    outbound: list[tuple[str, str, list[tuple[str, str]]]] = []
    for node_id, data in G.nodes(data=True):
        repo = str(data.get("repo", "")).strip()
        if not repo:
            continue
        role, routes = _java_http_routes(data)
        if role == "inbound":
            for method, path in routes:
                inbound_by_path.setdefault(path, []).append((str(node_id), repo, method))
        elif role == "outbound":
            outbound.append((str(node_id), repo, routes))

    added = 0
    for source, source_repo, routes in outbound:
        candidates: dict[str, tuple[str, str]] = {}
        for source_method, path in routes:
            for target, target_repo, target_method in inbound_by_path.get(path, []):
                if target_repo == source_repo:
                    continue
                if (
                    source_method != "*"
                    and target_method != "*"
                    and source_method != target_method
                ):
                    continue
                method = source_method if source_method != "*" else target_method
                candidates[target] = (method, path)
        if len(candidates) != 1:
            continue
        target, (method, path) = next(iter(candidates.items()))
        if G.has_edge(source, target):
            continue
        G.add_edge(
            source,
            target,
            relation="calls",
            confidence="INFERRED",
            confidence_score=0.95,
            source_file="graphify:auto-java-http-route-bridge",
            source_location=None,
            weight=1.0,
            cross_service=True,
            bridge_strategy="java_http_route",
            http_method=method,
            http_route=path,
            _src=source,
            _tgt=target,
        )
        added += 1
    return added


def infer_java_service_bridges(G: nx.Graph) -> int:
    """Connect unambiguous ``*Client`` classes to matching ``*Controller`` classes.

    Per-service AST extraction cannot observe the network hop between repositories.
    Java backend projects commonly express that hop with an outbound client and an
    inbound controller sharing the same domain stem (for example ``PaymentClient``
    and ``PaymentController``). Only a unique cross-repository match is accepted;
    ambiguous names are left disconnected rather than guessed.
    """
    controllers: dict[str, list[tuple[str, str]]] = {}
    clients: list[tuple[str, str, str]] = []
    for node_id, data in G.nodes(data=True):
        label = str(data.get("label", "")).strip()
        repo = str(data.get("repo", "")).strip()
        if not label or not repo:
            continue
        folded = label.casefold()
        if folded.endswith(_JAVA_CONTROLLER_SUFFIX):
            stem = folded[:-len(_JAVA_CONTROLLER_SUFFIX)].strip()
            if stem:
                controllers.setdefault(stem, []).append((str(node_id), repo))
        elif folded.endswith(_JAVA_CLIENT_SUFFIX):
            stem = folded[:-len(_JAVA_CLIENT_SUFFIX)].strip()
            if stem:
                clients.append((str(node_id), repo, stem))

    added = 0
    for source, source_repo, stem in clients:
        matches = [
            (target, target_repo)
            for target, target_repo in controllers.get(stem, [])
            if target_repo != source_repo
        ]
        if len(matches) != 1:
            continue
        target, _target_repo = matches[0]
        if G.has_edge(source, target):
            continue
        G.add_edge(
            source,
            target,
            relation="calls",
            confidence="INFERRED",
            confidence_score=0.9,
            source_file="graphify:auto-java-service-bridge",
            source_location=None,
            weight=1.0,
            cross_service=True,
            bridge_strategy="java_client_controller_name",
            _src=source,
            _tgt=target,
        )
        added += 1
    return added
