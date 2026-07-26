"""The query layer — every ``select()`` against a model lives in here.

The contract, which the services rely on:

* Every function takes ``session: AsyncSession`` as its first positional
  argument. Never ``None``, never defaulted — opening a session is the
  caller's job, via ``scorekeeper.db.connection.session_scope``.
* A repository never commits, rolls back, or refreshes. The unit of work
  belongs to the service so that transaction boundaries stay visible there.
* A repository owns the *reads*: ``select()``, ``delete()``, ``session.get()``.
  Constructing entities, ``session.add``/``delete`` and mutating attributes stay
  in the service — wrapping those would only hide the unit of work.

Import from the submodules directly; this package re-exports nothing.
"""
