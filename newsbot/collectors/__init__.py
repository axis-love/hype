"""News candidate collectors.

Each collector fetches from one source type and returns a list of
Candidate dicts (see base.py). All collectors share the same return shape
so the pipeline can score, dedupe, and summarize uniformly.
"""