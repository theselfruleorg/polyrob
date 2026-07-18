"""core.security — tier-0 security primitives (R-4 promotion, 2026-07-17).

Pure, dependency-light guards shared by every tier: secret/credential path
detection (secret_guard), untrusted-content framing (untrusted_wrap), and the
forged-turn kind constants (forged_turns). Formerly under
``agents/task/agent/core/``; the old paths remain as re-export shims.
"""
