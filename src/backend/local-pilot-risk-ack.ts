/** The acknowledgement is a server enforced operator input, never a gate override. */
export function hasPapiProtectionRiskAcknowledgement(body: unknown): boolean {
  return Boolean(body && typeof body === 'object' && !Array.isArray(body)
    && (body as Record<string, unknown>).papiProtectionRiskAcknowledged === true);
}
