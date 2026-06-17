/**
 * In-memory context mapping: {contact_id|group_id → agentZeroContextId}
 *
 * One A0 context per DM contact, one per group.
 * If A0 restarts and a context is lost, a new one is created on next message.
 */

const _map = new Map<string, string>();

/**
 * Build the mapping key.
 * For DMs: contact_id
 * For groups: group_id
 */
export function buildKey(contactId: string, groupId?: string): string {
  return groupId || contactId;
}

/**
 * Get existing context ID for a contact or group.
 * Returns undefined if no mapping exists.
 */
export function getContextId(contactId: string, groupId?: string): string | undefined {
  const key = buildKey(contactId, groupId);
  return _map.get(key);
}

/**
 * Store the mapping: contact_id|group_id → contextId
 */
export function setContextId(contactId: string, groupId: string | undefined, contextId: string): void {
  const key = buildKey(contactId, groupId);
  _map.set(key, contextId);
  console.log(`[mapper] Mapped ${groupId ? "group" : "contact"} ${key} → context ${contextId}`);
}

/**
 * Remove a mapping (e.g., if context is known to be lost).
 */
export function removeContextId(contactId: string, groupId?: string): void {
  const key = buildKey(contactId, groupId);
  _map.delete(key);
}

/**
 * Get all active mappings (for debugging/monitoring).
 */
export function getAllMappings(): Map<string, string> {
  return new Map(_map);
}
