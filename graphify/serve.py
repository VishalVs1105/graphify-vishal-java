# MCP stdio server - exposes graph query tools to Claude and other agents
from __future__ import annotations
import html
import json
import math
import os
import re
import sys
from array import array
from collections import OrderedDict
from pathlib import Path
import threading
from typing import NamedTuple
import networkx as nx
from networkx.readwrite import json_graph
from graphify.security import sanitize_label, check_graph_file_size_cap
from graphify.build import edge_data, edge_datas
from graphify.paths import default_graph_json as _default_graph_json

try:
    import jieba as _jieba  # type: ignore[import-untyped]
except ImportError:
    _jieba = None


def _load_graph(graph_path: str) -> nx.Graph:
    try:
        resolved = Path(graph_path).resolve()
        if resolved.suffix != ".json":
            raise ValueError(f"Graph path must be a .json file, got: {graph_path!r}")
        if not resolved.exists():
            raise FileNotFoundError(f"Graph file not found: {resolved}")
        check_graph_file_size_cap(resolved)
        safe = resolved
        data = json.loads(safe.read_text(encoding="utf-8"))
        if "links" not in data and "edges" in data:
            data = dict(data, links=data["edges"])
        data = {**data, "directed": True}
        try:
            from graphify.build import graph_has_legacy_ids as _legacy
            if _legacy(data.get("nodes", [])):
                print(
                    "[graphify] note: this graph uses the pre-#1504 node-ID scheme; "
                    "rebuild with `graphify extract --force` for path-qualified IDs.",
                    file=sys.stderr,
                )
        except Exception:
            pass
        try:
            G = json_graph.node_link_graph(data, edges="links")
        except TypeError:
            G = json_graph.node_link_graph(data)
        # Attach the work-memory overlay (derived sidecar next to graph.json) so
        # the query/MCP read surface can annotate NODE lines display-only. Empty
        # when no sidecar exists, leaving un-annotated output byte-identical.
        try:
            from graphify.reflect import load_learning_overlay as _llo
            G.graph["_learning_overlay"] = _llo(resolved)
        except Exception:
            G.graph["_learning_overlay"] = {}
        return G
    except json.JSONDecodeError as exc:
        print(f"error: graph.json is corrupted ({exc}). Re-run /graphify to rebuild.", file=sys.stderr)
        sys.exit(1)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def _communities_from_graph(G: nx.Graph) -> dict[int, list[str]]:
    """Reconstruct community dict from community property stored on nodes."""
    communities: dict[int, list[str]] = {}
    for node_id, data in G.nodes(data=True):
        cid = data.get("community")
        if cid is not None:
            communities.setdefault(int(cid), []).append(node_id)
    return communities


def _max_server_contexts() -> int:
    """Return the project-context LRU capacity (default 8, minimum 1).

    ``GRAPHIFY_MAX_CONTEXTS`` overrides the default. Invalid or blank values
    use 8; zero and negative values clamp to 1, since each request needs a
    graph context. The server's configured default graph is pinned separately
    and does not count against this limit.
    """
    raw = os.environ.get("GRAPHIFY_MAX_CONTEXTS", "").strip()
    if not raw:
        return 8
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


class _GraphContextCache:
    """Thread-safe graph contexts: one pinned default plus an LRU of projects."""

    def __init__(self, max_contexts: int):
        self._max_contexts = max_contexts
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._pinned: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _load_entry(self, resolved_path: str, key: tuple[int, int]) -> dict:
        """Build one entry for an already-resolved path and known file key.

        ``_load_graph`` is also used by the CLI, where invalid input terminates
        the process. A client-supplied ``project_path`` must instead become a
        tool error, so the shared MCP server can continue serving other graphs.
        """
        try:
            graph = _load_graph(resolved_path)
        except SystemExit as exc:
            raise RuntimeError(f"could not load graph.json at {resolved_path}") from exc
        # Warm the index before exposing the graph so its first query does not
        # pay the expensive build cost.
        _get_trigram_index(graph)
        communities = _communities_from_graph(graph)
        entry = {
            "key": key,
            "G": graph,
            "communities": communities,
        }
        return entry

    def load(self, resolved_path: str, *, pinned: bool = False) -> tuple[nx.Graph, dict[int, list[str]]]:
        """Return a fresh context, retaining project contexts by LRU order.

        ``resolved_path`` is resolved by the caller, making this method the
        sole owner of file statting and cache-key construction.

        ``pinned=True`` is reserved for the server's configured default graph;
        it remains warm without consuming a project-cache slot.
        """
        with self._lock:
            try:
                stat_result = Path(resolved_path).stat()
            except FileNotFoundError:
                raise FileNotFoundError(f"graph.json not found: {resolved_path}") from None
            key = (stat_result.st_mtime_ns, stat_result.st_size)
            entries = self._pinned if pinned else self._entries
            entry = entries.get(resolved_path)
            if entry is not None and entry["key"] == key:
                if not pinned:
                    self._entries.move_to_end(resolved_path)
                return entry["G"], entry["communities"]

            entry = self._load_entry(resolved_path, key)
            entries[resolved_path] = entry
            if not pinned:
                self._entries.move_to_end(resolved_path)
                while len(self._entries) > self._max_contexts:
                    self._entries.popitem(last=False)
            return entry["G"], entry["communities"]


def _strip_diacritics(text: str | None) -> str:
    import unicodedata
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _search_tokens(text: str) -> list[str]:
    """Split text into word tokens, stripping punctuation and diacritics."""
    return re.findall(r"\w+", _strip_diacritics(str(text)).lower())


def _has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _segment_chinese(text: str) -> list[str]:
    """Segment Chinese text and keep the original term for exact matching."""
    if _jieba is not None:
        segments = [w for w in _jieba.cut(text) if len(w.strip()) > 0]
    else:
        segments = [text[i:i + 2] for i in range(len(text) - 1)] or [text]
    if len(text) > 1 and text not in segments:
        segments.append(text)
    return segments


def _is_searchable(term: str) -> bool:
    """True if term is Chinese, non-English, or an English word longer than 2 chars."""
    if all("a" <= ch <= "z" for ch in term):
        return len(term) > 2
    return True


# Question/filler words dropped from query terms so content words drive BFS
# seeding. Without this, "how does the frontier cache work" seeds on "how"/
# "the"/"work" (which prefix-match prose labels like "Working Principles" at 100x)
# instead of "frontier"/"cache", and lands in the wrong part of the graph. Applied
# to query terms only — node text is never filtered, so a symbol literally named
# `work` stays findable via explain/path. `work`/`works`/`working` are included
# because "how does X work" / "how X works" is the most common question phrasing.
#
# Non-English question words are just as damaging (#1900): in a mostly-English
# code corpus, German "wie"/"funktioniert" are rare, so they get HIGH IDF weight
# and out-seed the actual content noun by orders of magnitude. So this also
# carries a curated German set plus a trimmed French/Spanish/Portuguese/Italian
# set of question/filler words. Diacritics are kept intact (the query tokenizer
# does not NFKD-strip).
#
# Collision tradeoff: a few foreign stopwords are also English content words.
# We include high-German-value ones like "die"/"hat" (the all-stopword fallback
# in _query_terms and the unfiltered find_node path keep an English "die"/"hat"
# query workable), but deliberately OMIT "war"/"bald" (German was/soon) so
# English queries about "war" or "bald" are not clobbered. On the Romance side
# we likewise omit "comment" (FR how), "come" (IT how), "son"/"sin"/"con" (ES),
# and "pour"/"des" (FR) — all too common as English/code terms.
_QUERY_STOPWORDS = frozenset({
    # English
    "how", "what", "why", "when", "where", "which", "who", "whom", "whose",
    "does", "did", "is", "are", "was", "were", "be", "been", "being",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "has", "have", "had", "the", "and", "but", "not", "for", "from", "with",
    "without", "into", "onto", "off", "that", "this", "these", "those", "there",
    "here", "its", "their", "them", "they", "about", "any", "all", "some",
    "work", "works", "working",
    # German (articles/conjunctions/question words/auxiliaries/prepositions)
    "der", "die", "das", "den", "dem", "ein", "eine", "und", "oder", "nicht",
    "wie", "wer", "wann", "wo", "warum", "wieso",
    "welche", "welcher", "welches",
    "ist", "sind", "wird", "wurde", "hat", "haben",
    "kann", "koennen", "können", "soll", "muss", "sich",
    "bei", "mit", "von", "fuer", "für", "ueber", "über", "nach", "aus",
    "gibt", "es",
    "funktioniert", "geaendert", "geändert", "aendert", "ändert",
    # French
    "pourquoi", "quand", "quel", "quelle", "quels", "quelles", "quoi",
    "qui", "que", "est", "sont", "fonctionne", "cette", "dans", "avec", "où",
    # Spanish
    "cómo", "como", "qué", "cuál", "cuáles", "cuándo", "dónde", "donde",
    "porque", "por", "para", "funciona", "está", "están", "hay",
    # Portuguese
    "qual", "quais", "quando", "onde", "são", "estão", "tem", "uma", "não",
    # Italian
    "perché", "cosa", "quale", "quali", "dove", "funziona", "sono", "che",
    "della",
})


def _query_terms(question: str) -> list[str]:
    """Split a query into searchable terms, segmenting Chinese text, then drop
    question/filler words (`_QUERY_STOPWORDS`, English plus common German/
    Romance-language fillers) so content words drive seeding. Falls back to the
    unfiltered terms if the query is all stopwords, so a question like "how does
    it work" or "wie funktioniert das" still seeds on something."""
    terms: list[str] = []
    for raw in question.split():
        if _has_chinese(raw):
            for seg in _segment_chinese(raw.lower().strip()):
                seg = seg.strip()
                if seg and _is_searchable(seg):
                    terms.append(seg)
        else:
            # Strip punctuation without touching Unicode characters (avoid NFKD mangling non-Latin scripts)
            for tok in re.findall(r"\w+", raw.lower()):
                if _is_searchable(tok):
                    terms.append(tok)
    content = [t for t in terms if t not in _QUERY_STOPWORDS]
    return content or terms


_EXACT_MATCH_BONUS = 1000.0
_PREFIX_MATCH_BONUS = 100.0
_SUBSTRING_MATCH_BONUS = 1.0
_SOURCE_MATCH_BONUS = 0.5


def _compute_idf(G: nx.Graph, terms: list[str]) -> dict[str, float]:
    """IDF weights for query terms, cached in G.graph['_idf_cache'].

    Common terms like 'error' or 'exception' that match hundreds of nodes get
    low weights; rare identifiers like 'FooBarService' get high weights.
    Cache is stored on the graph object itself so it auto-invalidates when
    a hot-reload replaces G with a new object.
    """
    cache: dict[str, float] = G.graph.setdefault("_idf_cache", {})
    N = G.number_of_nodes() or 1
    uncached = [t for t in terms if t not in cache]
    if uncached:
        df: dict[str, int] = {t: 0 for t in uncached}
        for _, data in G.nodes(data=True):
            norm_label = (
                data.get("norm_label") or _strip_diacritics(data.get("label") or "")
            ).lower()
            for t in uncached:
                if t in norm_label:
                    df[t] += 1
        for t in uncached:
            cache[t] = math.log(1 + N / (1 + df[t]))
    return {t: cache.get(t, math.log(1 + N)) for t in terms}


def _trigrams(text: str) -> set[str]:
    """Character trigrams of `text`; for <3-char text the whole string is the key."""
    if len(text) < 3:
        return {text} if text else set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def _node_search_text(data: dict, nid: str) -> str:
    """Concatenate every field _score_nodes / _find_node match a query against, so
    one trigram index over this text is a complete candidate generator for both.

    - `norm_label` and `source_file` feed _score_nodes' per-term substring tiers.
    - `label_tokens` (the space-joined token form) feeds _find_node's
      `term in label_tokens` branch, where a multi-word `term` can span a token
      boundary that punctuation hides in `norm_label` (e.g. query "foo bar" matches
      label "foo.bar" only via its tokenized form).
    - `source_tokens` feeds _find_node's exact source-file path lookup, where a
      query like "app/api/example/route.ts" tokenizes to "app api example route ts".
    - `nid` feeds the whole-query `joined == nid_lower` tier.

    NUL separators stop a trigram from spanning two fields (a query never contains
    NUL, so a cross-field trigram can never be a real match).
    """
    norm_label = data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()
    label_tokens = " ".join(_search_tokens(data.get("label") or ""))
    source = (data.get("source_file") or "").lower()
    source_tokens = " ".join(_search_tokens(data.get("source_file") or ""))
    return "\x00".join((norm_label, label_tokens, str(nid).lower(), source, source_tokens))


def _get_trigram_index(G: nx.Graph) -> dict:
    """Lazily build and cache a trigram -> node-position postings map on the graph.

    Cached on `G.graph` so it auto-invalidates when a hot-reload swaps in a
    fresh graph object, exactly like `_idf_cache`. `set_cache` memoizes per-trigram
    id-sets across queries within one graph generation.
    """
    idx = G.graph.get("_trigram_index")
    if idx is not None:
        return idx
    ids = list(G.nodes())
    postings: dict[str, array] = {}
    for i, nid in enumerate(ids):
        for g in _trigrams(_node_search_text(G.nodes[nid], nid)):
            bucket = postings.get(g)
            if bucket is None:
                bucket = array("i")
                postings[g] = bucket
            bucket.append(i)
    idx = {"ids": ids, "postings": postings, "set_cache": {}}
    G.graph["_trigram_index"] = idx
    return idx


def _trigram_candidates(G: nx.Graph, needles: list[str], *, guard_frac: float = 0.10) -> list[str] | None:
    """Node IDs whose text could contain any `needle` as a substring, via the
    trigram index — a *superset* the caller then re-scores with the exact predicates.

    Returns candidates in graph-iteration order (so order-sensitive callers like
    _find_node stay byte-identical to a full scan), or **None** when the index isn't
    worth it — a needle is too short to trigram, or its rarest trigram is still
    common enough that the candidate set would approach the whole graph. The caller
    falls back to the full scan, preserving the never-worse contract. The guard is
    cheap: postings-length lookups only, no set intersection.
    """
    idx = _get_trigram_index(G)
    ids, postings, set_cache = idx["ids"], idx["postings"], idx["set_cache"]
    n = len(ids)
    if n == 0:
        return []
    needles = [s for s in needles if s]
    thresh = int(n * guard_frac)
    for s in needles:
        tgs = _trigrams(s)
        if not tgs or any(len(g) < 3 for g in tgs):
            return None  # too short to trigram-filter
        present = [len(postings[g]) for g in tgs if g in postings]
        if not present:
            continue  # this needle matches nothing — contributes no candidates
        if min(present) > thresh:
            return None  # rarest trigram still too common -> not worth the index
    cand: set[int] = set()
    for s in needles:
        sets: list[set] | None = []
        for g in _trigrams(s):
            bucket = postings.get(g)
            if bucket is None:
                sets = None  # a trigram absent everywhere -> needle matches nothing
                break
            cached = set_cache.get(g)
            if cached is None:
                cached = set(bucket)
                set_cache[g] = cached
            sets.append(cached)
        if not sets:
            continue
        sets.sort(key=len)  # intersect smallest-first
        hit = set(sets[0])
        for other in sets[1:]:
            hit &= other
            if not hit:
                break
        cand |= hit
    return [ids[i] for i in sorted(cand)]


class _QueryScores(NamedTuple):
    """Per-query scoring result, returned by the private `_score_query` helper.

    `ranked` is the existing ordered `(score, node_id)` ranking produced by the
    combined query scorer (the value `_score_nodes` always returned). When the
    caller asks for it via `collect_per_term_seeds=True`, `best_seed_by_term`
    additionally carries the winning node id for each normalized search token —
    the seed `_pick_seeds` would have picked for that token via the now-retired
    per-token `_score_nodes([token])` rescoring pass — computed in the *same*
    per-node traversal so the query path makes exactly one graph scoring pass
    regardless of query length. Empty when `collect_per_term_seeds=False`.
    """
    ranked: list[tuple[float, str]]
    best_seed_by_term: dict[str, str]


def _score_nodes(G: nx.Graph, terms: list[str]) -> list[tuple[float, str]]:
    """Combined query scorer returning the existing ranked `(score, node_id)` list.

    Backwards-compatible thin wrapper around `_score_query` for path, explain,
    tests, and every other caller that only needs the combined ranking. The
    per-term seed metadata computed by `_score_query` (when requested) is
    discarded here so existing callers see no API or runtime-cost change.
    """
    return _score_query(G, terms, collect_per_term_seeds=False).ranked


