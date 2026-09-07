"""Lightweight treatment identity shared by the engine and benchmark tooling.

Changing feedback text, repair policy, or cadence requires a new revision.
The engine owns the payload; runners only select and record the treatment.
"""

CONTINUATION_FEEDBACK_VERSION = "rejection-once-v1"
FEEDBACK_ENV = "RESCHEMA_CONTINUATION_FEEDBACK"
FEEDBACK_DEADLINE_ENV = "RESCHEMA_FEEDBACK_DEADLINE"
FEEDBACK_PROBE_CEILING_ENV = "RESCHEMA_FEEDBACK_PROBE_CEILING"
