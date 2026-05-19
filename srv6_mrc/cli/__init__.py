"""srv6_mrc.cli — user-facing command-line entry points.

Thin wrappers around srv6_mrc library modules. Each `main()` is
wired to a console-script entry in pyproject.toml so it ends up at
`/usr/local/bin/<name>` in the host-image Docker container.
"""
