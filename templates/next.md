Handle exactly request `@EVENT@` for task `@TASK@`, topic `@TOPIC@`.
Read it using `a2a read '@ROOM@' --event '@EVENT@' --json`. Verify event_id,
sender `@ORCH@`, room `@ROOM@`, kind `request`, recipient `@ID@`, and task `@TASK@`.
Follow the standing worker protocol: board authority and every precondition,
ack, exact brief, signed feature delivery or independent exact-head review,
verified result attachment, and handover in `@TOPIC@`. Then clean task scratch
and end the turn. Do not select the latest inbox entry. Earlier or later queued
requests are separate runs; an inbox receipt does not replace this event selector.
