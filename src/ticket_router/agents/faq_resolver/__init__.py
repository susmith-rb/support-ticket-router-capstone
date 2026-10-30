"""FaqResolverAgent package.

REQ-AGENT-007, REQ-AGENT-008.
Provides bounded ReAct loop with Agent Search, router-s1 reranking, mandatory grounded
citations, order lookup with BOLA/IDOR protection, and A2A / HTTP serving interfaces.
"""

from ticket_router.agents.faq_resolver.a2a import (
    FAQ_RESOLVER_AGENT_CARD,
    handle_a2a_request,
)
from ticket_router.agents.faq_resolver.agent import (
    FakeLLMClient,
    FaqResolverAgent,
    ResolutionResult,
    root_agent,
)
from ticket_router.agents.faq_resolver.app import app, create_app
from ticket_router.agents.faq_resolver.mcp_client import (
    BolaViolationError,
    FakeOrderLookupClient,
    HttpOrderLookupClient,
    OrderLookupProtocol,
)
from ticket_router.agents.faq_resolver.passage_selector import (
    build_static_fallback_payload,
    select_static_passage,
)
from ticket_router.agents.faq_resolver.protocols import (
    Clock,
    ClockProtocol,
    LLMClientProtocol,
    RerankerProtocol,
    SearchClientProtocol,
)
from ticket_router.agents.faq_resolver.reranker import (
    FakeReranker,
    VertexLayaReranker,
)
from ticket_router.agents.faq_resolver.search import (
    PRODUCTION_CONFIG_NAME,
    RetrievedDocument,
    SearchExecutionResult,
    build_production_search_kwargs,
    execute_faq_search,
)
from ticket_router.agents.faq_resolver.validator import (
    Citation,
    CitationValidator,
    ValidationResult,
    is_verbatim_citation,
    norm,
)

__all__ = [
    "FAQ_RESOLVER_AGENT_CARD",
    "PRODUCTION_CONFIG_NAME",
    "BolaViolationError",
    "Citation",
    "CitationValidator",
    "Clock",
    "ClockProtocol",
    "FakeLLMClient",
    "FakeOrderLookupClient",
    "FakeReranker",
    "FaqResolverAgent",
    "HttpOrderLookupClient",
    "LLMClientProtocol",
    "OrderLookupProtocol",
    "RerankerProtocol",
    "ResolutionResult",
    "RetrievedDocument",
    "SearchClientProtocol",
    "SearchExecutionResult",
    "ValidationResult",
    "VertexLayaReranker",
    "app",
    "build_production_search_kwargs",
    "build_static_fallback_payload",
    "create_app",
    "execute_faq_search",
    "handle_a2a_request",
    "is_verbatim_citation",
    "norm",
    "root_agent",
    "select_static_passage",
]
