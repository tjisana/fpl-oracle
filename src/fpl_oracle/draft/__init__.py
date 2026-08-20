"""Official FPL Draft support — a different game from classic FPL.

Draft is selection WITHOUT replacement: every Premier League player is owned by
exactly one manager in the league, so nothing in `solver/` applies. There is no
£100m budget (the ILP's whole reason to exist), no captain
(`squad.captains_disabled` is true in the draft API), and no 3-per-club limit.
What replaces the budget as the binding constraint is positional scarcity, and
the currency is value over the replacement STARTER at a player's own position.

`consensus/` does not transfer either: it scores votes inside £1m price bands,
which is exactly the wrong normalisation when there are no prices.
"""
