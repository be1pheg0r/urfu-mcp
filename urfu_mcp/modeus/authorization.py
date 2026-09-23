"""Authorization policy for callers allowed to view resolved person schedules."""

from __future__ import annotations

from uuid import UUID


class ResolvedPersonAuthorizer:
    """Allow any non-nil resolved person UUID for an authenticated caller.

    The identity provider must supply a trusted Modeus person UUID. The MCP tool
    separately requires the target to be present in the complete resolver result.
    This policy intentionally does not enforce a university ACL for other people.
    """

    async def authorize(self, identity: str, person_id: str) -> bool:
        """Permit self or another valid person ID; reject malformed or nil UUIDs."""
        try:
            caller_id = UUID(identity)
            selected_id = UUID(person_id)
        except (AttributeError, TypeError, ValueError):
            return False
        return caller_id.int != 0 and selected_id.int != 0


__all__ = ["ResolvedPersonAuthorizer"]
