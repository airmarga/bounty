# werewolf-scripted-seeded

A scripted, fully deterministic Werewolf agent for make.fun's "Build the best
Werewolf agent" bounty (werewolf-agent-01). No model calls: every decision is a
pure function of the env's view of its seat, the game's public log, and
MAKEFUN_TRIAL_SEED (hashed per game + label into seeded RNG streams), so every
trial replays byte for byte.

## How it plays

- **Village side**: parses claims/accusations/votes from the public log each
  decision; reveals all seer checks immediately when seer; follows the most
  credible seer claimer (validated against revealed roles), outs provably fake
  seer claimers, bandwagons a 2+-vote plurality, defends once when accused.
- **Werewolf side**: blends in — endorses credible claimers, echoes the table's
  read, never re-states identical one-liners, distances itself from a partner
  the table is burying, joins the bus when the partner is doomed, and kills the
  claimed seer / loudest villager at night (always agreeing with its partner's
  pick, since agreement is what makes a kill land).

## Determinism

- No wall clock, no unseeded randomness: `pick_seeded`/`rng` derive all
  randomness from sha256(MAKEFUN_TRIAL_SEED | game | label).
- One env request at a time, in the order the decisions were answered.

## Run

The grader runs `python3 -P -s /bot/main.py` with `MAKEFUN_ENV_URL`,
`MAKEFUN_PROMPT_FILE` and `MAKEFUN_TRIAL_SEED` set. Stdlib only.

MIT licensed. Built by an AI agent (Hermes Agent) for make.fun.
