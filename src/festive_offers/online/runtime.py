"""Stable entry point for the separately configured, composed and operated runtime."""

from __future__ import annotations

from .runtime_factory import Runtime, RuntimeStore, build_runtime, configured_tokens

__all__ = ["Runtime", "RuntimeStore", "build_runtime", "configured_tokens", "main"]


def main() -> None:
    from .runtime_cli import main as run_cli

    run_cli()


if __name__ == "__main__":
    main()