def _score_query(
    G: nx.Graph, terms: list[str], *, collect_per_term_seeds: bool
) -> _QueryScores:
    """Single-pass combined scorer that optionally also records the best seed
    for each normalized query token.

    The combined ranking is byte-identical to what `_score_nodes` produced
    before the refactor; `_score_nodes` is now a thin wrapper that asks for
    `collect_per_term_seeds=False` and returns only `.ranked`.

    When `collect_per_term_seeds=True`, the per-token singleton winner is
    computed alongside the combined score in the *same* per-node visit (it
    reuses the same `norm_label` / `label_tokens` / `source` already evaluated
    for the combined tier), so `_query_graph_text` can feed `best_seed_by_term`
    straight into `_pick_seeds` and skip the T additional whole-graph rescoring
    passes the old per-token `_score_nodes([token])` loop ran.

    Singleton-winner semantics match the legacy per-token path exactly. The
    score itself mirrors `_score_nodes([token])` with `n_terms == 1` (so the
    coverage term is 1 and the per-token tier is unscaled) plus the broader
    joined-singlet tier (which also checks `label_tokens` and `nid_lower`).
    Tie-break order is (1) highest singleton score, (2) highest graph degree,
    (3) shortest displayed label, (4) lexicographically smallest node id —
    exactly what `max(tied, key=degree)` over a sort by `(-score, label_len,
    nid)` produced in the legacy `_pick_seeds` per-token loop. The combined
    trigram candidate set (needles `norm_terms + [joined]`) is a superset of
    each per-token `[t]` candidate set, so iterating combined candidates
    discovers every non-zero singleton-score node for every term.
    """
    scored: list[tuple[float, str]] = []
    # Dedupe tokens, order-preserving (as _pick_seeds already does): a repeated
    # query word must not double-count every tier, and with coverage scaling
    # below it would also inflate the matched-term ratio (#1602).
    norm_terms = list(dict.fromkeys(tok for t in terms for tok in _search_tokens(t)))
    n_terms = len(norm_terms)
    idf = _compute_idf(G, norm_terms)
    # Keep the caller's punctuation-preserving form as well as its tokenized
    # form. Merged node IDs contain repo prefixes, ``::``, and underscores;
    # tokenization turns those into spaces, so the documented "retry with the
    # exact node ID" path could not actually select the pasted ID.
    raw_query = " ".join(str(t).strip() for t in terms).strip().casefold()
    # Whole-query string for full-label matching (mirrors _find_node's `term`).
    joined = " ".join(norm_terms)
    # Weight the full-query bonus by the rarest constituent term so a specific
    # multi-word label still outweighs common-token noise; floor at 1.0.
    joined_w = max((idf.get(t, 1.0) for t in norm_terms), default=1.0)
    # Trigram prefilter: score only nodes whose text could match a term, falling
    # back to the whole graph when the index isn't selective. The result is
    # identical either way — the per-node scoring below is unchanged and a
    # non-candidate node always scores 0. (IDF above stays a whole-graph statistic.)
    candidate_ids = _trigram_candidates(
        G,
        norm_terms
        + ([joined] if joined else [])
        + ([raw_query] if raw_query and raw_query != joined else []),
    )
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    # Per-token best tracking, only when the caller (the query path) wants the
    # seed metadata. The key tuple is the full multi-key tie-break
    # (`(-singleton_score, -degree, label_len, nid)`), so `min` over the
    # stored key mirrors the legacy `max(tied, key=degree)` over a
    # (-score, label_len, nid)-sorted term_scored list. `None` is comparable
    # as "smaller" than every tuple, so the first non-zero candidate seeds the
    # entry without a separate `if t not in best_by_term` branch.
    best_by_term: dict[str, tuple[tuple, str]] | None = (
        {} if collect_per_term_seeds else None
    )
    for nid, data in node_iter:
        norm_label = data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        # Tokenized form of the label (punctuation stripped, same transform as the
        # query). norm_label may still carry punctuation like ':' or '-', which a
        # tokenized query can never equal; comparing token-joined forms on both
        # sides makes "uoce: dehumidifier driver" match query "uoce dehumidifier
        # driver".
        label_tokens = " ".join(_search_tokens(data.get("label") or ""))
        source = (data.get("source_file") or "").lower()
        # `nid_lower` is needed both by the full-query tier (`if joined`) and by
        # the per-token singleton tier (joined-singlet exact-match check). When
        # neither runs (`joined` empty AND not collecting seeds) skip the call;
        # this preserves the single-query-time perf where nid_lower was lazy.
        nid_lower = nid.lower() if (joined or collect_per_term_seeds) else ""
        score = 0.0
        # Full-query tier: a multi-word query that equals (or prefixes) the whole
        # label must dominate the per-token bag-of-words sums below, so `path`/
        # `query` resolve the same node `explain` does (via _find_node). Without
        # this, no single token equals a multi-word label, the per-token exact
        # tier never fires, and every node sharing the token set ties -> arbitrary
        # node-id sort -> wrong/disconnected endpoint -> false "No path found".
        if joined:
            if raw_query == nid_lower:
                score += _EXACT_MATCH_BONUS * 100 * joined_w
            elif joined in (norm_label, bare_label, label_tokens, nid_lower):
                score += _EXACT_MATCH_BONUS * 10 * joined_w
            elif (
                norm_label.startswith(joined)
                or bare_label.startswith(joined)
                or label_tokens.startswith(joined)
            ):
                score += _PREFIX_MATCH_BONUS * 10 * joined_w
        # Term coverage (#1602): scale the per-term exact/prefix tiers by the
        # squared fraction of query terms the node's LABEL matches, so a lone
        # generic word that happens to equal a short label (query term "home"
        # vs. a home() leaf) cannot bury nodes that match several of the
        # query's terms. Squaring matters because the exact tier is 10x the
        # prefix tier: at linear coverage a 1-of-10-terms exact match still
        # outscores a 3-of-10 prefix+substring match. Single-term and
        # full-coverage queries are unchanged (coverage == 1), so identifier
        # lookups keep exact-match dominance. Source-file hits score but do
        # not count as coverage: a colliding leaf whose directory shares
        # tokens with the query (common near the intended target) must not
        # win back its exact tier via path fragments. The substring/source
        # bonuses and the full-query tier above stay unscaled.
        matched = 0
        tiered = 0.0
        for t in norm_terms:
            w = idf.get(t, 1.0)
            # Per-tier contributions for this token, kept separate so the
            # singleton tracking below can reuse them without re-evaluating
            # the same predicates. Three-tier precedence: exact > prefix >
            # substring (take the strongest tier per term so a single term
            # cannot double-count).
            tier_value = 0.0
            substr_value = 0.0
            source_value = 0.0
            if t == norm_label or t == bare_label:
                tier_value = _EXACT_MATCH_BONUS * w
                matched += 1
            elif norm_label.startswith(t) or bare_label.startswith(t):
                tier_value = _PREFIX_MATCH_BONUS * w
                matched += 1
            elif t in norm_label:
                substr_value = _SUBSTRING_MATCH_BONUS * w
                score += substr_value
                matched += 1
            if t in source:
                source_value = _SOURCE_MATCH_BONUS * w
                score += source_value
            tiered += tier_value
            if collect_per_term_seeds and best_by_term is not None:
                # Singleton score for [t] on this node, mirroring
                # `_score_nodes(G, [t])` exactly (n_terms == 1, no coverage
                # scaling). The joined-singlet tier is broader than the per-
                # token tier: it also checks `label_tokens` and `nid_lower`,
                # matching the legacy single-token `_score_nodes([t])` call
                # (where `joined == t`).
                if t in (norm_label, bare_label, label_tokens, nid_lower):
                    singleton = _EXACT_MATCH_BONUS * 10 * w
                elif (
                    norm_label.startswith(t)
                    or bare_label.startswith(t)
                    or label_tokens.startswith(t)
                ):
                    singleton = _PREFIX_MATCH_BONUS * 10 * w
                else:
                    singleton = 0.0
                singleton += tier_value + substr_value + source_value
                if singleton > 0:
                    # Tie-break key mirrors the legacy sort+max(degree):
                    # (-singleton, -degree, label_len, nid) — the minimum
                    # tuple wins, exactly matching max(tied, key=degree)
                    # over (label_len asc, nid asc)-sorted ties.
                    key = (-singleton, -G.degree(nid), len(data.get("label") or nid), nid)
                    cur = best_by_term.get(t)
                    if cur is None or key < cur[0]:
                        best_by_term[t] = (key, nid)
        if tiered:
            score += tiered * (matched / n_terms) ** 2
        if score > 0:
            scored.append((score, nid))
    # Sort by score desc; break ties toward the shorter label so a concise exact
    # match beats a longer superset that happens to share the same score.
    scored.sort(key=lambda s: (-s[0], len(G.nodes[s[1]].get("label") or s[1]), s[1]))
    best_seed_by_term: dict[str, str] = {}
    if collect_per_term_seeds and best_by_term:
        best_seed_by_term = {t: nid for t, (_key, nid) in best_by_term.items()}
    return _QueryScores(ranked=scored, best_seed_by_term=best_seed_by_term)


def _pick_scored_endpoint(G: nx.Graph, scored: list[tuple[float, str]], query: str) -> str:
    """Pick a path endpoint from a _score_nodes result, preferring full-token matches.

    The full-query tier in _score_nodes only fires when the query equals or
    prefixes a label, so a query that is a token *subset* of the intended label
    (query "Reject-everything judge" vs. label "Degenerate Reject-Everything
    Judge") gets no bonus, and a node prefix-matching one rare token (label
    "Rejection Summary") can out-score it on IDF alone. Committing to scored[0]
    then anchors the path on an unrelated — often disconnected — node and yields
    a false "No path found". Scan the score-ordered list and take the first
    candidate whose label contains EVERY query token; when the top candidate
    already full-matches, or no candidate does, this is exactly scored[0].

    `scored` must be non-empty (both callers return early on no match).
    """
    qtokens = set(_search_tokens(query))
    if not qtokens:
        return scored[0][1]
    for _score, nid in scored:
        if qtokens <= set(_search_tokens(G.nodes[nid].get("label") or nid)):
            return nid
    return scored[0][1]


def _pick_seeds(
    scored: list[tuple[float, str]],
    max_k: int = 3,
    gap_ratio: float = 0.2,
    *,
    G: "nx.Graph | None" = None,
    best_seed_by_term: dict[str, str] | None = None,
) -> list[str]:
    """Select BFS seed nodes, stopping when score drops too far below the top.

    Prevents high-frequency noise terms (error, exception) from stealing seed
    slots from a dominant identifier match. When FooBarService scores 1000 and
    error nodes score 1.0, only FooBarService is seeded — the score gap is 99.9%
    which is well above the 20% threshold that would allow additional seeds.

    That same gap_ratio cutoff has a failure mode on multi-term natural-language
    queries: if one term happens to hit an EXACT label match on a node that is
    otherwise unrelated to the query's intent (e.g. a common word that is also
    used as an unrelated identifier or field name elsewhere in the corpus), it
    can outscore every SUBSTRING match on the query's other, actually-relevant
    terms by ~1000x (see `_EXACT_MATCH_BONUS` vs. `_SUBSTRING_MATCH_BONUS`).
    The 20%-gap cutoff then silently discards all of those substring-tier
    seeds, so the BFS traversal only ever explores the neighborhood of the one
    unrelated exact match — see #1445.

    When `G` and `best_seed_by_term` are supplied, this guarantees at least one
    seed per distinct query term that has any match at all, so one term's
    incidental collision cannot starve out the others. The per-token winners
    in `best_seed_by_term` are precomputed by `_score_query` (during the same
    traversal that produced `scored`) so this function no longer rescores the
    graph per term — see #1445 and the `_score_query` docstring.

    Coverage scaling in _score_nodes (#1602) now dampens a lone collision's
    exact tier on multi-term queries, which brings label-matching relevant
    nodes back inside the gap window; this per-term guarantee remains
    load-bearing for relevant nodes matched only via substrings, whose flat
    scores a dampened collision can still exceed.
    """
    if not scored:
        return []

    # Deduplicate seeds by (normalized) label so a generic, homonymous symbol —
    # e.g. dozens of route handlers all labelled `GET`/`POST`, or a `handler`
    # repeated across a framework — contributes at most one seed instead of
    # consuming every slot and flooding the BFS with near-identical neighborhoods
    # (#1766). The key mirrors _score_nodes' normalization so `GET`/`Get`/`get`
    # collapse together. When G is absent we can't read labels, so fall back to
    # the (unique) node id, which is a no-op — preserving the old behavior.
    def _seed_label_key(nid: str) -> str:
        if G is None:
            return nid
        data = G.nodes[nid]
        return (data.get("norm_label")
                or _strip_diacritics(data.get("label") or "").lower()) or nid

    top_score = scored[0][0]
    seeds: list[str] = []
    seen_labels: set[str] = set()
    for score, nid in scored:
        if len(seeds) >= max_k:
            break
        if seeds and score < top_score * gap_ratio:
            break
        key = _seed_label_key(nid)
        if key in seen_labels:
            continue
        seen_labels.add(key)
        seeds.append(nid)

    if G is not None and best_seed_by_term:
        # Guarantee one seed per distinct query term that has any match at all,
        # so an incidental exact match on one term cannot starve matches on
        # other terms (#1445). Iterate tokens in a deterministic sorted order
        # so seeds added by this loop have a stable order independent of dict
        # iteration — preserving the legacy `_pick_seeds(terms=...)` behavior
        # which iterated `sorted({tok ...})`. Per-token winners arrive
        # precomputed in `best_seed_by_term` from `_score_query`'s single
        # traversal, so `_pick_seeds` no longer rescoring the graph per term.
        # The per-label dedup cap also gates these additions, so the guarantee
        # cannot reintroduce a second copy of an already-seeded generic label
        # (#1766).
        for term in sorted(best_seed_by_term):
            best_nid = best_seed_by_term[term]
            # Honor the same per-label cap so the per-term guarantee can't
            # reintroduce a second copy of an already-seeded generic label.
            key = _seed_label_key(best_nid)
            if best_nid not in seeds and key not in seen_labels:
                seen_labels.add(key)
                seeds.append(best_nid)
    return seeds


_CONTEXT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("call", ("call", "calls", "called", "invoke", "invokes", "invoked")),
    ("import", ("import", "imports", "imported", "module", "modules")),
    ("field", ("field", "fields", "member", "members", "property", "properties")),
    ("parameter_type", ("parameter", "parameters", "param", "params", "argument", "arguments")),
    ("return_type", ("return", "returns", "returned")),
    ("generic_arg", ("generic", "generics", "template", "templates")),
)


_CONTEXT_FILTER_ALIASES: dict[str, str] = {
    "param": "parameter_type",
    "params": "parameter_type",
    "parameter": "parameter_type",
    "parameters": "parameter_type",
    "argument": "parameter_type",
    "arguments": "parameter_type",
    "arg": "parameter_type",
    "args": "parameter_type",
    "return": "return_type",
    "returns": "return_type",
    "returned": "return_type",
    "generic": "generic_arg",
    "generics": "generic_arg",
    "template": "generic_arg",
    "templates": "generic_arg",
    "annotation": "attribute",
    "annotations": "attribute",
    "decorator": "attribute",
    "decorators": "attribute",
    "calls": "call",
    "called": "call",
    "invoke": "call",
    "invocation": "call",
    "fields": "field",
    "property": "field",
    "properties": "field",
    "member": "field",
    "members": "field",
    "imports": "import",
    "imported": "import",
    "module": "import",
    "modules": "import",
    "exports": "export",
    "exported": "export",
}


def _normalize_context_filters(filters: list[str] | None) -> list[str]:
    if not filters:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in filters:
        key = _strip_diacritics(str(value)).strip().lower()
        if not key:
            continue
        key = _CONTEXT_FILTER_ALIASES.get(key, key)
        if key not in seen:
            seen.add(key)
            normalized.append(key)
    return normalized


def _infer_context_filters(question: str) -> list[str]:
    lowered = {
        _strip_diacritics(token).lower()
        for token in question.replace("?", " ").replace(",", " ").split()
    }
    inferred: list[str] = []
    for context, hints in _CONTEXT_HINTS:
        if any(hint in lowered for hint in hints):
            inferred.append(context)
    return inferred


def _resolve_context_filters(question: str, explicit_filters: list[str] | None = None) -> tuple[list[str], str | None]:
    normalized = _normalize_context_filters(explicit_filters)
    if normalized:
        return normalized, "explicit"
    inferred = _infer_context_filters(question)
    if inferred:
        return inferred, "heuristic"
    return [], None


def _filter_graph_by_context(G: nx.Graph, context_filters: list[str] | None) -> nx.Graph:
    filters = set(_normalize_context_filters(context_filters))
    if not filters:
        return G
    H = G.__class__()
    H.add_nodes_from(G.nodes(data=True))
    if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)):
        for u, v, key, data in G.edges(keys=True, data=True):
            if data.get("context") in filters:
                H.add_edge(u, v, key=key, **data)
    else:
        for u, v, data in G.edges(data=True):
            if data.get("context") in filters:
                H.add_edge(u, v, **data)
    return H


def _complete_induced_edges(G: nx.Graph, visited: set[str], edges_seen: list[tuple]) -> None:
    """Append edges between visited nodes that the traversal never recorded (#2323).

    Both traversals only record an edge that *discovers* an unvisited neighbour,
    so what they return is a traversal tree, not the induced subgraph over the
    nodes they return. `_bfs` marks every seed visited up front, so an edge
    between two seeds can never be recorded — the reported symptom, where both
    endpoints render and the edge between them does not. It drops ordinary
    cross-edges for the same reason. `_dfs` appends on push rather than on
    visit, so it already captured those; its one gap is an edge between two
    non-seed hubs, since neither endpoint is ever expanded.

    Scans only edges incident to `visited`, so cost tracks the subgraph rather
    than the whole graph, bounded by O(2E) overall. A visited hub is rescanned
    in full even though the traversal deliberately did not expand it — that is
    unavoidable, since a hub-to-hub edge is exactly the case `_dfs` misses.
    `G` here is the context-filtered `traversal_graph` (see
    `_query_graph_text`), so a filtered-out relation cannot reappear.

    Self-loops are skipped. A recursive function legitimately carries one, but
    neither traversal has ever recorded one (`n` is always already visited when
    its own self-loop is examined), and surfacing them is a separate output
    change from the missing edges reported here.

    Dedup keys on the ordered pair for directed graphs and the unordered pair
    otherwise: on a DiGraph `u->v` and `v->u` are genuinely distinct edges
    (mutual recursion, circular imports), and collapsing them would drop a real
    one. On a multigraph parallel edges collapse to one entry, matching the
    renderer, which already shows only the first (`_subgraph_to_text`).

    Traversal edges keep their discovery order; completions are appended after.
    """
    directed = G.is_directed()

    def _key(u: str, v: str):
        return (u, v) if directed else frozenset((u, v))

    seen = {_key(u, v) for u, v in edges_seen}
    # sorted() so the appended order can't shift run-to-run with CPython's
    # per-process string-hash seed, the same reason the renderer sorts (#1753).
    for u, v in G.edges(sorted(visited)):
        if u == v or v not in visited:
            continue
        key = _key(u, v)
        if key in seen:
            continue
        seen.add(key)
        edges_seen.append((u, v))


def _bfs(G: nx.Graph, start_nodes: list[str], depth: int) -> tuple[set[str], list[tuple]]:
    # Compute hub threshold: nodes above this degree are not expanded as transit.
    # p99 of degree distribution, floored at 50 to avoid over-blocking small graphs.
    degrees = [G.degree(n) for n in G.nodes()]
    if degrees:
        degrees_sorted = sorted(degrees)
        p99_idx = int(len(degrees_sorted) * 0.99)
        hub_threshold = max(50, degrees_sorted[p99_idx])
    else:
        hub_threshold = 50
    seed_set = set(start_nodes)
    visited: set[str] = set(start_nodes)
    frontier = set(start_nodes)
    edges_seen: list[tuple] = []
    for _ in range(depth):
        next_frontier: set[str] = set()
        for n in frontier:
            # Don't expand through high-degree hubs (except seeds - a hub that
            # is the starting node should still be explored).
            if n not in seed_set and G.degree(n) >= hub_threshold:
                continue
            for neighbor in G.neighbors(n):
                if neighbor not in visited:
                    next_frontier.add(neighbor)
                    edges_seen.append((n, neighbor))
        visited.update(next_frontier)
        frontier = next_frontier
    _complete_induced_edges(G, visited, edges_seen)
    return visited, edges_seen


def _dfs(G: nx.Graph, start_nodes: list[str], depth: int) -> tuple[set[str], list[tuple]]:
    degrees = [G.degree(n) for n in G.nodes()]
    if degrees:
        degrees_sorted = sorted(degrees)
        p99_idx = int(len(degrees_sorted) * 0.99)
        hub_threshold = max(50, degrees_sorted[p99_idx])
    else:
        hub_threshold = 50
    seed_set = set(start_nodes)
    visited: set[str] = set()
    edges_seen: list[tuple] = []
    stack = [(n, 0) for n in reversed(start_nodes)]
    while stack:
        node, d = stack.pop()
        if node in visited or d > depth:
            continue
        visited.add(node)
        if node not in seed_set and G.degree(node) >= hub_threshold:
            continue
        for neighbor in G.neighbors(node):
            if neighbor not in visited:
                stack.append((neighbor, d + 1))
                edges_seen.append((node, neighbor))
    _complete_induced_edges(G, visited, edges_seen)
    return visited, edges_seen


