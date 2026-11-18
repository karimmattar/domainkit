"""Wallets domain: each customer's wallet is an account in double-entry books.

- ``controllers.py``: what the domain does. It opens wallets and moves money
  through ``domainkit.contrib.accounting``, which keeps the books.
- ``routes.py``: the HTTP API, and who may call it.
- ``apps.py``: ``manage.py seed wallets`` opens the books.

The domain has no table of its own: the books hold every wallet, every
movement, and every hold.
"""
