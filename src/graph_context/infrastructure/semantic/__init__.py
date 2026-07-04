"""Semantic-layer adapters (ADR 014): embedders and the vector cache.

Everything here is projection machinery -- disposable, rebuildable,
never truth. The real model-backed embedders (sentence-transformers,
Voyage) arrive with the container rebuild; :class:`HashingEmbedder`
keeps the whole layer runnable and deterministic before it.
"""