def _subgraph_to_text(G: nx.Graph, nodes: set[str], edges: list[tuple], token_budget: int = 2000, *, seeds: list[str] | None = None) -> str:
    """Render subgraph as text, cutting at token_budget (approx 3 chars/token).

    seeds: exact-match nodes rendered first before the degree-sorted expansion,
    so the queried symbol always appears at the top of the output.
    """
    char_budget = token_budget * 3
    lines = []
    # Work-memory overlay (derived sidecar) stashed on the graph at load time.
    # Empty when no sidecar exists, so un-annotated output stays byte-identical.
    overlay = getattr(G, "graph", {}).get("_learning_overlay", {}) or {}
    seed_set = set(seeds or [])
    seed_hits = [n for n in (seeds or []) if n in nodes]
    # Rank non-seed nodes by hop distance from the seeds so the node that answers
    # the query (a direct hit or its close neighbors) survives the budget cut
    # instead of being pushed past it by incidental high-degree hubs (#BUG2). BFS
    # discovery order was discarded upstream (_bfs returns a set), so recompute
    # layers here over BOTH edge directions. Deterministic: neighbor iteration is
    # insertion-ordered and the sort key ends in str(n) (no hash-order).
    def _adj(n):
        if G.is_directed():
            yield from G.successors(n)
            yield from G.predecessors(n)
        else:
            yield from G.neighbors(n)
    dist: dict[str, int] = {n: 0 for n in seed_hits}
    frontier, hop = seed_hits, 0
    while frontier:
        hop += 1
        nxt = []
        for n in frontier:
            for nb in _adj(n):
                if nb in nodes and nb not in dist:
                    dist[nb] = hop
                    nxt.append(nb)
        frontier = nxt
    ordered = seed_hits + sorted(
        nodes - seed_set,
        key=lambda n: (dist.get(n, 1 << 30), -G.degree(n), str(n)),
    )
    for nid in ordered:
        d = G.nodes[nid]
        # Every LLM-derived field passes through sanitize_label before being
        # concatenated into MCP tool output (F-010): an attacker who controls a
        # corpus document can otherwise inject ANSI escapes, fake graphify-out
        # log lines, or prompt-injection markup into the model's context via
        # source_file / source_location / community.
        # The learning= suffix is appended INSIDE the bracket and BEFORE the
        # budget check below, so it counts in char_budget accounting.
        entry = overlay.get(str(nid))
        learning_suffix = ""
        if entry:
            status = sanitize_label(str(entry.get("status", "")))
            if status:
                learning_suffix = f" learning={status}{':stale' if entry.get('stale') else ''}"
        line = (
            f"NODE {sanitize_label(d.get('label', nid))} "
            f"[src={sanitize_label(str(d.get('source_file', '')))} "
            f"loc={sanitize_label(str(d.get('source_location', '')))} "
            f"community={sanitize_label(str(d.get('community_name') or d.get('community', '')))}"
            f"{learning_suffix}]"
        )
        lines.append(line)
    for u, v in edges:
        if u in nodes and v in nodes:
            raw = G[u][v]
            d = next(iter(raw.values()), {}) if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)) else raw
            # (u, v) is BFS/DFS visit order, not necessarily the true edge
            # direction: on an undirected graph G.neighbors() walks callers
            # and callees alike, so a caller->callee edge renders backwards
            # whenever the callee is visited first. _src/_tgt (stashed on the
            # edge data by the `query` CLI loader) carry the real direction;
            # fall back to (u, v) for graphs/edges that don't set them.
            src = d.get("_src", u)
            tgt = d.get("_tgt", v)
            # Guard against a stray/dangling _src/_tgt (hand-edited or adversarial
            # graph.json): only trust them when they name exactly this edge's
            # endpoints, else fall back to (u, v). Without this, G.nodes[src]
            # would KeyError on an unknown id (#2080 review).
            if {src, tgt} != {u, v}:
                src, tgt = u, v
            context = d.get("context")
            context_suffix = f" context={sanitize_label(str(context))}" if context else ""
            # The relation SITE (call/import/reference line in the source's
            # file), not a def line — so "who calls X" cites a clickable call
            # location, not the caller's def (#BUG1).
            _loc = str(d.get("source_location") or "")
            at_suffix = (
                f" at={sanitize_label(str(d.get('source_file') or ''))}:{sanitize_label(_loc)}"
                if _loc else ""
            )
            line = (
                f"EDGE {sanitize_label(G.nodes[src].get('label', src))} "
                f"--{sanitize_label(str(d.get('relation', '')))} "
                f"[{sanitize_label(str(d.get('confidence', '')))}{context_suffix}]--> "
                f"{sanitize_label(G.nodes[tgt].get('label', tgt))}{at_suffix}"
            )
            lines.append(line)
    output = "\n".join(lines)
    if len(output) > char_budget:
        cut_at = output[:char_budget].rfind("\n")
        cut_at = cut_at if cut_at > 0 else char_budget
        # Never cut the seed nodes: they render first, so if the budget lands
        # inside the seed block, extend the cut to cover it. The symbol the
        # question named must always be in the answer (#BUG2). Seeds are bounded
        # (_pick_seeds max_k + one per term), so the overshoot is a few lines.
        if seed_hits:
            seed_block_end = sum(len(lines[i]) + 1 for i in range(len(seed_hits))) - 1
            cut_at = max(cut_at, min(seed_block_end, len(output)))
        total_nodes = sum(1 for l in lines if l.startswith("NODE "))
        shown_nodes = output[:cut_at].count("\nNODE ") + (1 if output.startswith("NODE ") else 0)
        cut_count = total_nodes - shown_nodes
        # Prominent notice at the TOP so a truncated answer can never be mistaken
        # for a complete one — silence used to read as absence (#BUG2). The
        # notice + end marker sit OUTSIDE char_budget by design (two bounded
        # wrapper lines, like the existing end marker).
        output = (
            f"[!] TRUNCATED: showing {shown_nodes} of {total_nodes} nodes "
            f"(~{token_budget}-token budget). The answer may be among the "
            f"{cut_count} cut nodes — raise the token budget (CLI: --budget) or "
            f"narrow the query (e.g. context_filter=['call'], or get_node for a "
            f"specific symbol).\n\n"
            + output[:cut_at]
            + f"\n... (truncated — {cut_count} more nodes cut by ~{token_budget}-token budget."
            f" Narrow with context_filter=['call'] or use get_node for a specific symbol)"
        )
    return output


def _cut_lines_to_budget(lines: list[str], token_budget: int, narrow_hint: str) -> str:
    """Render pre-built lines under the same ~3-chars/token budget rule as
    _subgraph_to_text; over-budget output is cut at a line boundary with a count and a
    narrowing hint instead of flooding the caller's context window."""
    output = "\n".join(lines)
    char_budget = token_budget * 3
    if len(output) <= char_budget:
        return output
    cut_at = output[:char_budget].rfind("\n")
    cut_at = cut_at if cut_at > 0 else char_budget
    kept = output[:cut_at]
    shown = kept.count("\n") + 1
    cut_count = len(lines) - shown
    # Announce truncation at the TOP as well, matching _subgraph_to_text — a
    # bottom-only marker reads as silence/absence (the BUG-2 fix rationale). The
    # notice sits outside char_budget by design (one bounded wrapper line).
    return (
        f"[!] TRUNCATED: showing {shown} of {len(lines)} lines "
        f"(~{token_budget}-token budget). {narrow_hint}\n\n"
        + kept
        + f"\n... (truncated — {cut_count} more lines cut by ~{token_budget}-token budget. "
        + narrow_hint
        + ")"
    )


_JAVA_FLOW_INTENT_RE = re.compile(r"\b(?:api|endpoint|flow|trace|explain|mapping|route)\b", re.IGNORECASE)
_JAVA_BSA_AUDIENCE_RE = re.compile(
    r"\b(?:bsa|business\s+(?:systems?\s+)?analyst|business\s+level|functional\s+flow|non[- ]technical)\b",
    re.IGNORECASE,
)
_JAVA_HTTP_PATH_RE = re.compile(r"(?<![\w:])(/[A-Za-z0-9._~!$&'()*+,;=:@%{}\-/]+)")
_JAVA_QUALIFIED_METHOD_RE = re.compile(
    r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)(?:\(\))?"
)


def _true_edge_records(G: nx.Graph):
    """Yield persisted source->target direction for simple or multi graphs."""
    if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)):
        raw_edges = (
            (u, v, data)
            for u, v, _key, data in G.edges(keys=True, data=True)
        )
    else:
        raw_edges = G.edges(data=True)
    for u, v, data in raw_edges:
        src = data.get("_src", u)
        tgt = data.get("_tgt", v)
        if {src, tgt} != {u, v}:
            src, tgt = u, v
        yield str(src), str(tgt), data


def _java_method_owners(G: nx.Graph, records: list[tuple[str, str, dict]]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for src, tgt, data in records:
        if data.get("relation") == "method" and src in G and tgt in G:
            owners.setdefault(tgt, src)
    return owners


def _java_is_test_node(G: nx.Graph, node_id: str) -> bool:
    """Return whether a Java graph node comes from non-production test code."""
    source = str(G.nodes[node_id].get("source_file") or "").replace("\\", "/").casefold()
    if not source:
        return False
    if re.search(r"(?:^|/)(?:test|tests|integrationtest|integration-test)(?:/|$)", source):
        return True
    filename = source.rsplit("/", 1)[-1]
    return bool(re.search(r"(?:test|tests|it)\.java$", filename))


def _java_interface_dispatch_records(
    G: nx.Graph,
    records: list[tuple[str, str, dict]],
    owners: dict[str, str],
) -> list[tuple[str, str, dict]]:
    """Derive interface-method -> implementation-method runtime dispatch edges.

    Java call sites normally target the declared field type, so a controller's
    call ends at ``AddonService.method()`` even though Spring invokes
    ``AddonServiceImpl.method()``.  The graph already contains the extracted
    class ``implements`` interface relationship and both method declarations;
    this joins those facts for flow rendering without mutating the graph.
    """
    methods_by_owner_and_label: dict[tuple[str, str], list[str]] = {}
    for method_id, owner_id in owners.items():
        label = str(G.nodes[method_id].get("label") or "")
        methods_by_owner_and_label.setdefault((owner_id, label), []).append(method_id)

    implementing_classes: dict[str, set[str]] = {}
    for src, tgt, data in records:
        if data.get("relation") == "implements" and src in G and tgt in G:
            implementing_classes.setdefault(tgt, set()).add(src)

    derived: list[tuple[str, str, dict]] = []
    for interface_method, interface_owner in owners.items():
        implementations = implementing_classes.get(interface_owner, set())
        if not implementations:
            continue
        method_label = str(G.nodes[interface_method].get("label") or "")
        candidates = sorted({
            method_id
            for class_id in implementations
            for method_id in methods_by_owner_and_label.get((class_id, method_label), [])
            if method_id != interface_method
        })
        interface_metadata = G.nodes[interface_method].get("metadata")
        if isinstance(interface_metadata, dict) and len(candidates) > 1:
            interface_params = tuple(
                _java_display_value(value.get("type"))
                for value in interface_metadata.get("java_parameters") or []
                if isinstance(value, dict)
            )
            signature_matches = []
            for candidate in candidates:
                candidate_metadata = G.nodes[candidate].get("metadata")
                if not isinstance(candidate_metadata, dict):
                    continue
                candidate_params = tuple(
                    _java_display_value(value.get("type"))
                    for value in candidate_metadata.get("java_parameters") or []
                    if isinstance(value, dict)
                )
                if candidate_params == interface_params:
                    signature_matches.append(candidate)
            if signature_matches:
                candidates = signature_matches
        if not candidates:
            continue
        strategy = (
            "java_interface_dispatch"
            if len(candidates) == 1
            else "java_interface_dispatch_branch"
        )
        score = 0.95 if len(candidates) == 1 else 0.75
        for implementation_method in candidates:
            method_data = G.nodes[implementation_method]
            derived.append((
                interface_method,
                implementation_method,
                {
                    "relation": "dispatches_to",
                    "confidence": "INFERRED",
                    "confidence_score": score,
                    "bridge_strategy": strategy,
                    "dispatch_candidates": len(candidates),
                    "source_file": method_data.get("source_file", ""),
                    "source_location": method_data.get("source_location", ""),
                },
            ))
    return derived


def _java_flow_symbol(G: nx.Graph, node_id: str, owners: dict[str, str]) -> str:
    data = G.nodes[node_id]
    label = str(data.get("label") or node_id)
    owner_id = owners.get(node_id)
    if owner_id and label.startswith("."):
        label = f"{G.nodes[owner_id].get('label', owner_id)}{label}"
    repo = str(data.get("repo") or "").strip()
    return f"[{repo}] {label}" if repo else label


def _java_flow_source(G: nx.Graph, node_id: str) -> str:
    data = G.nodes[node_id]
    source = str(data.get("source_file") or "")
    location = str(data.get("source_location") or "")
    return f"{source}:{location}" if source and location else source


def _mentioned_java_repos(G: nx.Graph, question: str) -> set[str]:
    lowered = question.casefold()
    spaced = re.sub(r"[-_]+", " ", lowered)
    repos = {
        str(data.get("repo"))
        for _, data in G.nodes(data=True)
        if data.get("repo")
    }
    return {
        repo
        for repo in repos
        if repo.casefold() in lowered
        or re.sub(r"[-_]+", " ", repo.casefold()) in spaced
    }


def _java_flow_edge_details(data: dict) -> str:
    details = [str(data.get("confidence") or "UNKNOWN")]
    strategy = data.get("bridge_strategy")
    if strategy:
        details.append(f"bridge={strategy}")
    score = data.get("confidence_score")
    if score is not None:
        details.append(f"score={score}")
    candidates = data.get("dispatch_candidates")
    if candidates and int(candidates) > 1:
        details.append(f"possible_implementations={candidates}")
    return "; ".join(details)


def _java_flow_edge_source(data: dict) -> str:
    source = str(data.get("source_file") or "")
    location = str(data.get("source_location") or "")
    return f"{source}:{location}" if source and location else source


def _java_metadata(G: nx.Graph, node_id: str) -> dict:
    metadata = G.nodes[node_id].get("metadata") if node_id in G else None
    return metadata if isinstance(metadata, dict) else {}


def _java_display_value(value: object) -> str:
    return html.unescape(str(value or "")).strip()


def _java_parameter_text(parameter: dict, *, include_types: bool = True) -> str:
    name = _java_display_value(parameter.get("name")) or "unnamed"
    binding = _java_display_value(parameter.get("binding")) or "argument"
    declared_type = _java_display_value(parameter.get("type")) or "unknown"
    external = _java_display_value(parameter.get("external_name"))
    required = parameter.get("required")
    flags = []
    if external and external != name:
        flags.append(f"external={external}")
    if required is not None:
        flags.append("required" if required else "optional")
    if parameter.get("validated"):
        flags.append("validated")
    constraints: list[str] = []
    for constraint in parameter.get("constraints") or []:
        if not isinstance(constraint, dict):
            continue
        constraint_name = _java_display_value(constraint.get("name"))
        values = constraint.get("values")
        arguments: list[str] = []
        if isinstance(values, dict):
            for key, raw_values in values.items():
                if not isinstance(raw_values, list):
                    continue
                arguments.extend(
                    f"{key}={_java_display_value(value)}" for value in raw_values
                )
        constraints.append(
            constraint_name + (f"({', '.join(arguments)})" if arguments else "")
        )
    if constraints:
        flags.append(f"constraints={'+'.join(constraints)}")
    suffix = f" ({', '.join(flags)})" if flags else ""
    if include_types:
        return f"{binding} {name}: {declared_type}{suffix}"
    return f"{binding} {name}{suffix}"


def _java_contract_summary(G: nx.Graph, node_id: str) -> str | None:
    metadata = _java_metadata(G, node_id)
    if "java_parameters" not in metadata and "java_return_type" not in metadata:
        return None
    raw_parameters = metadata.get("java_parameters")
    parameters = [
        _java_parameter_text(value)
        for value in raw_parameters or []
        if isinstance(value, dict)
    ]
    request = "; ".join(parameters) if parameters else "no declared parameters"
    return_type = _java_display_value(metadata.get("java_return_type")) or "unknown"
    response_types = [
        _java_display_value(value)
        for value in metadata.get("java_response_types") or []
        if _java_display_value(value)
    ]
    dto_suffix = f"; response types={', '.join(response_types)}" if response_types else ""
    return f"request=[{request}]; returns={return_type}{dto_suffix}"


def _java_condition_text(condition: dict) -> str:
    expression = _java_display_value(condition.get("expression")) or "unspecified condition"
    resolved = _java_display_value(condition.get("resolved_expression"))
    branch = _java_display_value(condition.get("branch"))
    line = _java_display_value(condition.get("line"))
    if branch == "else":
        text = f"otherwise (not {expression})"
    elif branch == "after_guard":
        text = f"after terminating guard, when not ({expression})"
    elif branch == "after_else_guard":
        text = f"after terminating alternative, when {expression}"
    elif branch in {"then", "while", "for", "for_each"}:
        text = f"when {expression}"
    elif branch == "exception":
        text = f"when handling {expression}"
    else:
        text = f"when {expression}"
    if resolved and resolved != expression:
        text += f"; resolved as {resolved}"
    return f"{text} at {line}" if line else text


def _java_edge_conditions(data: dict) -> list[str]:
    results: list[str] = []
    sites = data.get("call_sites")
    if isinstance(sites, list) and len(sites) > 1:
        for site in sites:
            if not isinstance(site, dict):
                continue
            location = _java_display_value(site.get("source_location"))
            conditions = site.get("conditions")
            if isinstance(conditions, list) and conditions:
                text = " and ".join(
                    _java_condition_text(value)
                    for value in conditions if isinstance(value, dict)
                )
            else:
                text = "unconditional"
            item = f"{text} (call site {location})" if location else text
            if item not in results:
                results.append(item)
        return results
    conditions = data.get("conditions")
    if isinstance(conditions, list):
        for condition in conditions:
            if isinstance(condition, dict):
                text = _java_condition_text(condition)
                if text not in results:
                    results.append(text)
    return results


def _java_substitute_bindings(expression: object, bindings: dict[str, str]) -> str:
    text = html.unescape(str(expression or "")).strip()
    for name in sorted(bindings, key=len, reverse=True):
        text = re.sub(
            rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_$])",
            f"({bindings[name]})",
            text,
        )
    return text


def _java_compare_atom(value: str) -> str:
    atom = value.strip().strip("() ").strip('"\'')
    atom = re.sub(r"^(?:[A-Za-z_$][\w$]*\.)+", "", atom)
    return atom.casefold()


def _java_simple_predicate(expression: str) -> bool | None:
    """Evaluate only safe, deterministic literal/enum predicates."""
    text = expression.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    folded = text.casefold()
    if folded in {"true", "(true)"}:
        return True
    if folded in {"false", "(false)"}:
        return False
    equals = re.fullmatch(r"(.+?)\s*(==|!=|\bis\b)\s*(.+)", text)
    if equals:
        left, operator, right = equals.groups()
        left_atom = _java_compare_atom(left)
        right_atom = _java_compare_atom(right)
        if not left_atom or not right_atom:
            return None
        result = left_atom == right_atom
        return not result if operator == "!=" else result
    method_equals = re.fullmatch(r"(.+?)\.equals\((.+)\)", text)
    if method_equals:
        return _java_compare_atom(method_equals.group(1)) == _java_compare_atom(
            method_equals.group(2)
        )
    negated = re.fullmatch(r"!\s*\((.+)\)|!\s*(.+)", text)
    if negated:
        nested = _java_simple_predicate(negated.group(1) or negated.group(2))
        return None if nested is None else not nested
    return None


def _java_condition_truth(
    condition: dict,
    bindings: dict[str, str],
) -> bool | None:
    expression = _java_substitute_bindings(condition.get("expression"), bindings)
    if expression.casefold().startswith("no explicit "):
        return None
    truth = _java_simple_predicate(expression)
    branch = str(condition.get("branch") or "")
    if truth is not None and branch in {"else", "after_guard"}:
        truth = not truth
    return truth


