from __future__ import annotations

import importlib
import pkgutil

_TOOLS_LOADED = False


def load_builtin_tools() -> None:
	"""Import all modules in tool_registry.tools to trigger self-registration."""
	global _TOOLS_LOADED
	if _TOOLS_LOADED:
		return

	tools_pkg_name = "tool_registry.tools"
	tools_pkg = importlib.import_module(tools_pkg_name)

	for module_info in pkgutil.iter_modules(tools_pkg.__path__):
		if module_info.ispkg or module_info.name.startswith("_"):
			continue
		importlib.import_module(f"{tools_pkg_name}.{module_info.name}")

	_TOOLS_LOADED = True
