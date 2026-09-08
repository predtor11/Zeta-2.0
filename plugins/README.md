# External plugins

Drop a folder here containing an `__init__.py` that exposes a `PLUGIN` object; Zeta loads it at
startup and registers its tools under the plugin's name as a permission category.

See `example_hello/` for the smallest possible plugin and `docs/PLUGINS.md` for the full guide.
