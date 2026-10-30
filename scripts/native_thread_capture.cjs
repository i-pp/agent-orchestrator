/* Read-only Codex app backchannel. No spawn, messaging, shell or model API.
 * The caller supplies readThread and a local saveCapture function. This module
 * also runs in Code Mode; it has no Node filesystem/network dependencies.
 */
'use strict';
const uuid = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const finite = x => typeof x === 'number' && Number.isFinite(x) && x >= 0;

function unpack(result) {
  if (result?.isError) throw Error('THREAD_READ_FAILED');
  if (result?.schemaVersion === 1) return result;
  const text = result?.content?.filter(x => x.type === 'text');
  if (text?.length !== 1) throw Error('UNSUPPORTED_THREAD_ENVELOPE');
  const value = JSON.parse(text[0].text);
  if (value.schemaVersion !== 1) throw Error('UNSUPPORTED_THREAD_SCHEMA');
  return value;
}

async function captureExistingThread(api, spec) {
  if (typeof api?.readThread !== 'function' || typeof api?.saveCapture !== 'function')
    throw Error('READ_OR_CAPTURE_PORT_NOT_CONNECTED');
  if (!uuid.test(spec?.parentThreadId) || !/^\/root(?:\/[a-z0-9_]+)+$/.test(spec?.agentPath)
      || !finite(spec?.deadlineAt)) throw Error('INVALID_CAPTURE_SCOPE');
  const clock = api.clock ? (() => api.clock()) : (() => Date.now() / 1000);
  const lookupStarted = clock();
  const parent = unpack(await api.readThread({threadId: spec.parentThreadId,
    turnLimit: 3, includeOutputs: false, maxOutputCharsPerItem: 4096}));
  const lookupFinished = clock();
  if (parent.thread?.id !== spec.parentThreadId || !Array.isArray(parent.turns))
    throw Error('PARENT_THREAD_MISMATCH');
  const matches = parent.turns.flatMap(t => (t.items || []).filter(i =>
    i.type === 'subAgentActivity' && i.agentPath === spec.agentPath).map(i => ({...i, rootTurnId: t.id})));
  const ids = [...new Set(matches.map(x => x.agentThreadId))];
  if (ids.length !== 1 || !uuid.test(ids[0])) throw Error('NATIVE_THREAD_UNRESOLVED_OR_AMBIGUOUS');
  const pollStarted = clock();
  const child = unpack(await api.readThread({threadId: ids[0], turnLimit: 2,
    includeOutputs: false, maxOutputCharsPerItem: 4096}));
  const pollFinished = clock();
  if (child.thread?.id !== ids[0] || !Array.isArray(child.turns) || child.turns.length !== 1)
    throw Error('CHILD_THREAD_OR_TURN_MISMATCH');
  const turn = child.turns[0];
  if (!['completed', 'inProgress'].includes(turn.status)) throw Error('UNSUPPORTED_TURN_STATUS');
  if (!uuid.test(turn.id) || !finite(turn.startedAt)) throw Error('INVALID_PROVIDER_TIME');
  const completed = turn.status === 'completed';
  if (completed && (!finite(turn.completedAt) || turn.completedAt < turn.startedAt))
    throw Error('INVALID_PROVIDER_TIME');
  const final = (turn.items || []).filter(i => i.type === 'agentMessage' && i.phase === 'final_answer');
  if (completed && !turn.error && (final.length !== 1 || typeof final[0].text !== 'string'
      || final[0].textTruncated === true)) throw Error('FINAL_ANSWER_UNAVAILABLE_OR_TRUNCATED');
  const capture = Object.freeze({schema: 1, source: 'codex_app.read_thread',
    parent_thread_id: spec.parentThreadId, agent_path: spec.agentPath, child_thread_id: ids[0],
    child_turn_id: turn.id, root_turn_id: matches[0].rootTurnId,
    state: turn.error ? 'FAILED' : completed ? 'COMPLETED' : 'RUNNING',
    answer: completed && !turn.error ? final[0].text : null,
    provider_started_at: turn.startedAt, provider_completed_at: completed ? turn.completedAt : null,
    provider_duration_ms: finite(turn.durationMs) ? turn.durationMs : null,
    lookup_started_at: lookupStarted, lookup_finished_at: lookupFinished,
    poll_started_at: pollStarted, poll_finished_at: pollFinished,
    deadline_at: spec.deadlineAt, provider_finished_before_deadline: completed ? turn.completedAt < spec.deadlineAt : null,
    poll_returned_before_deadline: pollFinished < spec.deadlineAt,
    actual_model: typeof child.thread.model === 'string' ? child.thread.model : null,
    billing_classification: null});
  if (![lookupStarted, lookupFinished, pollStarted, pollFinished].every(finite)
      || !(lookupStarted <= lookupFinished && lookupFinished <= pollStarted && pollStarted <= pollFinished))
    throw Error('LOCAL_CAPTURE_CLOCK_UNKNOWN');
  // Persist in the same awaited call, before handing control back to the parent.
  const acknowledgement = await api.saveCapture(capture);
  return {capture, acknowledgement, model_calls: 0, changes_prior_run: false};
}

