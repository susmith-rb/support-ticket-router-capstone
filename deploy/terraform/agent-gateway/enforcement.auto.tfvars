# REQ-AUTH-007: ENFORCE. Gate passed 2026-10-31 00:14 UTC: full e2e identity suite in DRY_RUN
# with IAP data-access audit logs showing 84/84 decisions allowed (0 would-deny) after fixing
# registry name forms (project id vs number), gRPC trace export and the REST /a2a/handoff.
# Rollback: set "DRY_RUN" and re-apply this root. NOTE: the gateway LB log reports ALLOWED for
# everything in DRY_RUN; only IAP audit logs (protoPayload.serviceName="iap.googleapis.com")
# carry the real verdict.
enforcement_mode = "ENFORCE"
