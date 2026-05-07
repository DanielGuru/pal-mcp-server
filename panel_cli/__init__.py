"""Panel CLI helpers (install-hooks, uninstall-hooks, inbox-drain).

Each is a separate console_scripts entry point in pyproject.toml so the
hook script and install commands can be invoked directly without a
dispatcher. This package collects them in one namespace for clarity.
"""
