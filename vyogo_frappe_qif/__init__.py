# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

# No side effects on import: a host can import the parser, staging or worker
# without patching anything. On a site with the app installed, hooks.py
# installs the Bank Statement Import patches, and the worker sets up tenant
# stamping when it runs.
__version__ = "0.0.1"
