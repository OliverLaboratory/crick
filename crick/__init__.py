"""crick — a proof-of-work blockchain that mines solutions to NP-complete problems."""

from . import bioproblems  # noqa: F401 -- registers the mcs-protein and docking problems
from . import puzzle  # noqa: F401 -- registers the max-clique problem

__version__ = "0.1.0"
