"""WorkGrid: deterministic offline scheduling for a batch of dependent tasks.

Three packages, each usable on its own:

* :mod:`workgrid.dependencies` -- graph structure, validation, stable order;
* :mod:`workgrid.resources` -- capacity, availability windows, constraints;
* :mod:`workgrid.scheduler` -- the shared plan built from both.
"""

__version__ = "1.4.0"
