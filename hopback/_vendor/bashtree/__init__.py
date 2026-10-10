"""bashtree: bash command structure from tree-sitter-bash, shared by hopback
and bash-write-guard. See core.py (parse) and helpers.py (unwrap, nested)."""
from .core import (KINDS, OK, SYNTAX_ERROR, TOO_BIG, Ancestor, Assignment, Command,  # noqa: F401
                   Redirect, Result, Word, parse)
from .helpers import GitArgs, Nested, Unwrapped, git_args, nested, unwrap  # noqa: F401

__version__ = "0.1.0"
