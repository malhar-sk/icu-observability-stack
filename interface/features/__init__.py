"""Every .py file in this folder is loaded, and each @feature function in it becomes a tool (MCP) and an endpoint (API).

To add a question: copy a small file here, change the function. To remove one: delete its file, or set
FLEET_INTERFACE_DISABLE=<name> to switch it off without deleting. Nothing else needs editing.
"""
import importlib
import pkgutil

for _module in sorted(m.name for m in pkgutil.iter_modules(__path__)):
    importlib.import_module(f"{__name__}.{_module}")
