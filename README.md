# eval-lag

<!--
SKELETON. The section order below is fixed by the plan's §8 and is not a suggestion —
it is what a reviewer reads top to bottom. Fill each section; delete these comments.
Scope contract: ~/Desktop/job search/claude-resume-kit/output/Kraken/plan_proyecto_rust_ajedrez.md

Banned words anywhere in this file: arbitrage, trading bot, alpha, edge, profit, returns.
-->

<!-- 1. WHAT IT DOES — two sentences. No preamble. -->
TODO

<!-- 2. THE WHY — these two sentences, in this order. Edit the wording, keep the substance. -->
> I noticed game-by-game chess markets while betting on Norway Chess and wondered whether they lag
> the engine. This measures whether they do.
>
> It measures how long a prediction market takes to price information a chess engine already has.

## This places no orders

<!-- 3. THE DISCLAIMER — high up, explicit, non-negotiable. -->
This tool is read-only. It holds no API keys, touches no funds, and places no orders of any kind. It
subscribes to Polymarket's public market-data channel, which requires no authentication. Any P&L
figure discussed anywhere is hypothetical and computed after the fact.

## What it measured

<!-- 4. ONE REAL RESULT from the 2026 World Championship: lag in ms, price delta, which market.
     The raw JSONL goes in data/ and is committed. A null result goes here too, stated plainly. -->
TODO

## Running it

<!-- 5. Live mode and replay mode. Replay must work with no network and no engine — that is the
     demo that works when no game is on. -->
TODO

## Non-goals

<!-- 6. Copy the right-hand column of the plan's §3 table. Being explicit about what this does not
     do is the point of the section. -->
- Places no orders; no authenticated endpoints, keys or funds
- No decision rule on the only-move gap — the gap is recorded, not acted on
- No database, dashboard, frontend or deployment
- One tournament round at a time; no generic market indexer
- No engine strength tuning or adaptive search depth

## This is my first Rust project. Design notes

<!-- 7. Three or four short paragraphs on the ACTUAL decisions. If you cannot write the paragraph,
     delete the code it describes and write the simpler version you can explain.
     Suggested topics, from the ticket questions:
       - channels vs shared state between the feeds and the engine
       - what happens when one of the three sources dies
       - why the engine is a subprocess rather than a linked library
       - why every record carries both a monotonic and a wall-clock timestamp
-->
TODO

## License

GPL-3.0, inherited from `shakmaty` and `pgn-reader`.
