"""Deciding whether something a user pointed NOVA at could become a capability.

Nothing here executes what it examines. "NOVA, learn this folder" is the most
dangerous sentence in the product, and the obvious implementation -- fetch,
import, see what happens -- is remote code execution wearing a friendly hat.
Importing a module *is* running it: top-level code executes on import, before
any check could intervene.
"""
