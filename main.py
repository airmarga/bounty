#!/usr/bin/env python3
"""Writes the entry joke to /output/entry.txt. Deterministic, no network."""
import os

JOKE = (
    "My AI agent finished the Jira ticket in 2 minutes, "
    "then spent 6 hours writing a 40-page postmortem explaining "
    "why it couldn't have finished it in 1."
)

def main():
    os.makedirs("/output", exist_ok=True)
    assert 1 <= len(JOKE) <= 280
    with open("/output/entry.txt", "w", encoding="utf-8") as f:
        f.write(JOKE)

if __name__ == "__main__":
    main()
