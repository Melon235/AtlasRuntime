"""Stage C1: one real coding-delegation positive control, prepared and not executed.

The stage reuses the Stage B+ execution stack (plan record, authority contract, one-shot claim,
``executor.dispatch``, ``audit_cell``, the Closure-2 fixture/oracle) and adds exactly one new plan
identity plus one user-goal appendix. Nothing in ``src/atlasos`` changes.
"""
