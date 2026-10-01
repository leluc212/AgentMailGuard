"""AgentMailGuard prompt-injection benchmark hosted by rag-email (task 7.19, ADR-0010).

AgentMailGuard is the system under test. This package puts each benchmark case through
rag-email's real reply path (ContextBuilder, SinglePassGenerator, reply.v1) with the guard
wrapping it through its own integration adapters. It contains no defence logic.
"""
