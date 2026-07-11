"""Behavioral eval harness for the LLM-driven orchestrator (WP16).

Runs scripted scenarios through the real ``Orchestrator.handle_message``
seam against the in-memory backend, grades the graph end-state and tool
trajectory, and writes comparable run reports. Not a test suite: live
runs spend Claude subscription quota and are nondeterministic, so the
entry point is ``python -m evals run`` -- pytest never collects this
package (only ``tests/evals`` exercises the plumbing, scripted).
"""