def _java_edge_sites(data: dict) -> list[dict]:
    sites = data.get("call_sites")
    if isinstance(sites, list) and sites:
        return [site for site in sites if isinstance(site, dict)]
    return [data]


def _java_feasible_edge_data(
    data: dict,
    bindings: dict[str, str],
    *,
    matched_switch_case: bool,
) -> dict | None:
    viable: list[dict] = []
    for site in _java_edge_sites(data):
        conditions = [
            value for value in site.get("conditions") or data.get("conditions") or []
            if isinstance(value, dict)
        ]
        if matched_switch_case and any(
            str(value.get("kind") or "") == "switch"
            and str(value.get("branch") or "") == "else"
            for value in conditions
        ):
            continue
        truths = [_java_condition_truth(value, bindings) for value in conditions]
        if any(value is False for value in truths):
            continue
        enriched_site = dict(site)
        if conditions:
            enriched_conditions: list[dict] = []
            for condition in conditions:
                enriched = dict(condition)
                resolved = _java_substitute_bindings(
                    condition.get("expression"), bindings
                )
                original = html.unescape(
                    str(condition.get("expression") or "")
                ).strip()
                if resolved and resolved != original:
                    enriched["resolved_expression"] = resolved
                enriched_conditions.append(enriched)
            enriched_site["conditions"] = enriched_conditions
        viable.append(enriched_site)
    if not viable:
        return None
    filtered = dict(data)
    if isinstance(data.get("call_sites"), list):
        filtered["call_sites"] = viable
        filtered["occurrence_count"] = len(viable)
    conditions: list[dict] = []
    for site in viable:
        for condition in site.get("conditions") or data.get("conditions") or []:
            if not isinstance(condition, dict):
                continue
            enriched = dict(condition)
            resolved = _java_substitute_bindings(condition.get("expression"), bindings)
            original = html.unescape(str(condition.get("expression") or "")).strip()
            if resolved and resolved != original:
                enriched["resolved_expression"] = resolved
            if enriched not in conditions:
                conditions.append(enriched)
    if conditions:
        filtered["conditions"] = conditions
    else:
        filtered.pop("conditions", None)
    return filtered


def _java_call_argument_sets(data: dict) -> list[list[str]]:
    values: list[list[str]] = []
    for site in _java_edge_sites(data):
        arguments = site.get("arguments") or data.get("arguments")
        if isinstance(arguments, list):
            item = [html.unescape(str(value)) for value in arguments]
            if item not in values:
                values.append(item)
    return values


def _java_target_binding_sets(
    G: nx.Graph,
    source: str,
    target: str,
    data: dict,
    bindings: dict[str, str],
) -> list[dict[str, str]]:
    target_parameters = [
        value for value in _java_metadata(G, target).get("java_parameters") or []
        if isinstance(value, dict) and value.get("name")
    ]
    if not target_parameters:
        return [{}]
    argument_sets = _java_call_argument_sets(data)
    if argument_sets:
        outputs: list[dict[str, str]] = []
        for arguments in argument_sets:
            target_bindings = {
                str(parameter["name"]): _java_substitute_bindings(argument, bindings)
                for parameter, argument in zip(target_parameters, arguments)
            }
            if target_bindings not in outputs:
                outputs.append(target_bindings)
        return outputs or [{}]

    source_parameters = [
        value for value in _java_metadata(G, source).get("java_parameters") or []
        if isinstance(value, dict) and value.get("name")
    ]
    forwarded: dict[str, str] = {}
    for index, parameter in enumerate(target_parameters):
        target_name = str(parameter["name"])
        source_name = (
            str(source_parameters[index]["name"])
            if index < len(source_parameters) else target_name
        )
        if source_name in bindings:
            forwarded[target_name] = bindings[source_name]
    return [forwarded]


def _java_reachable_calls(
    G: nx.Graph,
    endpoint: str,
    outgoing: dict[str, list[tuple[str, dict]]],
    *,
    max_depth: int = 64,
) -> tuple[
    list[tuple[int, str, str, dict]],
    list[str],
    dict[str, list[dict[str, str]]],
]:
    """Return feasible reachable calls while propagating constant arguments."""
    queue: list[tuple[str, int, dict[str, str]]] = [(endpoint, 0, {})]
    expanded: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    nodes: list[str] = [endpoint]
    node_seen = {endpoint}
    bindings_by_node: dict[str, list[dict[str, str]]] = {endpoint: [{}]}
    edges: list[tuple[int, str, str, dict]] = []
    edge_seen: set[tuple[str, str, str, str]] = set()
    while queue:
        source, depth, bindings = queue.pop(0)
        state_key = (source, tuple(sorted(bindings.items())))
        if state_key in expanded or depth >= max_depth:
            continue
        expanded.add(state_key)
        values = sorted(
            outgoing.get(source, []),
            key=lambda item: (
                int((re.search(r"(?:^|:)L?(\d+)", str(item[1].get("source_location") or "")) or [None, 1_000_000_000])[1]),
                str(item[0]),
            ),
        )
        matched_switch_case = any(
            _java_condition_truth(condition, bindings) is True
            for _target, edge_data in values
            for site in _java_edge_sites(edge_data)
            for condition in site.get("conditions") or edge_data.get("conditions") or []
            if isinstance(condition, dict)
            and str(condition.get("kind") or "") == "switch"
            and str(condition.get("branch") or "") == "case"
        )
        for target, data in values:
            feasible_data = _java_feasible_edge_data(
                data,
                bindings,
                matched_switch_case=matched_switch_case,
            )
            if feasible_data is None:
                continue
            key = (
                source, target,
                str(feasible_data.get("relation") or "calls"),
                str(feasible_data.get("bridge_strategy") or ""),
            )
            if key not in edge_seen:
                edge_seen.add(key)
                edges.append((depth + 1, source, target, feasible_data))
            if target not in node_seen:
                node_seen.add(target)
                nodes.append(target)
            for target_bindings in _java_target_binding_sets(
                G, source, target, feasible_data, bindings
            ):
                values_for_target = bindings_by_node.setdefault(target, [])
                if target_bindings not in values_for_target:
                    values_for_target.append(target_bindings)
                target_state = (target, tuple(sorted(target_bindings.items())))
                if target_state not in expanded and len(expanded) + len(queue) < 2048:
                    queue.append((target, depth + 1, target_bindings))
    return edges, nodes, bindings_by_node


def _append_java_developer_evidence(
    lines: list[str],
    G: nx.Graph,
    endpoint: str,
    outgoing: dict[str, list[tuple[str, dict]]],
    owners: dict[str, str],
) -> None:
    """Append exhaustive AST evidence beyond the curated primary-path view."""
    reachable_edges, reachable_nodes, bindings_by_node = _java_reachable_calls(
        G, endpoint, outgoing
    )
    has_enriched_metadata = any(
        "java_parameters" in _java_metadata(G, node_id)
        or "java_decisions" in _java_metadata(G, node_id)
        or "java_outcomes" in _java_metadata(G, node_id)
        for node_id in reachable_nodes
    )
    if not has_enriched_metadata:
        lines.append(
            "Enhanced contracts/conditions: unavailable in this graph; rebuild each "
            "service with this Graphify version and re-merge."
        )
        return
    lines.append("Complete reachable production call inventory:")
    if not reachable_edges:
        lines.append("  (none recorded in the graph)")
    for number, (_depth, source, target, data) in enumerate(reachable_edges, 1):
        evidence = _java_flow_edge_source(data)
        at = f" at={evidence}" if evidence else ""
        relation = str(data.get("relation") or "calls")
        sites = data.get("call_sites")
        count = int(data.get("occurrence_count") or (len(sites) if isinstance(sites, list) else 1))
        count_suffix = f"; occurrences={count}" if count > 1 else ""
        lines.append(
            f"  C{number}. {_java_flow_symbol(G, source, owners)} --{relation} "
            f"[{_java_flow_edge_details(data)}{count_suffix}]--> "
            f"{_java_flow_symbol(G, target, owners)}{at}"
        )
        conditions = _java_edge_conditions(data)
        for condition in conditions:
            lines.append(f"      Condition: {condition}")

    contract_nodes = [
        node_id for node_id in reachable_nodes
        if _java_contract_summary(G, node_id) is not None
    ]
    lines.append("Method request/response contracts:")
    if not contract_nodes:
        lines.append(
            "  (contract metadata absent; rebuild each service graph with this version and re-merge)"
        )
    for number, node_id in enumerate(contract_nodes, 1):
        lines.append(
            f"  M{number}. {_java_flow_symbol(G, node_id, owners)}: "
            f"{_java_contract_summary(G, node_id)}"
        )

    decision_lines: list[str] = []
    outcome_lines: list[str] = []
    nodes_with_reachable_switch_branches = {
        source
        for _depth, source, _target, data in reachable_edges
        if any(
            isinstance(condition, dict)
            and str(condition.get("kind") or "") == "switch"
            for condition in data.get("conditions") or []
        )
    }
    for node_id in reachable_nodes:
        metadata = _java_metadata(G, node_id)
        symbol = _java_flow_symbol(G, node_id, owners)
        for decision in metadata.get("java_decisions") or []:
            if not isinstance(decision, dict):
                continue
            kind = _java_display_value(decision.get("kind"))
            if (
                node_id in nodes_with_reachable_switch_branches
                and kind in {"switch", "case"}
            ):
                continue
            expression = _java_display_value(decision.get("expression"))
            location = _java_display_value(decision.get("line"))
            item = f"{symbol}: {kind} {expression}" + (f" at {location}" if location else "")
            if item not in decision_lines:
                decision_lines.append(item)
        for outcome in metadata.get("java_outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            raw_conditions = [
                value for value in outcome.get("conditions") or []
                if isinstance(value, dict)
            ]
            rendered_conditions = raw_conditions
            if raw_conditions:
                feasible = None
                for state in bindings_by_node.get(node_id, [{}]):
                    matched_switch_case = any(
                        _java_condition_truth(condition, state) is True
                        for _target, edge_data in outgoing.get(node_id, [])
                        for site in _java_edge_sites(edge_data)
                        for condition in site.get("conditions") or edge_data.get("conditions") or []
                        if isinstance(condition, dict)
                        and str(condition.get("kind") or "") == "switch"
                        and str(condition.get("branch") or "") == "case"
                    )
                    feasible = _java_feasible_edge_data(
                        {"conditions": raw_conditions},
                        state,
                        matched_switch_case=matched_switch_case,
                    )
                    if feasible is not None:
                        break
                if feasible is None:
                    continue
                rendered_conditions = [
                    value for value in feasible.get("conditions") or []
                    if isinstance(value, dict)
                ]
            kind = _java_display_value(outcome.get("kind"))
            expression = _java_display_value(outcome.get("expression"))
            location = _java_display_value(outcome.get("line"))
            item = f"{symbol}: {kind} {expression}" + (f" at {location}" if location else "")
            if rendered_conditions:
                item += "; " + " and ".join(
                    _java_condition_text(value)
                    for value in rendered_conditions
                )
            if item not in outcome_lines:
                outcome_lines.append(item)
    lines.append("Decision and outcome logic:")
    if not decision_lines and not outcome_lines:
        lines.append("  (no explicit if/switch/loop/return/throw evidence recorded)")
    for number, item in enumerate(decision_lines, 1):
        lines.append(f"  D{number}. {item}")
    for number, item in enumerate(outcome_lines, 1):
        lines.append(f"  O{number}. {item}")

    unresolved_lines: list[str] = []
    for node_id in reachable_nodes:
        symbol = _java_flow_symbol(G, node_id, owners)
        for unresolved in _java_metadata(G, node_id).get("java_unresolved_calls") or []:
            if not isinstance(unresolved, dict):
                continue
            receiver = _java_display_value(unresolved.get("receiver"))
            callee = _java_display_value(unresolved.get("callee"))
            if (
                str(unresolved.get("call_kind") or "") == "constructor"
                and callee.casefold().endswith(("exception", "error"))
            ):
                continue
            raw_conditions = [
                value for value in unresolved.get("conditions") or []
                if isinstance(value, dict)
            ]
            rendered_conditions = raw_conditions
            if raw_conditions:
                feasible = None
                for state in bindings_by_node.get(node_id, [{}]):
                    matched_switch_case = any(
                        _java_condition_truth(condition, state) is True
                        for _target, edge_data in outgoing.get(node_id, [])
                        for site in _java_edge_sites(edge_data)
                        for condition in site.get("conditions") or edge_data.get("conditions") or []
                        if isinstance(condition, dict)
                        and str(condition.get("kind") or "") == "switch"
                        and str(condition.get("branch") or "") == "case"
                    )
                    feasible = _java_feasible_edge_data(
                        {"conditions": raw_conditions},
                        state,
                        matched_switch_case=matched_switch_case,
                    )
                    if feasible is not None:
                        break
                if feasible is None:
                    continue
                rendered_conditions = [
                    value for value in feasible.get("conditions") or []
                    if isinstance(value, dict)
                ]
            location = _java_display_value(unresolved.get("source_location"))
            arguments = [
                _java_display_value(value)
                for value in unresolved.get("arguments") or []
            ]
            signature = ", ".join(arguments)
            target = (
                f"{receiver}.{callee}({signature})"
                if receiver else f"{callee}({signature})"
            )
            item = f"{symbol}: observed {target} at {location or 'unknown location'}; target unresolved"
            conditions = [
                _java_condition_text(value)
                for value in rendered_conditions
            ]
            if conditions:
                item += "; " + " and ".join(conditions)
            if item not in unresolved_lines:
                unresolved_lines.append(item)
    lines.append("Observed unresolved Java calls:")
    if unresolved_lines:
        for number, item in enumerate(unresolved_lines, 1):
            lines.append(f"  U{number}. {item}")
    else:
        lines.append("  (none in the reachable enriched graph evidence)")


