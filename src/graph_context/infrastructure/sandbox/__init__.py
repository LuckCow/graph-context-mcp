"""Sandboxed script execution for Automation Rules (WP32, ADR 040).

``bootstrap.py`` is the CHILD program -- self-contained, stdlib-only,
spawned as ``python -I -S bootstrap.py`` -- and ``runner.py`` is the
parent-side :class:`graph_context.ports.script_runner.ScriptRunner`
implementation that spawns, feeds, caps, and kills it.
"""
