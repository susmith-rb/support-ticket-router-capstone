# REQ-AUTH-007: stays DRY_RUN. ENFORCE was tried on 2026-10-30 22:57 and broke production:
# the authz policy DENIED `POST .../reasoningEngines/<id>/sessions?sessionId=<caller id>`
# (session create with a caller-supplied id) on us-central1-aiplatform.mtls.googleapis.com,
# while session GET/appendEvent and all other hosts were ALLOWED. DRY_RUN logs had shown no
# denials because the smoke-style session creates did not occur in that window. Fix the
# access policy / registry entry for that path before switching to "ENFORCE" again.
enforcement_mode = "DRY_RUN"