def _java_business_words(value: object) -> str:
    """Turn a Java identifier/expression into deterministic business-readable text."""
    text = html.unescape(str(value or "")).strip()
    text = re.sub(r"\bnew\s+([A-Z][A-Za-z0-9_]*)\s*\([^)]*\)", r"\1 result", text)
    text = re.sub(r"ResponseEntity\.(?:ok|status|created|accepted)\s*\(", "respond with ", text)
    text = re.sub(
        r"\.is([A-Z][A-Za-z0-9_]*)\(\)",
        lambda match: " is " + re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", match.group(1)),
        text,
    )
    text = re.sub(
        r"\.has([A-Z][A-Za-z0-9_]*)\(\)",
        lambda match: " has " + re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", match.group(1)),
        text,
    )
    text = re.sub(r"\.isEmpty\(\)", " is empty", text)
    text = re.sub(r"\.isPresent\(\)", " is present", text)
    text = re.sub(r"\.isValid\(\)", " is valid", text)
    text = re.sub(r"\.equals\(([^)]+)\)", r" equals \1", text)
    text = text.replace("&&", " and ").replace("||", " or ")
    text = text.replace("!=", " is not ").replace("==", " is ")
    text = re.sub(r"!(?!=)\s*", "not ", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = text.replace("_", " ")
    text = re.sub(r"[();{}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:1].upper() + text[1:] if text else ""


def _java_business_action(G: nx.Graph, node_id: str) -> str:
    label = str(G.nodes[node_id].get("label") or "").lstrip(".").removesuffix("()")
    return _java_business_words(label) or "Continue processing"


def _java_business_condition(condition: dict) -> str:
    expression = _java_business_words(
        condition.get("resolved_expression") or condition.get("expression")
    ) or "the condition is met"
    branch = str(condition.get("branch") or "")
    if branch == "else":
        return f"Otherwise, when {expression.casefold()} is not satisfied"
    if branch == "after_guard":
        return f"After the invalid/stop case is rejected, when {expression.casefold()} is not satisfied"
    if branch == "after_else_guard":
        return f"After the alternative stop case, when {expression.casefold()}"
    if branch == "exception":
        return f"When handling {expression.casefold()}"
    return f"When {expression.casefold()}"


def _render_java_business_flow(
    G: nx.Graph,
    endpoint: str,
    *,
    mapping: str | None,
    question: str,
    token_budget: int,
) -> str:
    """Render a BSA-oriented flow without exposing DTO types or Java call chains."""
    records = list(_true_edge_records(G))
    owners = _java_method_owners(G, records)
    def is_executable_method(node_id: str) -> bool:
        if node_id in owners:
            return True
        metadata = _java_metadata(G, node_id)
        return bool(metadata.get("java_http_role"))
    call_records = [
        (source, target, data)
        for source, target, data in records
        if data.get("relation") == "calls"
        and source in G and target in G
        and not _java_is_test_node(G, source)
        and not _java_is_test_node(G, target)
        and _java_flow_source(G, source)
        and _java_flow_source(G, target)
        and is_executable_method(target)
    ]
    from graphify.bridges import derive_java_method_name_bridges
    call_records.extend(derive_java_method_name_bridges(G))
    call_records.extend(_java_interface_dispatch_records(G, records, owners))
    outgoing: dict[str, list[tuple[str, dict]]] = {}
    for source, target, data in call_records:
        outgoing.setdefault(source, []).append((target, data))
    reachable_edges, reachable_nodes, bindings_by_node = _java_reachable_calls(
        G, endpoint, outgoing
    )

    lines = ["BUSINESS API FLOW"]
    if mapping:
        lines.append(f"API: {mapping}")
    endpoint_repo = str(G.nodes[endpoint].get("repo") or "")
    if endpoint_repo:
        lines.append(f"Owning service: {endpoint_repo}")
    lines.append(f"Business capability: {_java_business_action(G, endpoint)}")

    endpoint_metadata = _java_metadata(G, endpoint)
    parameters = [
        value for value in endpoint_metadata.get("java_parameters") or []
        if isinstance(value, dict)
    ]
    lines.append("Business request:")
    if not parameters:
        lines.append("  No explicit request inputs are recorded in the current graph.")
    for parameter in parameters:
        name = _java_business_words(parameter.get("external_name") or parameter.get("name"))
        binding = str(parameter.get("binding") or "argument")
        required = parameter.get("required")
        requirement = "required" if required is not False else "optional"
        validation = " and validated" if parameter.get("validated") else ""
        descriptions = {
            "body": "request body information",
            "path": "URL path value",
            "query": "query input",
            "header": "request header",
            "cookie": "request cookie",
            "multipart": "uploaded request part",
            "model": "submitted request information",
            "argument": "input",
        }
        lines.append(
            f"  - {name or 'Unnamed input'} is a {requirement} "
            f"{descriptions.get(binding, 'input')}{validation}."
        )
        for constraint in parameter.get("constraints") or []:
            if not isinstance(constraint, dict):
                continue
            constraint_name = str(constraint.get("name") or "").casefold()
            values = constraint.get("values")
            values = values if isinstance(values, dict) else {}
            display_name = (name or "This input").casefold()
            if constraint_name in {"notnull", "nonnull"}:
                meaning = "must be supplied"
            elif constraint_name in {"notempty", "notblank"}:
                meaning = "must contain a value"
            elif constraint_name == "size":
                minimum = next(iter(values.get("min") or []), None)
                maximum = next(iter(values.get("max") or []), None)
                if minimum is not None and maximum is not None:
                    meaning = f"must contain between {minimum} and {maximum} items/characters"
                elif minimum is not None:
                    meaning = f"must contain at least {minimum} items/characters"
                elif maximum is not None:
                    meaning = f"must contain no more than {maximum} items/characters"
                else:
                    meaning = "must satisfy the configured size limit"
            elif constraint_name in {"min", "decimalmin"}:
                limit = next(iter(values.get("value") or []), "the configured minimum")
                meaning = f"must be at least {limit}"
            elif constraint_name in {"max", "decimalmax"}:
                limit = next(iter(values.get("value") or []), "the configured maximum")
                meaning = f"must be no greater than {limit}"
            elif constraint_name == "email":
                meaning = "must be a valid email address"
            elif constraint_name == "pattern":
                meaning = "must match the configured format"
            elif constraint_name.startswith("positive"):
                meaning = "must be zero or positive" if constraint_name.endswith("orzero") else "must be positive"
            elif constraint_name.startswith("negative"):
                meaning = "must be zero or negative" if constraint_name.endswith("orzero") else "must be negative"
            elif constraint_name.startswith("past"):
                meaning = "must be a past date/time"
            elif constraint_name.startswith("future"):
                meaning = "must be a future date/time"
            else:
                meaning = f"must satisfy the {constraint_name} rule"
            lines.append(f"    Rule: {display_name} {meaning}.")

    lines.append("Business process:")
    action_seen: set[tuple[str, str, str]] = set()
    business_steps: list[str] = []
    for _depth, source, target, data in reachable_edges:
        source_repo = str(G.nodes[source].get("repo") or endpoint_repo)
        target_repo = str(G.nodes[target].get("repo") or source_repo)
        action = _java_business_action(G, target)
        prefix = f"In {target_repo}, " if target_repo else ""
        if data.get("cross_service") and source_repo != target_repo:
            prefix = f"The process requests {target_repo} to "
        condition_values = _java_edge_conditions(data)
        condition_suffix = ""
        raw_conditions = data.get("conditions")
        if isinstance(raw_conditions, list) and raw_conditions:
            phrases = [
                _java_business_condition(value)
                for value in raw_conditions if isinstance(value, dict)
            ]
            if phrases:
                condition_suffix = f" ({'; '.join(phrases)})"
        elif condition_values:
            condition_suffix = f" ({'; '.join(condition_values)})"
        key = (target_repo, action, condition_suffix)
        if key in action_seen:
            continue
        action_seen.add(key)
        business_steps.append(f"{prefix}{action.casefold()}{condition_suffix}.")
    if business_steps:
        for number, step in enumerate(business_steps, 1):
            lines.append(f"  {number}. {step}")
    else:
        lines.append("  No downstream business operations are recorded in the graph.")

    rules: list[str] = []
    rule_expressions: dict[str, set[str]] = {}
    nodes_with_branch_edges: set[str] = set()
    for _depth, source, target, data in reachable_edges:
        repo = str(G.nodes[source].get("repo") or endpoint_repo)
        action = _java_business_action(G, target).casefold()
        for condition in data.get("conditions") or []:
            if not isinstance(condition, dict):
                continue
            nodes_with_branch_edges.add(source)
            original = html.unescape(str(condition.get("expression") or "")).strip()
            rule_expressions.setdefault(source, set()).add(original)
            phrase = _java_business_condition(condition)
            rule = (
                f"In {repo}, {phrase[:1].casefold() + phrase[1:]} "
                f"before the process performs {action}."
            )
            if rule not in rules:
                rules.append(rule)
    for node_id in reachable_nodes:
        metadata = _java_metadata(G, node_id)
        repo = str(G.nodes[node_id].get("repo") or endpoint_repo)
        for decision in metadata.get("java_decisions") or []:
            if not isinstance(decision, dict):
                continue
            kind = str(decision.get("kind") or "condition")
            raw_expression = html.unescape(str(decision.get("expression") or "")).strip()
            if raw_expression in rule_expressions.get(node_id, set()):
                continue
            if node_id in nodes_with_branch_edges and kind in {"switch", "case"}:
                # Reachable branch-edge conditions already identify the selected
                # switch alternatives; do not reintroduce infeasible cases here.
                continue
            expression = _java_business_words(decision.get("expression"))
            if not expression:
                continue
            if kind == "else":
                rule = f"In {repo}, otherwise follow the alternative when {expression.casefold()} is not satisfied."
            elif kind in {"loop", "for_each"}:
                rule = f"In {repo}, repeat processing while {expression.casefold()}."
            elif kind == "case":
                rule = f"In {repo}, select the {expression.casefold()} alternative."
            else:
                rule = f"In {repo}, continue the applicable branch when {expression.casefold()}."
            if rule not in rules:
                rules.append(rule)
    lines.append("Business rules and decision points:")
    if rules:
        for number, rule in enumerate(rules, 1):
            lines.append(f"  R{number}. {rule}")
    else:
        lines.append("  No explicit conditional rules are recorded in the current graph.")

    cross_service: list[str] = []
    for _depth, source, target, data in reachable_edges:
        if not data.get("cross_service"):
            continue
        source_repo = str(G.nodes[source].get("repo") or "unknown service")
        target_repo = str(G.nodes[target].get("repo") or "unknown service")
        action = _java_business_action(G, target).casefold()
        item = f"{source_repo} requests {target_repo} to {action}."
        if item not in cross_service:
            cross_service.append(item)
    lines.append("Service interactions:")
    if cross_service:
        for item in cross_service:
            lines.append(f"  - {item}")
    else:
        lines.append("  No cross-service interaction is recorded for this API.")

    alternative_outcomes: list[str] = []
    successful_outcomes: list[str] = []
    for node_id in reachable_nodes:
        metadata = _java_metadata(G, node_id)
        repo = str(G.nodes[node_id].get("repo") or endpoint_repo)
        for outcome in metadata.get("java_outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            raw_conditions = [
                value for value in outcome.get("conditions") or []
                if isinstance(value, dict)
            ]
            rendered_conditions = raw_conditions
            if raw_conditions:
                feasible = None
                for state in bindings_by_node.get(node_id, [{}]):
                    matched_switch_case = any(
                        _java_condition_truth(condition, state) is True
                        for _target, edge_data in outgoing.get(node_id, [])
                        for site in _java_edge_sites(edge_data)
                        for condition in site.get("conditions") or edge_data.get("conditions") or []
                        if isinstance(condition, dict)
                        and str(condition.get("kind") or "") == "switch"
                        and str(condition.get("branch") or "") == "case"
                    )
                    feasible = _java_feasible_edge_data(
                        {"conditions": raw_conditions},
                        state,
                        matched_switch_case=matched_switch_case,
                    )
                    if feasible is not None:
                        break
                if feasible is None:
                    continue
                rendered_conditions = [
                    value for value in feasible.get("conditions") or []
                    if isinstance(value, dict)
                ]
            kind = str(outcome.get("kind") or "")
            expression = _java_business_words(outcome.get("expression"))
            conditions = [
                _java_business_condition(value)
                for value in rendered_conditions
            ]
            suffix = f"; {' and '.join(conditions).casefold()}" if conditions else ""
            if kind == "throw":
                item = f"{repo} ends the applicable path with {expression.casefold() or 'an error'}{suffix}."
                if item not in alternative_outcomes:
                    alternative_outcomes.append(item)
            elif node_id == endpoint or conditions:
                item = f"{repo} returns the applicable business result{suffix}."
                if item not in successful_outcomes:
                    successful_outcomes.append(item)
    lines.append("Business response and outcomes:")
    if successful_outcomes:
        for item in successful_outcomes:
            lines.append(f"  - {item}")
    else:
        lines.append("  - Returns the result produced by the completed business path.")
    for item in alternative_outcomes:
        lines.append(f"  - Alternative outcome: {item}")

    unresolved_count = 0
    for node_id in reachable_nodes:
        for unresolved in _java_metadata(G, node_id).get("java_unresolved_calls") or []:
            if not isinstance(unresolved, dict):
                continue
            callee = str(unresolved.get("callee") or "")
            if (
                str(unresolved.get("call_kind") or "") == "constructor"
                and callee.casefold().endswith(("exception", "error"))
            ):
                continue
            conditions = [
                value for value in unresolved.get("conditions") or []
                if isinstance(value, dict)
            ]
            if conditions:
                is_feasible = False
                for state in bindings_by_node.get(node_id, [{}]):
                    matched_switch_case = any(
                        _java_condition_truth(condition, state) is True
                        for _target, edge_data in outgoing.get(node_id, [])
                        for site in _java_edge_sites(edge_data)
                        for condition in site.get("conditions") or edge_data.get("conditions") or []
                        if isinstance(condition, dict)
                        and str(condition.get("kind") or "") == "switch"
                        and str(condition.get("branch") or "") == "case"
                    )
                    if _java_feasible_edge_data(
                        {"conditions": conditions},
                        state,
                        matched_switch_case=matched_switch_case,
                    ) is not None:
                        is_feasible = True
                        break
                if not is_feasible:
                    continue
            unresolved_count += 1

    lines.extend([
        "Evidence boundaries:",
        "  - This explanation is generated from static Java AST evidence, not runtime traces.",
        "  - Business wording is a deterministic translation of method names and predicates.",
        f"  - {unresolved_count} observed Java call(s) in this reachable scope could not be statically bound to a target.",
        "  - Dynamic configuration, reflection, generated implementations and runtime bean selection may be absent.",
        "  - Rebuild and re-merge older graphs if request, response or rule metadata is missing.",
    ])
    return _cut_lines_to_budget(
        lines,
        token_budget,
        "Use a larger --budget for more business-rule evidence.",
    )


def _render_java_call_flow(
    G: nx.Graph,
    endpoint: str,
    *,
    title: str,
    mapping: str | None,
    question: str,
    token_budget: int,
) -> str:
    records = list(_true_edge_records(G))
    owners = _java_method_owners(G, records)

    def is_exception_node(node_id: str) -> bool:
        data = G.nodes[node_id]
        label = str(data.get("label") or "").casefold()
        owner_id = owners.get(node_id)
        owner = str(G.nodes[owner_id].get("label") or "").casefold() if owner_id else ""
        return label.endswith(("exception", "error")) or owner.endswith(("exception", "error"))

    def is_type_only_call_target(node_id: str) -> bool:
        """Java constructor/DTO references are not executable method-flow steps."""
        if node_id in owners:
            return False
        data = G.nodes[node_id]
        label = str(data.get("label") or "")
        metadata = data.get("metadata")
        if isinstance(metadata, dict) and metadata.get("java_http_role"):
            return False
        return bool(label[:1].isupper())

    call_records = [
        (src, tgt, data)
        for src, tgt, data in records
        if data.get("relation") == "calls" and src in G and tgt in G
        and not _java_is_test_node(G, src)
        and not _java_is_test_node(G, tgt)
        and not is_exception_node(tgt)
        and not is_type_only_call_target(tgt)
    ]
    from graphify.bridges import derive_java_method_name_bridges
    call_records.extend(derive_java_method_name_bridges(G))
    call_records.extend(_java_interface_dispatch_records(G, records, owners))
    lines = [title]
    if mapping:
        lines.append(f"Route: {mapping}")
    lines.extend([
        "Method mapping:",
        f"  {_java_flow_symbol(G, endpoint, owners)}",
        f"  Source: {_java_flow_source(G, endpoint) or '(no source location)'}",
        "Scope: production Java flow (test callers and source-less framework wrappers omitted)",
    ])
    endpoint_contract = _java_contract_summary(G, endpoint)
    lines.append("Endpoint request/response contract:")
    lines.append(
        f"  {endpoint_contract}"
        if endpoint_contract
        else "  (contract metadata absent; rebuild and re-merge with this Graphify version)"
    )

    outgoing: dict[str, list[tuple[str, dict]]] = {}
    incoming: dict[str, list[tuple[str, dict]]] = {}
    for src, tgt, data in call_records:
        outgoing.setdefault(src, []).append((tgt, data))
        incoming.setdefault(tgt, []).append((src, data))
    feasible_edges, _feasible_nodes, _feasible_bindings = _java_reachable_calls(
        G, endpoint, outgoing
    )
    feasible_outgoing: dict[str, list[tuple[str, dict]]] = {}
    for _depth, source, target, data in feasible_edges:
        feasible_outgoing.setdefault(source, []).append((target, data))
    # Every later path/branch decision must use the same constant-propagated
    # feasible graph as the completeness inventory.  Otherwise the curated
    # path can claim a DEVICE branch while the selected API passed ADDON.
    outgoing = feasible_outgoing
    def downstream_priority(item: tuple[str, dict]) -> tuple[int, int, int, str]:
        target, data = item
        target_edges = outgoing.get(target, [])
        return (
            0 if data.get("cross_service") else 1,
            0 if any(edge.get("cross_service") for _next, edge in target_edges) else 1,
            0 if data.get("relation") == "dispatches_to" else 1,
            _java_flow_symbol(G, target, owners),
        )

    for values in outgoing.values():
        values.sort(key=downstream_priority)
    for values in incoming.values():
        values.sort(key=lambda item: _java_flow_symbol(G, item[0], owners))

    # The output budget controls presentation size, not graph correctness.
    # Coupling traversal depth to the default 2,000-token budget previously
    # stopped an otherwise valid Java E2E flow after eight hops and mislabeled
    # that eighth node as a terminal. Build the complete bounded flow first;
    # _cut_lines_to_budget will report any presentation truncation honestly.
    max_depth = 64
    lines.append("Upstream calls:")
    upstream: list[tuple[int, str, str, dict]] = []
    upstream_queue: list[tuple[str, int]] = [(endpoint, 0)]
    upstream_seen = {endpoint}
    while upstream_queue:
        tgt, depth = upstream_queue.pop(0)
        if depth >= max_depth:
            continue
        for src, data in incoming.get(tgt, []):
            if not _java_flow_source(G, src):
                continue
            upstream.append((depth + 1, src, tgt, data))
            if src not in upstream_seen:
                upstream_seen.add(src)
                upstream_queue.append((src, depth + 1))
    upstream.sort(
        key=lambda item: (
            -item[0],
            _java_flow_symbol(G, item[1], owners),
            _java_flow_symbol(G, item[2], owners),
        )
    )
    if upstream:
        for number, (_depth, src, tgt, data) in enumerate(upstream, 1):
            evidence = _java_flow_edge_source(data)
            at = f" at={evidence}" if evidence else ""
            relation = str(data.get("relation") or "calls")
            lines.append(
                f"  {number}. {_java_flow_symbol(G, src, owners)} --{relation} "
                f"[{_java_flow_edge_details(data)}]--> "
                f"{_java_flow_symbol(G, tgt, owners)}{at}"
            )
    else:
        lines.append("  (none recorded in the graph)")

    def edge_key(src: str, tgt: str, data: dict) -> tuple[str, str, str, str]:
        return (
            src,
            tgt,
            str(data.get("relation") or "calls"),
            str(data.get("bridge_strategy") or ""),
        )

    def owner_label(node_id: str) -> str:
        owner_id = owners.get(node_id)
        return str(G.nodes[owner_id].get("label") or "") if owner_id else ""

    def detail_kind(node_id: str) -> str | None:
        folded = owner_label(node_id).casefold()
        if folded.endswith((
            "mapper", "util", "utils", "helper", "builder", "converter", "factory",
        )):
            return "mapper/helper"
        if folded.endswith(("config", "configuration")):
            return "configuration"
        return None

    def is_internal_detail(node_id: str) -> bool:
        return detail_kind(node_id) is not None

    def is_business_boundary(node_id: str) -> bool:
        return owner_label(node_id).casefold().endswith((
            "repository", "gateway", "client", "connector", "adapter",
        ))

    def normalise_context_term(term: str) -> str:
        folded = term.casefold()
        if folded.endswith("ies") and len(folded) > 4:
            return folded[:-3] + "y"
        if folded.endswith("s") and len(folded) > 3:
            return folded[:-1]
        return folded

    context_stopwords = {
        "complete", "explain", "flow", "java", "method", "remote", "repository",
        "service", "controller", "get", "post", "put", "patch", "delete", "head",
        "option", "rcom", "api", "the", "in", "of", "v1", "v2", "v3",
    }
    context_terms = {
        normalise_context_term(token)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", question)
        if normalise_context_term(token) not in context_stopwords
    }

    def context_score(node_id: str) -> int:
        label = str(G.nodes[node_id].get("label") or node_id)
        words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", label)
        terms = {
            normalise_context_term(token)
            for token in re.findall(r"[A-Za-z][A-Za-z0-9]*", words)
        }
        return len(context_terms & terms)

    def callsite_line(data: dict) -> int:
        location = str(data.get("source_location") or "")
        match = re.search(r"(?:^|:)L?(\d+)", location)
        return int(match.group(1)) if match else 1_000_000_000

    cross_reachable = {
        source
        for source, targets in outgoing.items()
        if any(data.get("cross_service") for _target, data in targets)
    }
    cross_queue = list(cross_reachable)
    while cross_queue:
        target = cross_queue.pop()
        for source, _data in incoming.get(target, []):
            if source not in cross_reachable and _java_flow_source(G, source):
                cross_reachable.add(source)
                cross_queue.append(source)

    def primary_candidates(src: str, ancestors: frozenset[str]) -> list[tuple[str, dict]]:
        candidates = [
            (target, data)
            for target, data in outgoing.get(src, [])
            if target not in ancestors and _java_flow_source(G, target)
        ]
        candidates.sort(key=lambda item: (
            0
            if item[1].get("cross_service")
            or item[0] in cross_reachable
            else 1,
            0 if is_business_boundary(item[0]) else 1,
            0
            if any(
                _java_flow_source(G, next_target)
                and detail_kind(next_target) != "configuration"
                for next_target, _next_data in outgoing.get(item[0], [])
            )
            else 1,
            1 if is_internal_detail(item[0]) else 0,
            0 if item[1].get("relation") == "dispatches_to" else 1,
            callsite_line(item[1]),
            _java_flow_symbol(G, item[0], owners),
        ))
        return candidates

    def render_edge(prefix: str, src: str, tgt: str, data: dict) -> None:
        evidence = _java_flow_edge_source(data)
        at = f" at={evidence}" if evidence else ""
        relation = str(data.get("relation") or "calls")
        lines.append(
            f"{prefix}{_java_flow_symbol(G, src, owners)} --{relation} "
            f"[{_java_flow_edge_details(data)}]--> "
            f"{_java_flow_symbol(G, tgt, owners)}{at}"
        )
        for condition in _java_edge_conditions(data):
            lines.append(f"      Condition: {condition}")

    def terminal_reason(node_id: str) -> str:
        folded = owner_label(node_id).casefold()
        if folded.endswith(("repository", "gateway", "client", "connector", "adapter")):
            return "repository/external boundary; no further Java call recorded"
        if folded.endswith(("service", "serviceimpl")):
            return "unresolved service leaf; no implementation or downstream call recorded"
        return "no further production Java call recorded"

    # A controller method can orchestrate several independent downstream services.
    # Keep the common prefix once, then render every cross-service branch as a
    # first-class E2E call instead of arbitrarily promoting the highest-confidence
    # bridge to "primary" and demoting the other service calls.
    orchestration_edges: list[tuple[str, str, dict]] = []
    orchestration_nodes = [endpoint]
    orchestration_ancestors = frozenset({endpoint})
    orchestration_current = endpoint
    service_starts: list[tuple[str, dict]] = []
    for _depth in range(max_depth):
        candidates = primary_candidates(orchestration_current, orchestration_ancestors)
        cross_candidates = [
            item for item in candidates
            if item[1].get("cross_service") or item[0] in cross_reachable
        ]
        unique_cross: dict[str, tuple[str, dict]] = {}
        for target, data in cross_candidates:
            unique_cross.setdefault(target, (target, data))
        cross_candidates = list(unique_cross.values())
        if len(cross_candidates) > 1:
            service_starts = sorted(
                cross_candidates,
                key=lambda item: (
                    callsite_line(item[1]),
                    _java_flow_symbol(G, item[0], owners),
                ),
            )
            break
        if len(cross_candidates) != 1:
            break
        target, data = cross_candidates[0]
        orchestration_edges.append((orchestration_current, target, data))
        orchestration_nodes.append(target)
        orchestration_ancestors |= {target}
        orchestration_current = target

    if len(service_starts) > 1:
        lines.append("Endpoint orchestration:")
        for number, (src, tgt, data) in enumerate(orchestration_edges, 1):
            render_edge(f"  {number}. ", src, tgt, data)
        if not orchestration_edges:
            lines.append(f"  {_java_flow_symbol(G, endpoint, owners)}")

        service_paths: list[tuple[list[tuple[str, str, dict]], bool]] = []
        contextual_alternatives_omitted = 0
        for first_target, first_data in service_starts:
            path = [(orchestration_current, first_target, first_data)]
            ancestors = orchestration_ancestors | {first_target}
            current = first_target
            depth_limited = False
            for _depth in range(max_depth - 1):
                candidates = primary_candidates(current, ancestors)
                if not candidates:
                    break
                same_owner = [
                    item for item in candidates
                    if owners.get(item[0]) is not None
                    and owners.get(item[0]) == owners.get(current)
                    and not is_internal_detail(item[0])
                ]
                families: dict[str, list[tuple[str, dict]]] = {}
                for item in same_owner:
                    label = str(G.nodes[item[0]].get("label") or "")
                    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", label)
                    tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*", words)
                    if tokens:
                        families.setdefault(normalise_context_term(tokens[-1]), []).append(item)
                preferred: set[str] = set()
                for family in families.values():
                    # Three or more same-owner methods with the same semantic
                    # suffix are switch/strategy alternatives (getAddonEntries,
                    # getDeviceEntries, ...), not an ordinary helper sequence.
                    if len(family) < 3:
                        continue
                    best_context = max(context_score(target) for target, _data in family)
                    if best_context <= 0:
                        continue
                    family_preferred = {
                        target
                        for target, _data in family
                        if context_score(target) == best_context
                    }
                    preferred.update(family_preferred)
                    contextual_alternatives_omitted += sum(
                        1 for target, _data in family if target not in family_preferred
                    )
                    candidates = [
                        item for item in candidates
                        if item not in family or item[0] in family_preferred
                    ]
                if preferred:
                    candidates.sort(key=lambda item: 0 if item[0] in preferred else 1)
                target, data = candidates[0]
                if detail_kind(target) == "configuration":
                    break
                repository_operations = [
                    item for item in candidates
                    if is_business_boundary(current)
                    and owners.get(item[0]) == owners.get(current)
                    and str(item[1].get("relation") or "calls") == "calls"
                    and detail_kind(item[0]) != "configuration"
                ]
                if len(repository_operations) > 1 and target in {
                    operation_target for operation_target, _operation_data
                    in repository_operations
                }:
                    # Repository methods commonly make several sequential calls
                    # (for example, build a Redis key and then execute the Redis
                    # operation). Preserve every direct operation in Java source
                    # order, but continue traversal through the operation that has
                    # downstream executable evidence.
                    repository_operations.sort(key=lambda item: (
                        callsite_line(item[1]),
                        _java_flow_symbol(G, item[0], owners),
                    ))
                    for operation_target, operation_data in repository_operations:
                        path.append((current, operation_target, operation_data))
                        ancestors |= {operation_target}
                else:
                    path.append((current, target, data))
                    ancestors |= {target}
                current = target
            else:
                depth_limited = bool(primary_candidates(current, ancestors))
            service_paths.append((path, depth_limited))

        lines.append("E2E service calls:")
        all_path_keys: set[tuple[str, str, str, str]] = set()
        for call_number, (path, depth_limited) in enumerate(service_paths, 1):
            target_repos = []
            for _src, tgt, _data in path:
                repo = str(G.nodes[tgt].get("repo") or "").strip()
                if repo and repo not in target_repos:
                    target_repos.append(repo)
            repo_suffix = f" ({' -> '.join(target_repos)})" if target_repos else ""
            lines.append(f"  Service call {call_number}{repo_suffix}:")
            for step_number, (src, tgt, data) in enumerate(path, 1):
                all_path_keys.add(edge_key(src, tgt, data))
                render_edge(f"    {step_number}. ", src, tgt, data)
            if depth_limited:
                lines.append(
                    "    Traversal stopped: Java flow depth limit reached; "
                    "the last shown node is not a confirmed terminal."
                )
            else:
                terminal = path[-1][1]
                lines.append(
                    f"    Terminal: {_java_flow_symbol(G, terminal, owners)} "
                    f"({terminal_reason(terminal)})"
                )

        response_edges = []
        used_keys = {
            edge_key(src, tgt, data) for src, tgt, data in orchestration_edges
        } | all_path_keys
        for node_id in orchestration_nodes:
            for target, data in outgoing.get(node_id, []):
                if (
                    edge_key(node_id, target, data) not in used_keys
                    and detail_kind(target) == "mapper/helper"
                ):
                    response_edges.append((node_id, target, data))
        if response_edges:
            lines.append("Response mapping:")
            for number, (src, tgt, data) in enumerate(response_edges, 1):
                render_edge(f"  {number}. ", src, tgt, data)
            lines.append("  Further mapper/helper internals collapsed.")
        if contextual_alternatives_omitted:
            lines.append(
                f"Context filtering: {contextual_alternatives_omitted} non-matching "
                "same-service method alternative(s) omitted."
            )
        _append_java_developer_evidence(lines, G, endpoint, outgoing, owners)
        return _cut_lines_to_budget(
            lines,
            token_budget,
            "Use a larger --budget for more service-call evidence.",
        )

    primary_edges: list[tuple[str, str, dict]] = []
    primary_nodes = [endpoint]
    ancestors = frozenset({endpoint})
    current = endpoint
    primary_depth_limited = False
    for _depth in range(max_depth):
        candidates = primary_candidates(current, ancestors)
        if not candidates:
            break
        target, data = candidates[0]
        primary_edges.append((current, target, data))
        primary_nodes.append(target)
        ancestors |= {target}
        current = target
    else:
        primary_depth_limited = bool(primary_candidates(current, ancestors))

    lines.append("Primary end-to-end path:")
    if primary_edges:
        for number, (src, tgt, data) in enumerate(primary_edges, 1):
            evidence = _java_flow_edge_source(data)
            at = f" at={evidence}" if evidence else ""
            relation = str(data.get("relation") or "calls")
            lines.append(
                f"  {number}. {_java_flow_symbol(G, src, owners)} --{relation} "
                f"[{_java_flow_edge_details(data)}]--> "
                f"{_java_flow_symbol(G, tgt, owners)}{at}"
            )
            for condition in _java_edge_conditions(data):
                lines.append(f"      Condition: {condition}")
    else:
        lines.append("  (none recorded in the graph)")

    primary_keys = {edge_key(src, tgt, data) for src, tgt, data in primary_edges}
    supporting: list[tuple[list[tuple[str, str, dict]], bool, bool]] = []
    supporting_seen: set[tuple[str, str, str, str]] = set()
    for primary_node in primary_nodes:
        for target, data in outgoing.get(primary_node, []):
            first_key = edge_key(primary_node, target, data)
            if (
                first_key in primary_keys
                or first_key in supporting_seen
                or not _java_flow_source(G, target)
            ):
                continue
            supporting_seen.add(first_key)
            branch = [primary_node, target]
            branch_edges = [(primary_node, target, data)]
            collapsed = is_internal_detail(target)
            branch_ancestors = frozenset(branch)
            branch_current = target
            branch_depth_limited = False
            for _depth in range(max_depth - 1):
                if collapsed:
                    break
                candidates = primary_candidates(branch_current, branch_ancestors)
                if not candidates:
                    break
                next_target, next_data = candidates[0]
                next_key = edge_key(branch_current, next_target, next_data)
                if next_key in primary_keys or next_key in supporting_seen:
                    break
                supporting_seen.add(next_key)
                branch_edges.append((branch_current, next_target, next_data))
                branch.append(next_target)
                branch_ancestors |= {next_target}
                branch_current = next_target
                collapsed = is_internal_detail(next_target)
            else:
                branch_depth_limited = bool(
                    primary_candidates(branch_current, branch_ancestors)
                )
            supporting.append((branch_edges, collapsed, branch_depth_limited))

    if supporting:
        lines.append("Supporting branches:")
        for branch_number, (branch_edges, collapsed, depth_limited) in enumerate(
            supporting[:12], 1
        ):
            lines.append(f"  Branch {branch_number}:")
            for step_number, (src, tgt, data) in enumerate(branch_edges, 1):
                evidence = _java_flow_edge_source(data)
                at = f" at={evidence}" if evidence else ""
                relation = str(data.get("relation") or "calls")
                lines.append(
                    f"    {step_number}. {_java_flow_symbol(G, src, owners)} "
                    f"--{relation} [{_java_flow_edge_details(data)}]--> "
                    f"{_java_flow_symbol(G, tgt, owners)}{at}"
                )
                for condition in _java_edge_conditions(data):
                    lines.append(f"        Condition: {condition}")
            terminal = branch_edges[-1][1]
            if depth_limited:
                lines.append(
                    "    Traversal stopped: Java flow depth limit reached; "
                    "the last shown node is not a confirmed terminal."
                )
            elif collapsed:
                lines.append("    Further mapper/helper internals collapsed.")
            else:
                folded = owner_label(terminal).casefold()
                if folded.endswith((
                    "repository", "gateway", "client", "connector", "adapter",
                )):
                    lines.append(
                        f"    Terminal: {_java_flow_symbol(G, terminal, owners)} "
                        "(repository/external boundary; no further Java call recorded)"
                    )
        if len(supporting) > 12:
            lines.append(f"  ... {len(supporting) - 12} additional branch(es) omitted")

    if primary_depth_limited:
        lines.append(
            "Primary traversal stopped: Java flow depth limit reached; "
            "the last shown node is not a confirmed terminal."
        )
    elif primary_nodes and primary_nodes[-1] != endpoint:
        terminal = primary_nodes[-1]
        folded = owner_label(terminal).casefold()
        if folded.endswith(("repository", "gateway", "client", "connector", "adapter")):
            reason = "repository/external boundary; no further Java call recorded"
        elif folded.endswith(("service", "serviceimpl")):
            reason = "unresolved service leaf; no implementation or downstream call recorded"
        else:
            reason = "no further production Java call recorded"
        lines.append(
            f"Primary terminal: {_java_flow_symbol(G, terminal, owners)} ({reason})"
        )
    _append_java_developer_evidence(lines, G, endpoint, outgoing, owners)
    return _cut_lines_to_budget(
        lines,
        token_budget,
        "Use the repository-qualified method or a larger --budget for more flow edges.",
    )


def _java_flow_ambiguity(
    G: nx.Graph,
    candidates: list[str],
    *,
    description: str,
) -> str:
    records = list(_true_edge_records(G))
    owners = _java_method_owners(G, records)
    lines = [f"AMBIGUOUS JAVA FLOW: {description} matches {len(candidates)} methods."]
    for node_id in sorted(candidates, key=lambda n: (_java_flow_symbol(G, n, owners), n)):
        lines.append(
            f"  {_java_flow_symbol(G, node_id, owners)} "
            f"at {_java_flow_source(G, node_id) or '(unknown source)'}"
        )
        lines.append(f"    id: {node_id}")
    lines.append("Retry with the service/repository name or the exact node ID.")
    return "\n".join(lines)


def _try_java_flow_query(
    G: nx.Graph,
    question: str,
    token_budget: int,
    *,
    audience: str | None = None,
) -> str | None:
    """Return a deterministic Java route/method flow for explicit flow questions."""
    if not _JAVA_FLOW_INTENT_RE.search(question):
        return None
    from graphify.bridges import _normalise_http_path

    verb_match = re.search(
        r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b",
        question,
        flags=re.IGNORECASE,
    )
    verb = verb_match.group(1).upper() if verb_match else None
    path_match = _JAVA_HTTP_PATH_RE.search(question)
    mentioned_repos = _mentioned_java_repos(G, question)

    if path_match:
        # Keep a closing `}` because it is part of a Java route template
        # (`/orders/{id}`), not sentence punctuation.
        raw_path = path_match.group(1).rstrip(".,;!?)]\"'")
        wanted_path = _normalise_http_path(raw_path)
        inbound: list[tuple[str, str, str]] = []
        outbound: list[tuple[str, str, str]] = []
        for node_id, data in G.nodes(data=True):
            if mentioned_repos and str(data.get("repo") or "") not in mentioned_repos:
                continue
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                continue
            role = str(metadata.get("java_http_role") or "").casefold()
            routes = metadata.get("java_http_routes")
            if role not in {"inbound", "outbound"} or not isinstance(routes, list):
                continue
            for route in routes:
                if not isinstance(route, dict):
                    continue
                route_method = str(route.get("method") or "*").upper()
                route_path = _normalise_http_path(route.get("path"))
                if route_path != wanted_path:
                    continue
                if verb and route_method not in {verb, "*"}:
                    continue
                item = (str(node_id), route_method, str(route.get("path") or wanted_path))
                (inbound if role == "inbound" else outbound).append(item)
        candidates = inbound or outbound
        description = f"{verb + ' ' if verb else ''}{raw_path}"
        if not candidates:
            repo_suffix = f" in {', '.join(sorted(mentioned_repos))}" if mentioned_repos else ""
            return (
                f"JAVA API FLOW NOT FOUND: no controller/client method maps "
                f"{description}{repo_suffix}. Rebuild and re-merge the graph."
            )
        candidate_ids = list(dict.fromkeys(item[0] for item in candidates))
        if len(candidate_ids) != 1:
            return _java_flow_ambiguity(G, candidate_ids, description=description)
        endpoint, route_method, route_path = candidates[0]
        display_method = verb or route_method
        if audience == "bsa":
            return _render_java_business_flow(
                G,
                endpoint,
                mapping=f"{display_method} {route_path}",
                question=question,
                token_budget=token_budget,
            )
        return _render_java_call_flow(
            G,
            endpoint,
            title="JAVA API FLOW",
            mapping=f"{display_method} {route_path}",
            question=question,
            token_budget=token_budget,
        )

    records = list(_true_edge_records(G))
    owners = _java_method_owners(G, records)
    for qualified in _JAVA_QUALIFIED_METHOD_RE.findall(question):
        parts = qualified.split(".")
        if len(parts) < 2:
            continue
        class_name, method_name = parts[-2], parts[-1]
        if not class_name[:1].isupper():
            continue
        candidates = []
        for node_id, owner_id in owners.items():
            data = G.nodes[node_id]
            owner = G.nodes[owner_id]
            if str(owner.get("label") or "") != class_name:
                continue
            if str(data.get("label") or "").lstrip(".").removesuffix("()") != method_name:
                continue
            if mentioned_repos and str(data.get("repo") or "") not in mentioned_repos:
                continue
            candidates.append(node_id)
        description = f"{class_name}.{method_name}()"
        if len(candidates) > 1:
            return _java_flow_ambiguity(G, candidates, description=description)
        if len(candidates) == 1:
            if audience == "bsa":
                return _render_java_business_flow(
                    G,
                    candidates[0],
                    mapping=None,
                    question=question,
                    token_budget=token_budget,
                )
            return _render_java_call_flow(
                G,
                candidates[0],
                title="JAVA METHOD FLOW",
                mapping=None,
                question=question,
                token_budget=token_budget,
            )
        return f"JAVA METHOD FLOW NOT FOUND: {description} is not present in the graph."
    return None


def _query_graph_text(
    G: nx.Graph,
    question: str,
    *,
    mode: str = "bfs",
    depth: int = 3,
    token_budget: int = 2000,
    context_filters: list[str] | None = None,
    audience: str | None = None,
) -> str:
    resolved_audience = audience
    if resolved_audience is None:
        resolved_audience = "bsa" if _JAVA_BSA_AUDIENCE_RE.search(question) else "developer"
    java_flow = _try_java_flow_query(
        G,
        question,
        token_budget,
        audience=resolved_audience,
    )
    if java_flow is not None:
        return java_flow
    terms = _query_terms(question)
    # One graph scoring pass produces both the combined ranking (used to drive
    # the gap-based seed selection below) and the per-token singleton winners
    # (used by _pick_seeds' per-term guarantee). Previously this was T+1 passes
    # — one combined + one per query token — re-walking the whole graph each
    # time; on a 100k-node, three-term benchmark ~71% of scoring time was
    # spent in those redundant per-term passes.
    qs = _score_query(G, terms, collect_per_term_seeds=True)
    start_nodes = _pick_seeds(qs.ranked, G=G, best_seed_by_term=qs.best_seed_by_term)
    if not start_nodes:
        return "No matching nodes found."
    resolved_filters, filter_source = _resolve_context_filters(question, context_filters)
    traversal_graph = _filter_graph_by_context(G, resolved_filters)
    nodes, edges = _dfs(traversal_graph, start_nodes, depth) if mode == "dfs" else _bfs(traversal_graph, start_nodes, depth)
    header_parts = [
        f"Traversal: {mode.upper()} depth={depth}",
        f"Start: {[G.nodes[n].get('label', n) for n in start_nodes]}",
    ]
    if resolved_filters:
        header_parts.append(f"Context: {', '.join(resolved_filters)} ({filter_source})")
    header_parts.append(f"{len(nodes)} nodes found")
    header = " | ".join(header_parts) + "\n\n"
    # Pass the seeds so the queried symbol renders first and survives truncation
    # (#BUG2): a branch merge had silently dropped this argument, leaving the
    # seed-first ordering as dead code.
    return header + _subgraph_to_text(traversal_graph, nodes, edges, token_budget, seeds=start_nodes)


def _find_node_tiers(
    G: nx.Graph, label: str
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return match tiers in precedence order: (source_exact, exact, prefix, substring).

    Split out of `_find_node` so callers that must not guess between equally-good
    matches can inspect the winning tier alone. `_find_node` flattens these, and
    its consumers take `[0]` — which resolves by graph-iteration order when one
    tier holds several nodes from different files. See `find_node_ambiguity`.
    """
    term = " ".join(_search_tokens(label))
    if not term:
        return []
    # Punctuation-preserving normalized query. `term` tokenizes on \w+ (so
    # "blockStream.ts" -> "blockstream ts", space where the '.' was), but a node's
    # stored `norm_label` keeps punctuation ("blockstream.ts"). Matching only via
    # `term`/`label_tokens` works when the node label tokenizes the same way, but is
    # fragile if `label` and `norm_label` diverge. `norm_query` matches `norm_label`
    # symmetrically so an exactly-typed punctuated label always resolves (#1704).
    norm_query = _strip_diacritics(str(label)).lower().strip()
    raw_query = str(label).strip().casefold()
    source_exact: list[str] = []
    exact: list[str] = []
    prefix: list[str] = []
    substring: list[str] = []
    # Trigram prefilter (graph-iteration order preserved so exact/prefix/substring
    # ordering — and thus matches[0] — is byte-identical to the full scan).
    candidate_ids = _trigram_candidates(G, [term, norm_query])
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    for nid, d in node_iter:
        norm_label = d.get("norm_label") or _strip_diacritics(d.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        label_tokens = " ".join(_search_tokens(d.get("label") or ""))
        source_tokens = " ".join(_search_tokens(d.get("source_file") or ""))
        nid_lower = nid.lower()
        if raw_query == nid_lower:
            exact.append(nid)
        elif term == source_tokens:
            source_exact.append(nid)
        elif (
            term == norm_label or term == bare_label or term == label_tokens or term == nid_lower
            or norm_query == norm_label or norm_query == bare_label
        ):
            exact.append(nid)
        elif (
            norm_label.startswith(term)
            or bare_label.startswith(term)
            or label_tokens.startswith(term)
            or nid_lower.startswith(term)
            or norm_label.startswith(norm_query)
            or bare_label.startswith(norm_query)
        ):
            prefix.append(nid)
        elif term in norm_label or term in label_tokens or norm_query in norm_label:
            substring.append(nid)

    if source_exact:
        query_basename = _strip_diacritics(Path(label).name).lower()
        preferred = []
        for nid in source_exact:
            if str(G.nodes[nid].get("source_location", "")) != "L1":
                continue
            # File-node label is the bare basename OR a directory-qualified form
            # from the #2032 disambiguation pass (e.g. "process-order/index.ts").
            lbl = _strip_diacritics(str(G.nodes[nid].get("label") or "")).lower()
            if lbl == query_basename or lbl.endswith("/" + query_basename):
                preferred.append(nid)
        if len(preferred) == 1:
            source_exact = preferred + [nid for nid in source_exact if nid != preferred[0]]

    return source_exact, exact, prefix, substring


def _find_node(G: nx.Graph, label: str) -> list[str]:
    """Return node IDs whose label or ID matches the search term (diacritic-insensitive).

    Results are ordered by precedence: exact source-file path match first, then
    exact (label/ID) match, then prefix match, then substring match. Node-ID exact
    matches are grouped with label exact matches.
    """
    source_exact, exact, prefix, substring = _find_node_tiers(G, label)
    return source_exact + exact + prefix + substring


def find_node_ambiguity(G: nx.Graph, label: str) -> list[str]:
    """Return rival candidates when the winning match tier spans several source files.

    `_find_node` ranks matches but never reports that a tie was broken, so callers
    taking `[0]` present one arbitrary file as the answer. Two workspaces that each
    define `MetricsPort` put both nodes in the same `exact` tier, separated only by
    `G.nodes()` iteration order — reorder the graph and the same query answers with
    a different file, equally confidently.

    Returns one representative node id per distinct source file when the winning
    tier is split that way, else `[]`. Several matches *within one file* (a file
    node plus its members) are ordinary precedence, not ambiguity, and return `[]`.

    `_disambiguate_file_node_labels` (#2032) already relabels colliding *file*
    nodes; this covers the symbol case it does not reach.
    """
    for tier in _find_node_tiers(G, label):
        if not tier:
            continue
        by_source: dict[str, str] = {}
        for nid in tier:
            source = str(G.nodes[nid].get("source_file") or "")
            by_source.setdefault(source, nid)
        return list(by_source.values()) if len(by_source) > 1 else []
    return []


def _filter_blank_stdin() -> None:
    """Filter blank lines from stdin before MCP reads it.

    Some MCP clients (Claude Desktop, etc.) send blank lines between JSON
    messages. The MCP stdio transport tries to parse every line as a
    JSONRPCMessage, so a bare newline triggers a Pydantic ValidationError.
    This installs an OS-level pipe that relays stdin while dropping blanks.
    """
    r_fd, w_fd = os.pipe()
    saved_fd = os.dup(sys.stdin.fileno())

    def _relay() -> None:
        try:
            with open(saved_fd, "rb") as src, open(w_fd, "wb") as dst:
                for line in src:
                    if line.strip():
                        dst.write(line)
                        dst.flush()
        except Exception:
            pass

    threading.Thread(target=_relay, daemon=True).start()
    os.dup2(r_fd, sys.stdin.fileno())
    os.close(r_fd)
    sys.stdin = open(0, "r", closefd=False)


def _community_header(cid: int, community_name) -> str:
    # Header for get_community: "Community N — Name", matching get_node / query
    # output which read the community_name attribute to_json writes onto nodes.
    # Skip the name when it is just the "Community N" placeholder (written for
    # unnamed communities) so the header never reads "Community 12 — Community 12";
    # also falls back to the bare id when there is no name. Name is sanitised
    # (F-010) like every other LLM-derived field.
    base = f"Community {cid}"
    if community_name:
        clean = sanitize_label(str(community_name))
        if clean and clean != base:
            return f"{base} — {clean}"
    return base


def _build_server(graph_path: str):
    """Build the configured low-level MCP Server (shared by every transport).

    All graph query tools and resources are registered here over a single
    ``mcp.server.Server`` instance; the caller picks the transport (stdio or
    Streamable HTTP) and runs it. Hot-reload of graph.json works the same way
    regardless of transport, since reloads happen inside the tool handlers.
    """
    try:
        from mcp.server import Server
        from mcp import types
    except ImportError as e:
        raise ImportError('mcp not installed. Run: pip install "graphifyy[mcp]"') from e
    try:
        from mcp.types import AnyUrl
    except ImportError:
        # mcp >= 2.0 dropped the AnyUrl re-export; it was always pydantic's
        # AnyUrl (pydantic is an mcp dependency, so this import cannot miss).
        from pydantic import AnyUrl

    from graphify import paths as _paths

    # Graph contexts comprise one pinned configured default plus a bounded LRU
    # of project_path graphs. This preserves the configured graph's warm index
    # while preventing a shared server from retaining every project it serves.
    _default_graph_path = str(Path(graph_path).resolve())
    _ctx_cache = _GraphContextCache(_max_server_contexts())

    def _load_ctx(path: str):
        """Return the current default or project graph context as a tool error.

        Unlike ``_load_graph``, this never lets a missing or corrupt client
        graph terminate the MCP process; it raises so other projects remain
        available on the same server.
        """
        resolved_path = str(Path(path).resolve())
        return _ctx_cache.load(resolved_path, pinned=resolved_path == _default_graph_path)

    def _resolve_graph_path(project_path) -> str:
        """Map an optional project_path to a concrete graph.json path. ``None``
        keeps the server's default graph (backward-compatible); a project_path
        resolves to ``<project_path>/<GRAPHIFY_OUT>/graph.json``, honouring the
        GRAPHIFY_OUT override so worktree/shared-output setups keep working."""
        if not project_path:
            return _default_graph_path
        return str(Path(project_path) / _paths.GRAPHIFY_OUT / "graph.json")

    # Active per-request context, rebound by _select_graph() and read by the tool
    # handlers below. No lock needed on the hot path: _select_graph and the
    # handler run in one synchronous stretch of each call_tool coroutine (no
    # await between them), so a concurrent call never observes a half-applied
    # swap.
    active_graph_path = _default_graph_path
    try:
        G, communities = _load_ctx(_default_graph_path)
    except (FileNotFoundError, RuntimeError):
        # No default graph at startup → run as a pure multi-project server. Tools
        # then require project_path; a call without one gets a clear error rather
        # than the process refusing to start (which is what _load_graph would do).
        G, communities = None, {}

    def _select_graph(project_path) -> None:
        nonlocal G, communities, active_graph_path
        path = _resolve_graph_path(project_path)
        G, communities = _load_ctx(path)
        active_graph_path = str(Path(path).resolve())

    # NOTE: no decorators here — the handlers below are plain coroutines,
    # bound to the Server at the END of this function in a version-aware way:
    # mcp 1.x exposes the @server.list_tools()/... decorator API, mcp 2.x
    # replaced it with on_list_tools=/... constructor callbacks.
    async def list_tools() -> list[types.Tool]:
        _tools = [
            types.Tool(
                name="query_graph",
                description="Search the knowledge graph using BFS or DFS. Returns relevant nodes and edges as text context.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "Natural language question or keyword search"},
                        "mode": {"type": "string", "enum": ["bfs", "dfs"], "default": "bfs",
                                 "description": "bfs=broad context, dfs=trace a specific path"},
                        "depth": {"type": "integer", "default": 3, "description": "Traversal depth (1-6)"},
                        "token_budget": {"type": "integer", "default": 2000, "description": "Max output tokens"},
                        "context_filter": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional explicit edge-context filter, e.g. ['call', 'field']",
                        },
                    },
                    "required": ["question"],
                },
            ),
            types.Tool(
                name="get_node",
                description="Get full details for a specific node by label or ID.",
                inputSchema={
                    "type": "object",
                    "properties": {"label": {"type": "string", "description": "Node label or ID to look up"}},
                    "required": ["label"],
                },
            ),
            types.Tool(
                name="get_neighbors",
                description="Get all direct neighbors of a node with edge details.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "relation_filter": {"type": "string", "description": "Optional: filter by relation type"},
                        "token_budget": {"type": "integer", "default": 2000, "description": "Max output tokens"},
                    },
                    "required": ["label"],
                },
            ),
            types.Tool(
                name="get_community",
                description="Get all nodes in a community by community ID.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "community_id": {"type": "integer", "description": "Community ID (0-indexed by size)"},
                        "token_budget": {"type": "integer", "default": 2000, "description": "Max output tokens"},
                    },
                    "required": ["community_id"],
                },
            ),
            types.Tool(
                name="god_nodes",
                description="Return the most connected nodes - the core abstractions of the knowledge graph.",
                inputSchema={"type": "object", "properties": {"top_n": {"type": "integer", "default": 10}}},
            ),
            types.Tool(
                name="graph_stats",
                description="Return summary statistics: node count, edge count, communities, confidence breakdown.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="shortest_path",
                description="Find the shortest path between two concepts in the knowledge graph.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Source concept label or keyword"},
                        "target": {"type": "string", "description": "Target concept label or keyword"},
                        "max_hops": {"type": "integer", "default": 8, "description": "Maximum hops to consider"},
                    },
                    "required": ["source", "target"],
                },
            ),
            types.Tool(
                name="list_prs",
                description=(
                    "List open GitHub PRs with CI status, review state, and graph impact "
                    "(which communities each PR touches, blast radius). Use this before starting "
                    "work to check if a PR already covers the area you're about to change."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "base": {"type": "string", "description": "Base branch to filter PRs by (auto-detected if omitted)"},
                        "repo": {"type": "string", "description": "GitHub repo (owner/repo). Defaults to current repo."},
                    },
                },
            ),
            types.Tool(
                name="get_pr_impact",
                description=(
                    "Get detailed graph impact for a specific PR: which files it changes, "
                    "which knowledge-graph communities are affected, and how many nodes are touched. "
                    "Use this to assess merge risk or check for overlap with your current work."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "pr_number": {"type": "integer", "description": "PR number to analyse"},
                        "repo": {"type": "string", "description": "GitHub repo (owner/repo). Defaults to current repo."},
                    },
                    "required": ["pr_number"],
                },
            ),
            types.Tool(
                name="triage_prs",
                description=(
                    "Return all actionable open PRs (correct base, not stale) with full graph impact data "
                    "so you can reason about review priority, merge order, and conflict risk. "
                    "Call this when the user asks 'what PRs should I review?' or 'what's ready to merge?'"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "base": {"type": "string", "description": "Base branch to filter PRs by (auto-detected if omitted)"},
                        "repo": {"type": "string", "description": "GitHub repo (owner/repo). Defaults to current repo."},
                    },
                },
            ),
        ]
        # Multi-project support: every tool accepts an optional project_path.
        # Injected here (rather than repeated in 11 literal schemas) so the set
        # stays in lockstep as tools are added. Omitting it keeps the historical
        # single-graph behaviour, so this is purely additive for existing callers.
        for _t in _tools:
            # The constructor accepts the camelCase alias in both majors, but
            # attribute access is inputSchema on mcp 1.x and input_schema on 2.x.
            _schema = getattr(_t, "inputSchema", None)
            if _schema is None:
                _schema = _t.input_schema
            _schema.setdefault("properties", {})["project_path"] = {
                "type": "string",
                "description": (
                    "Absolute path to a project directory containing "
                    "graphify-out/graph.json. Optional — defaults to the graph "
                    "this server was started with."
                ),
            }
        return _tools

    def _tool_query_graph(arguments: dict) -> str:
        import time as _time
        from graphify import querylog
        question = arguments["question"]
        mode = arguments.get("mode", "bfs")
        depth = min(int(arguments.get("depth", 3)), 6)
        budget = int(arguments.get("token_budget", 2000))
        context_filter = arguments.get("context_filter")
        _t0 = _time.perf_counter()
        result = _query_graph_text(
            G,
            question,
            mode=mode,
            depth=depth,
            token_budget=budget,
            context_filters=context_filter,
        )
        querylog.log_query(
            kind="mcp_query",
            question=question,
            corpus=str(active_graph_path),
            result=result,
            mode=mode,
            depth=depth,
            token_budget=budget,
            duration_ms=(_time.perf_counter() - _t0) * 1000,
        )
        return result

    def _tool_get_node(arguments: dict) -> str:
        label = arguments["label"].lower()
        matches = [(nid, d) for nid, d in G.nodes(data=True)
                   if label in (d.get("label") or "").lower() or label == nid.lower()]
        if not matches:
            return f"No node matching '{label}' found."
        nid, d = matches[0]
        # Sanitise every LLM-derived field before concatenation (F-010).
        return "\n".join([
            f"Node: {sanitize_label(d.get('label', nid))}",
            f"  ID: {sanitize_label(nid)}",
            f"  Source: {sanitize_label(str(d.get('source_file', '')))} {sanitize_label(str(d.get('source_location', '')))}",
            f"  Type: {sanitize_label(str(d.get('file_type', '')))}",
            f"  Community: {sanitize_label(str(d.get('community_name') or d.get('community', '')))}",
            f"  Degree: {G.degree(nid)}",
        ])

    def _tool_get_neighbors(arguments: dict) -> str:
        label = arguments["label"].lower()
        rel_filter = arguments.get("relation_filter", "").lower()
        matches = _find_node(G, label)
        if not matches:
            return f"No node matching '{label}' found."
        rivals = find_node_ambiguity(G, label)
        if rivals:
            listing = "\n".join(
                f"  {G.nodes[r].get('source_file') or r}\n    id: {r}" for r in rivals
            )
            return (
                f"Ambiguous: '{label}' matches {len(rivals)} nodes in different files.\n"
                f"{listing}\n"
                "Retry with the repo-relative path or the full node id."
            )
        nid = matches[0]
        lines = [f"Neighbors of {sanitize_label(G.nodes[nid].get('label', nid))}:"]
        def _edge_at(d: dict) -> str:
            # Edge location = the relation SITE (call/import line) in the source
            # node's file, not a def line (#BUG1).
            loc = str(d.get("source_location") or "")
            return (
                f" at={sanitize_label(str(d.get('source_file') or ''))}:{sanitize_label(loc)}"
                if loc else ""
            )
        for nb in G.successors(nid):
            d = edge_data(G, nid, nb)
            rel = d.get("relation", "")
            if rel_filter and rel_filter not in rel.lower():
                continue
            lines.append(
                f"  --> {sanitize_label(G.nodes[nb].get('label', nb))} "
                f"[{sanitize_label(str(rel))}] [{sanitize_label(str(d.get('confidence', '')))}]{_edge_at(d)}"
            )
        for nb in G.predecessors(nid):
            d = edge_data(G, nb, nid)
            rel = d.get("relation", "")
            if rel_filter and rel_filter not in rel.lower():
                continue
            lines.append(
                f"  <-- {sanitize_label(G.nodes[nb].get('label', nb))} "
                f"[{sanitize_label(str(rel))}] [{sanitize_label(str(d.get('confidence', '')))}]{_edge_at(d)}"
            )
        budget = int(arguments.get("token_budget", 2000))
        return _cut_lines_to_budget(
            lines, budget, "Narrow with relation_filter or use get_node for a specific symbol"
        )

    def _tool_get_community(arguments: dict) -> str:
        cid = int(arguments["community_id"])
        nodes = communities.get(cid, [])
        if not nodes:
            return f"Community {cid} not found."
        header = _community_header(cid, G.nodes[nodes[0]].get("community_name"))
        lines = [f"{header} ({len(nodes)} nodes):"]
        for n in nodes:
            d = G.nodes[n]
            # Sanitise label and source_file (F-010).
            lines.append(
                f"  {sanitize_label(d.get('label', n))} "
                f"[{sanitize_label(str(d.get('source_file', '')))}]"
            )
        budget = int(arguments.get("token_budget", 2000))
        return _cut_lines_to_budget(
            lines, budget, "Raise token_budget or use get_node for specific members"
        )

    def _tool_god_nodes(arguments: dict) -> str:
        from graphify.analyze import god_nodes as _god_nodes
        nodes = _god_nodes(G, top_n=int(arguments.get("top_n", 10)))
        lines = ["God nodes (most connected):"]
        lines += [f"  {i}. {n['label']} - {n['degree']} edges" for i, n in enumerate(nodes, 1)]
        return "\n".join(lines)

    def _tool_graph_stats(_: dict) -> str:
        confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
        total = len(confs) or 1
        return (
            f"Nodes: {G.number_of_nodes()}\n"
            f"Edges: {G.number_of_edges()}\n"
            f"Communities: {len(communities)}\n"
            f"EXTRACTED: {round(confs.count('EXTRACTED')/total*100)}%\n"
            f"INFERRED: {round(confs.count('INFERRED')/total*100)}%\n"
            f"AMBIGUOUS: {round(confs.count('AMBIGUOUS')/total*100)}%\n"
        )

    def _tool_shortest_path(arguments: dict) -> str:
        src_scored = _score_nodes(G, [t.lower() for t in arguments["source"].split()])
        tgt_scored = _score_nodes(G, [t.lower() for t in arguments["target"].split()])
        if not src_scored:
            return f"No node matching source '{arguments['source']}' found."
        if not tgt_scored:
            return f"No node matching target '{arguments['target']}' found."
        src_nid = _pick_scored_endpoint(G, src_scored, arguments["source"])
        tgt_nid = _pick_scored_endpoint(G, tgt_scored, arguments["target"])
        # Ambiguity guard: when both queries resolve to the same node, the
        # shortest path is trivially zero hops, which is almost never what the
        # caller wanted (see bug #828).
        if src_nid == tgt_nid:
            return (
                f"'{arguments['source']}' and '{arguments['target']}' both resolved to "
                f"the same node '{src_nid}'. Use a more specific label or the exact node ID."
            )
        warnings: list[str] = []
        for name, scored, nid in (
            ("source", src_scored, src_nid),
            ("target", tgt_scored, tgt_nid),
        ):
            # Only meaningful when the raw score head is what got picked — a
            # full-token override was chosen on token coverage, not score.
            if len(scored) >= 2 and nid == scored[0][1]:
                top, runner = scored[0][0], scored[1][0]
                if top > 0 and (top - runner) / top < 0.10:
                    warnings.append(
                        f"warning: {name} match was ambiguous "
                        f"(top score {top:g}, runner-up {runner:g})"
                    )
        max_hops = int(arguments.get("max_hops", 8))
        try:
            # Deterministic path (#2074): the hash-seeded undirected view picked an
            # arbitrary route among equal-length paths. Build a sorted, materialized
            # undirected graph so the chosen path is canonical. Serve's shared G is
            # left untouched (its degree feeds query-seed tie-breaks).
            _und = nx.Graph()
            _und.add_nodes_from(sorted(G.nodes))
            _und.add_edges_from(sorted((min(u, v), max(u, v)) for u, v in G.edges()))
            path_nodes = nx.shortest_path(_und, src_nid, tgt_nid)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return f"No path found between '{G.nodes[src_nid].get('label', src_nid)}' and '{G.nodes[tgt_nid].get('label', tgt_nid)}'."
        hops = len(path_nodes) - 1
        if hops > max_hops:
            return f"Path exceeds max_hops={max_hops} ({hops} hops found)."
        segments = []
        for i in range(len(path_nodes) - 1):
            u, v = path_nodes[i], path_nodes[i + 1]
            # Report the actual stored relation(s), never a fabricated `calls`;
            # fall back to an honest "related" when the edge has no relation (#2074).
            if G.has_edge(u, v):
                datas = edge_datas(G, u, v)
                forward = True
            else:
                datas = edge_datas(G, v, u)
                forward = False
            rels = sorted({d.get("relation") for d in datas if d.get("relation")})
            rel = "/".join(rels) if rels else "related"
            confs = sorted({d.get("confidence") for d in datas if d.get("confidence")})
            conf_str = f" [{'/'.join(confs)}]" if confs else ""
            if i == 0:
                segments.append(G.nodes[u].get("label", u))
            if forward:
                segments.append(f"--{rel}{conf_str}--> {G.nodes[v].get('label', v)}")
            else:
                segments.append(f"<--{rel}{conf_str}-- {G.nodes[v].get('label', v)}")
        prefix = ("\n".join(warnings) + "\n") if warnings else ""
        return prefix + f"Shortest path ({hops} hops):\n  " + " ".join(segments)

    def _tool_list_prs(arguments: dict) -> str:
        from graphify.prs import fetch_prs, fetch_worktrees, format_prs_text, _detect_default_branch
        repo = arguments.get("repo") or None
        base = arguments.get("base") or _detect_default_branch(repo)
        try:
            prs = fetch_prs(repo=repo, base=base)
        except RuntimeError as e:
            return f"Error: {e}"
        worktrees = fetch_worktrees()
        for pr in prs:
            pr.worktree_path = worktrees.get(pr.branch)
        return format_prs_text(prs, base)

    def _tool_get_pr_impact(arguments: dict) -> str:
        from graphify.prs import fetch_pr_files, compute_pr_impact, _gh, _parse_ci
        number = int(arguments["pr_number"])
        repo = arguments.get("repo") or None
        # Use gh pr view directly — works for any base branch, not just the default
        view_args = ["pr", "view", str(number), "--json",
                     "title,headRefName,baseRefName,author,isDraft,reviewDecision,statusCheckRollup,updatedAt"]
        if repo:
            view_args += ["--repo", repo]
        pr_data = _gh(*view_args)
        if pr_data is None:
            return f"PR #{number} not found or gh not authenticated."
        files = fetch_pr_files(number, repo)
        if not files:
            return f"PR #{number}: no changed files found (may require gh auth)."
        comms, nodes = compute_pr_impact(files, G)
        ci = _parse_ci(pr_data.get("statusCheckRollup") or [])
        lines = [
            f"PR #{number}: {pr_data['title']}",
            f"CI: {ci}  Review: {pr_data.get('reviewDecision') or 'none'}",
            f"Base: {pr_data['baseRefName']}  Author: {(pr_data.get('author') or {}).get('login', '?')}",
            f"\nGraph impact: {nodes} nodes across {len(comms)} communities",
            f"Communities touched: {comms}",
            f"Files changed ({len(files)}):",
        ]
        lines += [f"  {f}" for f in files[:20]]
        if len(files) > 20:
            lines.append(f"  … and {len(files) - 20} more")
        return "\n".join(lines)

    def _tool_triage_prs(arguments: dict) -> str:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from graphify.prs import fetch_prs, fetch_worktrees, fetch_pr_files, compute_pr_impact, _STATUS_ORDER, _detect_default_branch
        repo = arguments.get("repo") or None
        base = arguments.get("base") or _detect_default_branch(repo)
        try:
            prs = fetch_prs(repo=repo, base=base)
        except RuntimeError as e:
            return f"Error: {e}"
        worktrees = fetch_worktrees()
        for pr in prs:
            pr.worktree_path = worktrees.get(pr.branch)
        actionable = [p for p in prs if p.base_branch == base and p.status not in ("WRONG-BASE", "STALE")]
        if not actionable:
            return f"No actionable PRs targeting {base}."
        # Fetch diffs concurrently then compute graph impact using in-memory G
        workers = min(8, len(actionable))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_pr = {pool.submit(fetch_pr_files, pr.number, repo): pr for pr in actionable}
            for fut in as_completed(future_to_pr):
                pr = future_to_pr[fut]
                try:
                    files = fut.result()
                except Exception:
                    files = []
                if files:
                    pr.files_changed = files
                    pr.communities_touched, pr.nodes_affected = compute_pr_impact(files, G)
        header = (
            f"Actionable PRs targeting {base}: {len(actionable)}\n"
            "Rank these by review priority. Higher blast_radius = more graph communities affected = higher merge risk.\n"
        )
        lines = [header]
        for p in sorted(actionable, key=lambda x: (_STATUS_ORDER.index(x.status) if x.status in _STATUS_ORDER else 99)):
            impact = f"  blast_radius={p.blast_radius}" if p.blast_radius else ""
            wt = f"  worktree={p.worktree_path}" if p.worktree_path else ""
            lines.append(
                f"PR #{p.number} [{p.status}] CI={p.ci_status} review={p.review_decision or 'none'} "
                f"age={p.days_old}d author={p.author}{impact}{wt}\n  title: {p.title}"
            )
        return "\n\n".join(lines)

    _handlers = {
        "query_graph": _tool_query_graph,
        "get_node": _tool_get_node,
        "get_neighbors": _tool_get_neighbors,
        "get_community": _tool_get_community,
        "god_nodes": _tool_god_nodes,
        "graph_stats": _tool_graph_stats,
        "shortest_path": _tool_shortest_path,
        "list_prs": _tool_list_prs,
        "get_pr_impact": _tool_get_pr_impact,
        "triage_prs": _tool_triage_prs,
    }

    def _load_community_labels() -> dict[int, str]:
        labels_path = Path(active_graph_path).parent / ".graphify_labels.json"
        if labels_path.exists():
            try:
                return {int(k): v for k, v in json.loads(labels_path.read_text(encoding="utf-8")).items()}
            except Exception:
                pass
        return {cid: f"Community {cid}" for cid in communities}

    async def list_resources() -> list[types.Resource]:
        # Plain-string URIs on purpose: mcp 1.x types the field as AnyUrl and
        # coerces strings, mcp 2.x types it as str and REJECTS AnyUrl objects.
        return [
            types.Resource(uri="graphify://report", name="Graph Report", description="Full GRAPH_REPORT.md", mimeType="text/markdown"),
            types.Resource(uri="graphify://stats", name="Graph Stats", description="Node/edge/community counts and confidence breakdown", mimeType="text/plain"),
            types.Resource(uri="graphify://god-nodes", name="God Nodes", description="Top 10 most-connected nodes", mimeType="text/plain"),
            types.Resource(uri="graphify://surprises", name="Surprising Connections", description="Cross-community surprising connections", mimeType="text/plain"),
            types.Resource(uri="graphify://audit", name="Confidence Audit", description="EXTRACTED/INFERRED/AMBIGUOUS edge breakdown", mimeType="text/plain"),
            types.Resource(uri="graphify://questions", name="Suggested Questions", description="Suggested questions for this codebase", mimeType="text/plain"),
        ]

    async def read_resource(uri: AnyUrl) -> str:
        _select_graph(None)  # resources read the server's default graph
        uri_str = str(uri)
        if uri_str == "graphify://report":
            report_path = Path(active_graph_path).parent / "GRAPH_REPORT.md"
            if report_path.exists():
                return report_path.read_text(encoding="utf-8")
            return "GRAPH_REPORT.md not found. Run graphify extract first."
        if uri_str == "graphify://stats":
            return _tool_graph_stats({})
        if uri_str == "graphify://god-nodes":
            return _tool_god_nodes({"top_n": 10})
        if uri_str == "graphify://surprises":
            try:
                from graphify.analyze import surprising_connections
                surprises = surprising_connections(G, communities, top_n=10)
                if not surprises:
                    return "No surprising connections found."
                lines = ["Surprising cross-community connections:"]
                for s in surprises:
                    lines.append(f"  {s.get('source', '')} <-> {s.get('target', '')} [{s.get('relation', '')}]")
                return "\n".join(lines)
            except Exception as exc:
                return f"Could not compute surprising connections: {exc}"
        if uri_str == "graphify://audit":
            confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
            total = len(confs) or 1
            return (
                f"Total edges: {total}\n"
                f"EXTRACTED: {confs.count('EXTRACTED')} ({round(confs.count('EXTRACTED')/total*100)}%)\n"
                f"INFERRED: {confs.count('INFERRED')} ({round(confs.count('INFERRED')/total*100)}%)\n"
                f"AMBIGUOUS: {confs.count('AMBIGUOUS')} ({round(confs.count('AMBIGUOUS')/total*100)}%)\n"
            )
        if uri_str == "graphify://questions":
            try:
                from graphify.analyze import suggest_questions
                community_labels = _load_community_labels()
                questions = suggest_questions(G, communities, community_labels, top_n=10)
                if not questions:
                    return "No suggested questions available."
                lines = ["Suggested questions:"]
                for q in questions:
                    if isinstance(q, dict):
                        lines.append(f"  - {q.get('question', '')}")
                    else:
                        lines.append(f"  - {q}")
                return "\n".join(lines)
            except Exception as exc:
                return f"Could not generate questions: {exc}"
        raise ValueError(f"Unknown resource: {uri_str}")

    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        arguments = dict(arguments or {})
        project_path = arguments.pop("project_path", None)
        handler = _handlers.get(name)
        if not handler:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
        try:
            _select_graph(project_path)  # bind G/communities to the target graph
            return [types.TextContent(type="text", text=handler(arguments))]
        except Exception as exc:
            return [types.TextContent(type="text", text=f"Error executing {name}: {exc}")]

    if hasattr(Server, "list_tools"):
        # mcp 1.x: decorator-based registration. The SDK wraps the raw returns
        # (list[Tool] -> ListToolsResult, str -> resource contents) itself.
        server = Server("graphify")
        server.list_tools()(list_tools)
        server.call_tool()(call_tool)
        server.list_resources()(list_resources)
        server.read_resource()(read_resource)
    else:
        # mcp 2.x: handlers ride the Server constructor as on_* callbacks with
        # the (ctx, params) -> Result contract, so wrap the same impls and
        # build the result models the 1.x decorators used to build for us.
        async def _on_list_tools(ctx, params) -> types.ListToolsResult:
            return types.ListToolsResult(tools=await list_tools())

        async def _on_call_tool(ctx, params) -> types.CallToolResult:
            content = await call_tool(params.name, dict(params.arguments or {}))
            return types.CallToolResult(content=content)

        async def _on_list_resources(ctx, params) -> types.ListResourcesResult:
            return types.ListResourcesResult(resources=await list_resources())

        async def _on_read_resource(ctx, params) -> types.ReadResourceResult:
            text = await read_resource(params.uri)
            mime = "text/markdown" if str(params.uri).startswith("graphify://report") else "text/plain"
            return types.ReadResourceResult(
                contents=[types.TextResourceContents(uri=params.uri, mimeType=mime, text=text)]
            )

        try:
            from importlib.metadata import version as _pkg_version
            _version = _pkg_version("graphifyy")
        except Exception:
            _version = "0"
        server = Server(
            "graphify",
            version=_version,
            on_list_tools=_on_list_tools,
            on_call_tool=_on_call_tool,
            on_list_resources=_on_list_resources,
            on_read_resource=_on_read_resource,
        )

    return server


def serve(graph_path: str | None = None) -> None:
    """Start the MCP server over stdio (the default, per-developer transport)."""
    graph_path = graph_path or _default_graph_json()
    try:
        from mcp.server.stdio import stdio_server
    except ImportError as e:
        raise ImportError('mcp not installed. Run: pip install "graphifyy[mcp]"') from e
    import asyncio

    server = _build_server(graph_path)

    async def main() -> None:
        async with stdio_server() as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    _filter_blank_stdin()
    asyncio.run(main())


class _MCPASGIApp:
    """Raw-ASGI wrapper around the Streamable HTTP session manager.

    Passed to a Starlette ``Route`` as a class instance (not a function) so
    Starlette treats it as an ASGI app: it serves the exact mount path for all
    methods (GET/POST/DELETE) with no request/response wrapping and no
    trailing-slash redirect — mirroring how FastMCP mounts the same manager.
    """

    def __init__(self, manager) -> None:
        self._manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self._manager.handle_request(scope, receive, send)


class _ApiKeyMiddleware:
    """Pure-ASGI API-key gate for the HTTP transport.

    Implemented as raw ASGI (not Starlette's BaseHTTPMiddleware) on purpose:
    BaseHTTPMiddleware buffers responses and breaks the Streamable HTTP SSE
    stream. This short-circuits with 401 before the request ever reaches the
    session manager, leaving the streaming path untouched for authorized calls.
    """

    def __init__(self, app, api_key: str) -> None:
        self.app = app
        self._expected = api_key.encode("utf-8")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        import hmac
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"x-api-key")
        if provided is None:
            # RFC 6750: the auth scheme token is case-insensitive.
            scheme, _, token = headers.get(b"authorization", b"").partition(b" ")
            if scheme.lower() == b"bearer" and token:
                provided = token.strip()
        # Constant-time compare; reject when no key was supplied at all.
        if provided is None or not hmac.compare_digest(provided, self._expected):
            body = b'{"error": "unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _build_http_app(
    graph_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    api_key: str | None = None,
    path: str = "/mcp",
    json_response: bool = False,
    stateless: bool = False,
    session_timeout: float | None = 3600.0,
):
    """Build the Starlette ASGI app for the Streamable HTTP transport.

    Split out from :func:`serve_http` (which blocks on uvicorn) so the wiring
    can be exercised with an in-process ASGI test client.

    ``session_timeout`` reaps stateful sessions idle for that many seconds so a
    long-running shared server does not leak memory when IDE clients disconnect
    without sending a DELETE. ``None`` (or <= 0) disables reaping; it is forced
    to ``None`` in stateless mode, which has no sessions to reap.
    """
    try:
        import contextlib

        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Route

        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError as e:
        raise ImportError(
            'HTTP transport needs the mcp extra (mcp + starlette + uvicorn). '
            'Run: pip install "graphifyy[mcp]"'
        ) from e

    # A blank key (e.g. --api-key "" or an empty GRAPHIFY_API_KEY) must not be
    # mistaken for "auth on" — normalize it to None so the gate is unambiguous.
    api_key = (api_key or "").strip() or None

    server = _build_server(graph_path)

    # DNS-rebinding protection. When the operator binds a wildcard address they
    # are intentionally exposing the server, so accept any Host header; for a
    # loopback/specific bind, restrict Host to that address (with and without
    # the port) plus the localhost aliases.
    if host in ("0.0.0.0", "::", ""):
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        allowed = {host, "localhost", "127.0.0.1"}
        allowed |= {f"{h}:{port}" for h in list(allowed)}
        security = TransportSecuritySettings(allowed_hosts=sorted(allowed))

    # The SDK rejects a non-positive timeout and forbids one in stateless mode.
    idle_timeout = None if (stateless or not session_timeout or session_timeout <= 0) else session_timeout

    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=json_response,
        stateless=stateless,
        security_settings=security,
        session_idle_timeout=idle_timeout,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # The session manager owns an anyio task group that must wrap the whole
        # server lifetime, so enter it here rather than per-request.
        async with manager.run():
            yield

    middleware = []
    if api_key:
        middleware.append(Middleware(_ApiKeyMiddleware, api_key=api_key))

    return Starlette(
        routes=[Route(path, endpoint=_MCPASGIApp(manager))],
        middleware=middleware,
        lifespan=lifespan,
    )


def serve_http(
    graph_path: str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    api_key: str | None = None,
    path: str = "/mcp",
    json_response: bool = False,
    stateless: bool = False,
    session_timeout: float | None = 3600.0,
) -> None:
    """Start the MCP server over Streamable HTTP (MCP spec 2025-03-26).

    Serves the same tools/resources as the stdio transport, so a single shared
    process can host the graph for a whole team. Clients point their IDE MCP
    config at ``http://<host>:<port><path>`` (default ``/mcp``).

    ``api_key`` (or the ``GRAPHIFY_API_KEY`` env var) enables a simple header
    check (``Authorization: Bearer <key>`` or ``X-API-Key: <key>``). OAuth is a
    deliberate follow-up. Binding ``0.0.0.0`` exposes the server beyond
    localhost — set an api_key when you do.
    """
    graph_path = graph_path or _default_graph_json()
    try:
        import uvicorn
    except ImportError as e:
        raise ImportError(
            'HTTP transport needs the mcp extra (mcp + starlette + uvicorn). '
            'Run: pip install "graphifyy[mcp]"'
        ) from e

    api_key = (api_key or "").strip() or None

    app = _build_http_app(
        graph_path,
        host=host,
        port=port,
        api_key=api_key,
        path=path,
        json_response=json_response,
        stateless=stateless,
        session_timeout=session_timeout,
    )

    auth_note = "api-key required" if api_key else "no auth (set --api-key to require one)"
    print(
        f"graphify MCP server (streamable-http) on http://{host}:{port}{path} - {auth_note}",
        file=sys.stderr,
    )
    if host in ("0.0.0.0", "::", "") and not api_key:
        print(
            f"WARNING: binding {host or '0.0.0.0'} with no api-key exposes the graph "
            "unauthenticated on the network. Set --api-key (or GRAPHIFY_API_KEY).",
            file=sys.stderr,
        )
    uvicorn.run(app, host=host, port=port)


def _main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="python -m graphify.serve",
        description="Serve a graphify knowledge graph over MCP (stdio or Streamable HTTP).",
    )
    parser.add_argument(
        "graph_path",
        nargs="?",
        default=None,
        help="Path to graph.json (default: graphify-out/graph.json)",
    )
    parser.add_argument(
        "--graph",
        dest="graph_flag",
        default=None,
        metavar="PATH",
        help="Path to graph.json — alias for the positional argument",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport to serve on (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port (default: 8080)")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GRAPHIFY_API_KEY"),
        help="Require this key on the HTTP transport (env: GRAPHIFY_API_KEY)",
    )
    parser.add_argument("--path", default="/mcp", help="HTTP mount path (default: /mcp)")
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="Return plain JSON responses instead of SSE streams",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="Run without per-session state (for load-balanced / CI deployments)",
    )
    parser.add_argument(
        "--session-timeout",
        type=float,
        default=3600.0,
        help="Reap stateful sessions idle this many seconds (default: 3600; 0 disables)",
    )
    args = parser.parse_args(argv)
    graph_path = args.graph_flag or args.graph_path or _default_graph_json()

    if args.transport == "http":
        serve_http(
            graph_path,
            host=args.host,
            port=args.port,
            api_key=args.api_key,
            path=args.path,
            json_response=args.json_response,
            stateless=args.stateless,
            session_timeout=args.session_timeout,
        )
    else:
        serve(graph_path)


if __name__ == "__main__":
    _main()
