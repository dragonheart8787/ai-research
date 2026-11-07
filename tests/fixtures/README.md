# Adapter fixtures: SYNTHETIC, not live recordings

Every file here was written by hand to mirror the **documented request/response shape** of the service it stands in for.
None was captured from a live API (no network access and no credentials exist in this repository or in CI).

What they prove: the adapter builds the request shape the fixture describes (URL, credential header present, body keys), and parses/validates
the response shape (success, errors, truncation, refusal, malformed, oversized) fail-closed.

What they do NOT prove: that the provider still accepts exactly these parameter names, that live responses contain no extra/changed fields,
or any pricing, rate-limit or latency behaviour. A human-run live contract check is a prerequisite for enabling any real adapter
(`RecordingTransport` in `airos/agents/real/transport.py` writes fixtures from such a run; recorded files carry
`"provenance": "recorded from a live exchange ..."` so they are distinguishable from these).
