# Incidents

Incident reports preserve observed production evidence and the timeline it was
observed on. They are written to keep the record, not to describe the system as
it stands today.

Conventions for documents in this directory:

- **Observed facts, inferred mechanisms, and operational conclusions are labelled
  separately.** A report says plainly which of its claims were measured, which are
  inferences drawn from those measurements, and which are decisions taken in
  response. Confidence levels and falsifiers belong with the claim they qualify.
- **Incident docs are historical evidence, not automatically current
  architecture.** A report describes the system at the moment of the incident.
  Later work may have changed, mitigated, or invalidated what it records, and the
  report is not updated to track that.
- **Current operational truth belongs in the canonical docs.** If an incident
  produces a lasting rule, guard, or configuration, that outcome is written into
  the canonical documentation; the report retains only the evidence and the
  reasoning that led there.

## Reports

- [brcmfmac SDIO wedge under sustained host load](2026-08-19-brcmfmac-sdio-wedge.md)
