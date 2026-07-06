"""Semantic-layer adapters (ADR 014): embedders and the vector cache.

Everything here is projection machinery -- disposable, rebuildable,
never truth. :class:`SentenceTransformerEmbedder` is the model-backed
embedder (the model rides in the container image; ADR 014);
:class:`HashingEmbedder` keeps the layer runnable and deterministic
without it (tests, CI, and any environment without the baked model).
"""
