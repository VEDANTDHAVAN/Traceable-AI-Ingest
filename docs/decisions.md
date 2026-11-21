# Decision log

- Initial implementation is Python 3.11+ with async HTTP adapters and Pydantic validation.
- Initial storage interface will be abstracted; production database choice is not hard-coded until operational constraints are known.
- Anti-bot protections will not be bypassed; use authorized access paths or omit the source.
