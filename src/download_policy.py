"""Dependency-free failure policy shared by selectors and upload workers."""

DOWNLOAD_POLICY_VERSION = 1


def legacy_preview_failure(attempt: dict) -> bool:
    """Reopen old interrupted downloads once after the complete-file upgrade."""
    return (not attempt.get('permanent') and not attempt.get('download_policy_version')
            and str(attempt.get('reason') or '').startswith(
                ('download_failed: curl exit 18:', 'download_failed: curl exit 56:')))
