/** Only a completed server gate evaluation authorizes creation. A degraded
 * data-only response still needs confirmation and a second full preflight. */
export function admissionAllowsCreation(response: Record<string, any>): boolean {
  return ["ready", "degraded"].includes(response.status)
    && typeof response.report_hash === "string" && response.report_hash.length > 0
    && response.gates?.allowed === true;
}
