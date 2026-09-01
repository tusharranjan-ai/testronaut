import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArtifactStatus, ReviewStage, RunStatus, type GenerationRun, type Requirement, type Review } from '../types';
import {
  approveRequirements, approveRequirementsGate, createRequirement, deleteRequirement,
  formatStatus, getRun, listRequirements, listReviews, revert, updateRequirement,
} from '../api';
import { FindingsPanel } from '../components/FindingsPanel';

export function RequirementsReview() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const id = Number(runId);

  const [run, setRun] = useState<GenerationRun | null>(null);
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState<Partial<Requirement>>({});
  const [focus, setFocus] = useState<string | undefined>();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [r, reqs, revs] = await Promise.all([
        getRun(id), listRequirements(id), listReviews(id, ReviewStage.REQUIREMENTS)]);
      setRun(r); setRequirements(reqs); setReviews(revs); setError(null);
    } catch (e) { setError(String(e)); }
  }, [id]);

  useEffect(() => { void load(); }, [load]);

  async function act(work: () => Promise<unknown>) {
    setBusy(true); setError(null);
    try { await work(); await load(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }

  function startEdit(req: Requirement) {
    setEditing(req.id);
    setDraft({ title: req.title, description: req.description,
               acceptance_criteria: req.acceptance_criteria, priority: req.priority });
  }

  const approved = requirements.filter(r => r.status === ArtifactStatus.APPROVED);
  const gateOpen = run?.status === RunStatus.AWAITING_REQUIREMENTS_APPROVAL;
  const allApproved = requirements.length > 0 && approved.length === requirements.length;

  if (!run) return <div className="screen">{error ?? 'Loading…'}</div>;

  return (
    <div className="screen review-screen">
      <div className="screen-header">
        <h1>Gate 1 · Requirements</h1>
        <span className={`status-pill status-${run.status}`}>{formatStatus(run.status)}</span>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      <div className="summary-row">
        <div className="summary-item"><span className="summary-number">{requirements.length}</span>
          <span className="summary-label">Requirements</span></div>
        <div className="summary-item"><span className="summary-number">{approved.length}</span>
          <span className="summary-label">Approved</span></div>
        <div className="summary-item"><span className="summary-number">
          {reviews.reduce((n, r) => n + r.findings.length, 0)}</span>
          <span className="summary-label">Findings</span></div>
      </div>

      <div className="review-body">
        <div className="review-main">
          <div className="section-head">
            <h2>Requirements</h2>
            <button className="btn btn-sm btn-ghost" disabled={busy}
                    onClick={() => void act(() => createRequirement({
                      run_id: id, method: 'GET', path: '/', title: 'New requirement',
                      description: '', acceptance_criteria: [], priority: 'P2' }))}>
              Add
            </button>
          </div>

          {requirements.length === 0 && <p className="muted">Nothing generated yet.</p>}

          <table className="artifact-table">
            <thead>
              <tr><th>ID</th><th>Endpoint</th><th>Title</th><th>Priority</th><th>Status</th><th /></tr>
            </thead>
            <tbody>
              {requirements.map(req => (
                <React.Fragment key={req.id}>
                  <tr className={focus === req.req_id ? 'row-focus' : undefined}
                      onClick={() => setFocus(focus === req.req_id ? undefined : req.req_id)}>
                    <td><code>{req.req_id}</code></td>
                    <td className="muted">{req.endpoint_key}</td>
                    <td>{req.title}</td>
                    <td>{req.priority}</td>
                    <td><span className={`badge badge-${req.status}`}>{req.status}</span></td>
                    <td className="row-actions" onClick={e => e.stopPropagation()}>
                      <button className="btn btn-sm btn-ghost" onClick={() => startEdit(req)}>Edit</button>
                      {req.status !== ArtifactStatus.APPROVED && (
                        <button className="btn btn-sm btn-secondary" disabled={busy}
                                onClick={() => void act(() => approveRequirements(
                                  { run_id: id, requirement_ids: [req.req_id] }))}>Approve</button>
                      )}
                      <button className="btn btn-sm btn-ghost" disabled={busy}
                              onClick={() => void act(() => deleteRequirement(req.id))}>Delete</button>
                    </td>
                  </tr>

                  {editing === req.id && (
                    <tr className="edit-row"><td colSpan={6}>
                      <label>Title
                        <input value={draft.title ?? ''}
                               onChange={e => setDraft({ ...draft, title: e.target.value })} /></label>
                      <label>Description
                        <textarea rows={3} value={draft.description ?? ''}
                                  onChange={e => setDraft({ ...draft, description: e.target.value })} /></label>
                      <label>Acceptance criteria <span className="muted">(one per line)</span>
                        <textarea rows={4} value={(draft.acceptance_criteria ?? []).join('\n')}
                                  onChange={e => setDraft({ ...draft,
                                    acceptance_criteria: e.target.value.split('\n').filter(Boolean) })} /></label>
                      <label>Priority
                        <select value={draft.priority ?? 'P2'}
                                onChange={e => setDraft({ ...draft, priority: e.target.value as Requirement['priority'] })}>
                          <option>P1</option><option>P2</option><option>P3</option>
                        </select></label>
                      <div className="row-actions">
                        <button className="btn btn-sm btn-primary" disabled={busy}
                                onClick={() => void act(async () => {
                                  await updateRequirement(req.id, draft); setEditing(null); })}>Save</button>
                        <button className="btn btn-sm btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
                      </div>
                    </td></tr>
                  )}

                  {focus === req.req_id && editing !== req.id && (
                    <tr className="detail-row"><td colSpan={6}>
                      <p>{req.description}</p>
                      {req.acceptance_criteria.length > 0 && (
                        <ul className="criteria">
                          {req.acceptance_criteria.map((c, i) => <li key={i}>{c}</li>)}
                        </ul>
                      )}
                      {req.spec_source && <p className="muted">source: <code>{req.spec_source}</code></p>}
                    </td></tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>

        <aside className="review-side">
          <FindingsPanel reviews={reviews} filterTarget={focus}
                         onRevert={(target, finding) => void act(() => revert(id, target, finding))} />
        </aside>
      </div>

      <div className="screen-footer">
        <div>
          <button className="btn btn-secondary" disabled={busy || allApproved}
                  onClick={() => void act(() => approveRequirements({ run_id: id, all: true }))}>
            Approve all
          </button>
        </div>
        <div className="footer-gate">
          {!allApproved && (
            <p className="muted">
              {requirements.length - approved.length} requirement(s) still need approval before
              test cases can be generated.
            </p>
          )}
          <button className="btn btn-primary" disabled={busy || !gateOpen || !allApproved}
                  onClick={() => void act(async () => {
                    await approveRequirementsGate(id);
                    navigate(`/runs/${id}/progress`);
                  })}>
            Approve and generate test cases
          </button>
        </div>
      </div>
    </div>
  );
}
