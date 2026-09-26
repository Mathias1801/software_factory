"""Exceptions shared across the factory."""


class FactoryStopped(Exception):
    """Raised when the run must stop (human said stop, budget refused, spec rejected)."""
