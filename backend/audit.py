"""
Audit trail: one line per sensitive action, written to logs/audit.log.

Only the action, client IP and an optional detail are recorded - never
credentials or user identity.
"""

import logging

from flask import has_request_context, request

_audit_logger = logging.getLogger("audit")


def audit_log(action: str, detail: str = ""):
    ip = request.remote_addr if has_request_context() else "-"
    if detail:
        _audit_logger.info("%s | ip=%s | %s", action, ip, detail)
    else:
        _audit_logger.info("%s | ip=%s", action, ip)
