# EdgeFleet security and launch readiness

This is an engineering control assessment, not a legal opinion, penetration-test certificate, functional-safety certification, ISO certification, or guarantee of compliance.

## Verified controls

| Control | Implementation |
| --- | --- |
| Database boundary | The backend alone receives `DATABASE_URL`; the browser cannot connect to PostgreSQL. |
| Schema management | Alembic revisions are applied separately from application startup. |
| ORM transactions | SQLAlchemy `AsyncSession.begin()` commits successful mutations and rolls back failures. Reads use bounded sessions. |
| Password storage | Argon2 hashing runs off the event loop; plaintext credentials are not persisted. |
| Authentication | Short-lived HS256 JWTs require `sub`, `iss`, `aud`, `iat`, `nbf`, `exp`, and `jti`. Production refuses secrets shorter than 256 bits. |
| Authorization | Current active roles are loaded from PostgreSQL on every request; viewer/operator/admin/fleet-agent permissions are enforced server-side. |
| Input and query safety | Pydantic constrains values; SQLAlchemy ORM and bound expressions prevent request-driven SQL concatenation. |
| Browser/API protection | Exact CORS and host allowlists, rate limiting, CSP, frame denial, no-sniff, no-referrer, no-store, request IDs, production HSTS. |

## Required before public or physical deployment

- Rotate the database password previously supplied through chat and store the replacement in a secret manager.
- Replace the bootstrap password and JWT secret, provision the first administrator, then remove `BOOTSTRAP_ADMIN_PASSWORD` from production configuration.
- Terminate TLS at a trusted reverse proxy, use a least-privilege database role, restrict database ingress, and use a shared rate limiter for multiple API replicas.
- Add token revocation/refresh if sessions must outlive the configured 15-minute access token; otherwise require sign-in again.
- Run SAST, dependency/SBOM and license scans, DAST/penetration testing, backup-restore drills, accessibility review, and privacy/legal assessment.
- Complete physical AMR hazard analysis, protective-field and emergency-stop validation, and applicable qualified safety assessment.

PostgreSQL, FastAPI, JWT authentication, and the dashboard do not issue wheel commands and are not emergency-stop systems. On loss of these services, each AMR must stop/yield using onboard safety logic.
