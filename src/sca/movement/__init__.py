"""Coin movement simulator — predict, attribute, score, narrate.

The four-job pipeline lives here:
  1. predict.py  — emit a forecast (deterministic math, no LLM)
  2. attribute.py — write the cited 'why' paragraph (LLM, audited)
  3. resolve.py  — grade aged-out predictions against reality
  4. ticker.py   — drive 1 + 2 on a configurable cadence

The product's defensibility is the calibration archive — predictions
are append-only, addressable by id, paired with a single resolution
row when reality catches up. Nothing here invents data; the LLM is
strictly a prose layer over numbers the deterministic engine produced.
"""