async function observeIntoAdapter(api, spec) {
  if (typeof api?.runLocalAdapter !== 'function' || !spec?.requestRef)
    throw Error('LOCAL_ADAPTER_PORT_NOT_CONNECTED');
  return captureExistingThread({readThread: args => api.readThread(args),
    clock: api.clock ? (() => api.clock()) : undefined,
    saveCapture: async capture => {
      const reply = await api.runLocalAdapter('thread_observation', {request_ref: spec.requestRef, capture});
      const events = reply?.result?.registration;
      if (reply?.status !== 'OK' || !Array.isArray(events) || events.length !== 1
          || !(events[0].applied === true || events[0].duplicate === true))
        throw Error('LOCAL_OBSERVATION_NOT_CONFIRMED');
      return reply;
    }}, spec);
}

// Bind a fixed local CLI, not an arbitrary-command port. JSON is passed as one
// POSIX-quoted stdin value; shell metacharacters in paths/body stay literal.
function makeCliPort(execCommand, projectRoot, runDir) {
  const paths = [projectRoot, runDir];
  if (typeof execCommand !== 'function' || paths.some(p => typeof p !== 'string'
      || !p.startsWith('/') || /[\x00\r\n]/.test(p))) throw Error('FIXED_CLI_PATHS_REQUIRED');
  const quote = s => "'" + s.replace(/'/g, "'\"'\"'") + "'";
  const operations = new Set(['readonly_prepare', 'handoff_request', 'step', 'context',
    'bind_from_thread', 'thread_observation', 'readonly_verified']);
  return async (operation, body) => {
    if (!operations.has(operation)) throw Error('FIXED_CLI_OPERATION_REQUIRED');
    const json = JSON.stringify(body);
    const bytes = [...json].reduce((n, c) => n + (c.codePointAt(0) <= 0x7f ? 1
      : c.codePointAt(0) <= 0x7ff ? 2 : c.codePointAt(0) <= 0xffff ? 3 : 4), 0);
    if (bytes > 65536) throw Error('CLI_INPUT_SIZE_LIMIT');
    const cmd = 'printf %s ' + quote(json) + ' | python3 -B '
      + quote(projectRoot + '/scripts/native_parent.py') + ' ' + operation + ' --run ' + quote(runDir);
    const result = await execCommand({cmd, login: false, yield_time_ms: 10000, max_output_tokens: 10000});
    if (result?.session_id !== undefined || result?.exit_code !== 0 || typeof result?.output !== 'string')
      throw Error('CLI_OUTCOME_REQUIRES_RECONCILIATION');
    const reply = JSON.parse(result.output);
    if (reply?.status !== 'OK' || reply.operation !== operation) throw Error('CLI_ACK_NOT_CONFIRMED');
    return reply;
  };
}

async function local(api, op, body) {
  if (typeof api?.runLocalAdapter !== 'function') throw Error('LOCAL_ADAPTER_PORT_NOT_CONNECTED');
  const reply = await api.runLocalAdapter(op, body);
  if (reply?.status !== 'OK' || reply.operation !== op) throw Error('CLI_ACK_NOT_CONFIRMED');
  return reply.result;
}

