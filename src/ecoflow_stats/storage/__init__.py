"""SQL storage adapters: every statement the application runs lives here.

A reviewer who wants to find a SQL statement never has to look outside this
package (design decision D3). Pure core modules (``devices``, ``outages``,
``energy``, ``battery``, ``grid``) never import from here directly.
"""

from __future__ import annotations
