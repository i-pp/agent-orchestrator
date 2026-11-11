/** Example Sites-only Worker entry. No default credentials, pairing or DB.
 * Merely importing this module does not activate or provision anything.
 */
import {createApprovalService,D1ApprovalStore,sitesIdentityAdapter} from './service.mjs';
export default {
  async fetch(request,env={}) {
    return createApprovalService({
      store:env.APPROVAL_DB?new D1ApprovalStore(env.APPROVAL_DB):undefined,
      origin:env.APPROVAL_ORIGIN,
      ownerBinding:env.APPROVAL_OWNER_BINDING,
      approverSubjectSha256:env.APPROVAL_OWNER_SUBJECT_SHA256,
      machineKeySha256:env.APPROVAL_MACHINE_KEY_SHA256,
      authenticateBrowser:env.APPROVAL_TRUSTED_SITES_DISPATCH==='confirmed'
        ?sitesIdentityAdapter({trustedSitesDispatch:true}):undefined,
    }).handle(request);
  }
};
