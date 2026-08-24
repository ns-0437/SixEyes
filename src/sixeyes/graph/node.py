"""Nodes: the only unit of capability in Sextant.

Rule 4 of CLAUDE.md — a new capability is a new node, never a new branch inside an
existing one. Nodes declare typed ports so that a mis-wired graph fails at build time
rather than forty minutes into a run over a customer's trace export.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ClassVar, Mapping, get_type_hints

from sixeyes.core.errors import GraphBuildError
from sixeyes.core.ids import Digest, content_hash

if TYPE_CHECKING:
    from sixeyes.graph.context import RunContext


class NodeKind(str, Enum):
    """Determines caching, parallelism, and whether output may be certified.

    This enum is where product strategy meets the runtime. ``STOCHASTIC`` output is
    permanently tainted and can never back a certified finding, which makes it
    structurally impossible for an LLM to drift into the detection path.
    """

    SOURCE = "source"          # reads the outside world; cacheable by declared fingerprint
    PURE = "pure"              # total function of its inputs; freely cacheable
    RESIDENT = "resident"      # owns state across runs; addressed by identity, not content
    STOCHASTIC = "stochastic"  # non-deterministic (LLM); output is tainted forever

    @property
    def is_cacheable(self) -> bool:
        return self in (NodeKind.SOURCE, NodeKind.PURE)

    @property
    def taints(self) -> bool:
        return self is NodeKind.STOCHASTIC


class Node(ABC):
    """Base class for every node.

    Subclasses declare ``kind``, ``version``, ``inputs`` and ``output`` as class
    attributes, then implement ``execute``.

    ``version`` is a cache-invalidation lever: bump it whenever the node's behaviour
    changes, and the executor re-runs that node and everything downstream while leaving
    the rest of the graph's cache intact.
    """

    kind: ClassVar[NodeKind] = NodeKind.PURE
    version: ClassVar[str] = "1"
    inputs: ClassVar[Mapping[str, type]] = {}
    output: ClassVar[type] = object

    def __init__(self, node_id: str, **config: Any) -> None:
        if not node_id or not node_id.replace("_", "").replace(".", "").isalnum():
            raise GraphBuildError(
                f"node id {node_id!r} must be a non-empty alphanumeric/underscore/dot string"
            )
        self.node_id = node_id
        self.config = config

    @abstractmethod
    async def execute(self, ctx: RunContext, **inputs: Any) -> Any:
        """Compute this node's output. Must not mutate its inputs."""

    def config_key(self) -> Any:
        """Config contribution to the cache key. Override if config holds non-canonical
        objects such as open file handles."""
        return self.config

    def identity(self) -> Digest:
        """Everything about this node except its inputs."""
        return content_hash(
            type(self).__module__ + "." + type(self).__qualname__,
            self.node_id,
            self.version,
            self.kind.value,
            self.config_key(),
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.node_id!r} kind={self.kind.value} v{self.version}>"


class _FunctionNode(Node):
    """Backing class for the ``@node`` decorator."""

    _fn: ClassVar[Callable[..., Awaitable[Any]]]

    async def execute(self, ctx: RunContext, **inputs: Any) -> Any:
        return await type(self)._fn(ctx, **inputs)


def node(
    *,
    kind: NodeKind = NodeKind.PURE,
    version: str = "1",
    output: type,
    inputs: Mapping[str, type] | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], type[Node]]:
    """Turn an async function into a Node class.

    Input ports are read from the function signature (every parameter after ``ctx``);
    pass ``inputs`` explicitly only when annotations are unavailable.

        @node(output=Report, version="2")
        async def render(ctx, findings: FindingSet) -> Report: ...
    """

    def decorate(fn: Callable[..., Awaitable[Any]]) -> type[Node]:
        if not inspect.iscoroutinefunction(fn):
            raise GraphBuildError(f"node {fn.__name__!r} must be an async function")

        declared: dict[str, type]
        if inputs is not None:
            declared = dict(inputs)
        else:
            signature = inspect.signature(fn)
            params = list(signature.parameters.values())
            if not params or params[0].name != "ctx":
                raise GraphBuildError(
                    f"node {fn.__name__!r} must take 'ctx' as its first parameter"
                )
            # get_type_hints resolves string annotations, which is what every module
            # under `from __future__ import annotations` (all of them, see CLAUDE.md)
            # actually stores in __annotations__. Reading param.annotation directly
            # would hand us the literal string "int" instead of the type int, and the
            # port-type checker would then silently accept any wiring.
            try:
                hints = get_type_hints(fn)
            except NameError as exc:
                raise GraphBuildError(
                    f"node {fn.__name__!r} has an annotation that could not be resolved: {exc}"
                ) from exc
            declared = {}
            for param in params[1:]:
                if param.name not in hints:
                    raise GraphBuildError(
                        f"node {fn.__name__!r} parameter {param.name!r} needs a type annotation"
                    )
                declared[param.name] = hints[param.name]

        namespace: dict[str, Any] = {
            "kind": kind,
            "version": version,
            "inputs": declared,
            "output": output,
            "_fn": staticmethod(fn),
            "__doc__": fn.__doc__,
            "__module__": fn.__module__,
        }
        return type(fn.__name__, (_FunctionNode,), namespace)

    return decorate
