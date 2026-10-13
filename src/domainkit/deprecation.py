"""How domainkit retires an API (the policy is in ``docs/versioning.md``).

A deprecated API keeps working for at least one minor release and warns with
``DomainkitDeprecationWarning`` each time it is used. Mark it with the
standard decorator, which type checkers also report::

    @warnings.deprecated(
        "Use Page.items; removed in domainkit 0.4.",
        category=DomainkitDeprecationWarning,
    )
    def results(self) -> list[T]: ...

Projects find their uses before upgrading by turning the warning into an
error in their test settings::

    filterwarnings = ["error::domainkit.deprecation.DomainkitDeprecationWarning"]
"""


class DomainkitDeprecationWarning(DeprecationWarning):
    """A domainkit API that a later release removes; the message says what to use."""