async function prepareReadonly(api) {
  const step = await local(api, 'readonly_prepare', {});
  if (step.profile !== 'collaboration-parent-v3' || step.tool_calls?.length !== 1
      || step.parent_jobs?.length || step.pending_reconciliation?.length)
    throw Error('SINGLE_FRESH_SPAWN_REQUIRED_NO_REPLAY');
  const call = step.tool_calls[0];
  if (call.tool !== 'collaboration.spawn_agent' || call.arguments?.fork_turns !== 'none')
    throw Error('SINGLE_FRESH_SPAWN_REQUIRED_NO_REPLAY');
  // Returned arguments are exactly the fixed short bootstrap. The parent must
  // use its standard tool once; collaboration is unavailable inside Code Mode.
  return {tool: call.tool, arguments: call.arguments, request_ref: call.request_ref,
    expected_agent_name: call.expected_agent_name, deadline_at: call.not_after,
    model_calls: 0, automatic_spawn: false};
}

async function finishReadonly(api, spec) {
  if (!uuid.test(spec?.parentThreadId) || !spec.spawnRequestRef
      || typeof api?.readThread !== 'function' || typeof api?.pause !== 'function')
    throw Error('READONLY_CALLER_PORTS_REQUIRED');
  const spawn = await local(api, 'handoff_request', {request_ref: spec.spawnRequestRef});
  const scope = {parentThreadId: spec.parentThreadId, agentPath: spawn.expected_agent_name,
    deadlineAt: spawn.not_after};
  if (!spawn.binding_confirmed) {
    await captureExistingThread({readThread: x => api.readThread(x),
      clock: api.clock ? (() => api.clock()) : undefined,
      saveCapture: async capture => {
        const bound = await local(api, 'bind_from_thread', {request_ref: spec.spawnRequestRef, capture});
        if (!(bound.registration?.applied || bound.registration?.duplicate))
          throw Error('SPAWN_BIND_NOT_CONFIRMED_NO_REPLAY');
        return bound;
      }}, scope);
  }
  let captureRef = null;
  for (let count = 0; count < 30; count++) {
    const step = await local(api, 'step', {});
    if (step.progress?.status === 'COMPLETE') {
      const states = Object.values(step.progress.states || {});
      const final = await local(api, 'handoff_request', {request_ref: spec.spawnRequestRef});
      return {status: states.length === 1 && states[0] === 'SUCCEEDED'
        && step.progress.unreleased === 0 ? 'SUCCEEDED' : 'NOT_ADOPTED',
        progress: step.progress, receipt: final.receipt, model_calls: 0, automatic_spawn: false};
    }
    if (step.pending_reconciliation?.length || step.progress?.status === 'BLOCKED')
      return {status: 'RECONCILIATION_REQUIRED', progress: step.progress, model_calls: 0};
    for (const call of step.tool_calls || []) {
      if (call.tool !== 'collaboration.list_agents' || call.action_ids.length !== 1)
        return {status: 'RECONCILIATION_REQUIRED', reason: 'NATIVE_INTERRUPT_OR_NEW_SPAWN_NOT_AUTOMATED',
          progress: step.progress, model_calls: 0};
      const observed = await observeIntoAdapter(api, {...scope, requestRef: call.request_ref});
      captureRef = observed.acknowledgement.result.capture_ref;
    }
    for (const job of step.parent_jobs || []) {
      if (job.kind !== 'VERIFY') throw Error('FIXED_VERIFICATION_INPUT_REQUIRED');
      const verified = await local(api, 'readonly_verified', {action_id: job.action_id,
        capture_ref: captureRef, spawn_request_ref: spec.spawnRequestRef});
      if (!(verified.registration?.applied || verified.registration?.duplicate))
        return {status: 'RECONCILIATION_REQUIRED', reason: 'VERIFICATION_NOT_CONFIRMED',
          state: verified.state, model_calls: 0};
    }
    // One bounded call; no background wake. A pending return never respawns.
    if (!(step.parent_jobs || []).length) await api.pause(1000);
  }
  return {status: 'PENDING', reason: 'CALLER_CYCLE_LIMIT', model_calls: 0, automatic_spawn: false};
}

module.exports = {captureExistingThread, observeIntoAdapter, makeCliPort, prepareReadonly, finishReadonly};
