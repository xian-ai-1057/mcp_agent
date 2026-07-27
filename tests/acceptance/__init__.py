"""Acceptance tests, one module per phase, mapping the plan's 12 criteria.

Each test names the criterion it discharges. Criteria that ask a question about
*model behaviour* (5, 10, 11) come in two forms: a harness test that always runs
against the deterministic double, and a model test that runs only when a gateway
is configured. The harness test proves the measurement works; only the model test
is evidence about a model, and it is skipped rather than faked when there is
nothing to measure.
"""
