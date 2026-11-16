"""Closure-1B: the native AnyCode baseline on the shared OpenSandbox environment.

N0 = native single, N1 = native multi over the same candidate DAG as the Atlas multi arm. Both run
AnyCode's own agent loop, DAG scheduler, task prompt and tool registry; only the six basic coding
tools' execution backend is the container. See ``docs/closure-1b-native-baseline.md``.
"""
