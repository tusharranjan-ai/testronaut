import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArtifactStatus, Category, CATEGORY_LABELS, ReviewStage, RunStatus,
  type GenerationRun, type Requirement, type Review, type TestCase, type TraceabilityReport,
} from '../types';
import {
  approveCases, approveCasesGate, deleteCase, formatStatus, getRun, getTraceability,
  listCases, listRequirements, listReviews, revert, updateCase,
} from '../api';
import { FindingsPanel } from '../components/FindingsPanel';

export function CaseReview() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const id = Number(runId);

  const [run, setRun] = useState<GenerationRun | null>(null);
  const [cases, setCases] = useState<TestCase[]>([]);
  const [requirements, setRequirements] = useState<Requirement[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [trace, setTrace] = useState<TraceabilityReport | null>(null);
  const [filter, setFilter] = useState<Category | 'all'>('all');
  const [focus, setFocus] = useState<string | undefined>();
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState<Partial<TestCase>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [r, cs, reqs, revs, tr] = await Promise.all([
        getRun(id), listCases(id), listRequirements(id),
        listReviews(id, ReviewStage.TEST_CASES), getTraceability(id)]);
      setRun(r); setCases(cs); setRequirements(reqs); setReviews(revs); setTrace(tr); setError(null);
    } catch (e) { setError(String(e)); }
  }, [id]);

  useEffect(() => { void load(); }, [load]);

  async function act(work: () => Promise<unknown>) {
    setBusy(true); setError(null);
    try { await work(); await load(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }

  const visible = filter === 'all' ? cases : cases.filter(c => c.category === filter);
  const approved = cases.filter(c => c.status === ArtifactStatus.APPROVED);
  const allApproved = cases.length > 0 && approved.length === cases.length;
  const gateOpen = run?.status === RunStatus.AWAITING_CASE_APPROVAL;

  if (!run) return <div className="screen">{error ?? 'Loading…'}</div>;

  return (
    <div className="screen review-screen">
      <div className="screen-header">
        <h1>Gate 2 · Test cases</h1>
        <span className={`status-pill status-${run.status}`}>{formatStatus(run.status)}</span>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {trace && (
        <div className={`coverage-panel ${trace.fully_traced ? 'coverage-ok' : 'coverage-warn'}`}>
          <div className="summary-row">
            <div className="summary-item"><span className="summary-number">{cases.length}</span>
              <span className="summary-label">Cases</span></div>
            <div className="summary-item"><span className="summary-number">{approved.length}</span>
              <span className="summary-label">Approved</span></div>
            <div className="summary-item">
              <span className="summary-number">{trace.covered}/{trace.total_requirements}</span>
              <span className="summary-label">Requirements covered</span></div>
          </div>
          {trace.uncovered_requirement_ids.length > 0 && (
            <p className="coverage-line">Uncovered:{' '}
              {trace.uncovered_requirement_ids.map(r => <code key={r}>{r}</code>)}</p>
          )}
          {trace.orphan_details.length > 0 && (
            <p className="coverage-line">Orphans:{' '}
              {trace.orphan_details.map(o => (
                <span key={o.case_id}><code>{o.case_id}</code> ({o.reason}) </span>))}</p>
          )}
          {trace.endpoints_without_cases.length > 0 && (
            <p className="coverage-line">No cases at all:{' '}
              {trace.endpoints_without_cases.map(e => <code key={e}>{e}</code>)}</p>
          )}
          {trace.fully_traced && <p className="coverage-line">Every approved requirement is exercised.</p>}
        </div>
      )}

      <div className="filter-row">
        <button className={`chip ${filter === 'all' ? 'chip-on' : ''}`}
                onClick={() => setFilter('all')}>All ({cases.length})</button>
        {Object.values(Category).map(cat => {
          const n = cases.filter(c => c.category === cat).length;
          return n === 0 ? null : (
            <button key={cat} className={`chip ${filter === cat ? 'chip-on' : ''}`}
                    onClick={() => setFilter(cat)}>{CATEGORY_LABELS[cat]} ({n})</button>
          );
        })}
      </div>

      <div className="review-body">
        <div className="review-main">
          <table className="artifact-table">
            <thead>
              <tr><th>ID</th><th>Category</th><th>Title</th><th>Expects</th>
                  <th>Traces to</th><th>Status</th><th /></tr>
            </thead>
            <tbody>
              {visible.map(tc => (
                <React.Fragment key={tc.id}>
                  <tr className={focus === tc.case_id ? 'row-focus' : undefined}
                      onClick={() => setFocus(focus === tc.case_id ? undefined : tc.case_id)}>
                    <td><code>{tc.case_id}</code></td>
                    <td><span className={`badge badge-${tc.category}`}>{CATEGORY_LABELS[tc.category]}</span></td>
                    <td>{tc.title}</td>
                    <td>{tc.expected_status}</td>
                    <td>{tc.requirement_ids.length === 0
                      ? <span className="badge badge-high">orphan</span>
                      : tc.requirement_ids.map(r => <code key={r}>{r}</code>)}</td>
                    <td><span className={`badge badge-${tc.status}`}>{tc.status}</span></td>
                    <td className="row-actions" onClick={e => e.stopPropagation()}>
                      <button className="btn btn-sm btn-ghost" onClick={() => {
                        setEditing(tc.id);
                        setDraft({ title: tc.title, description: tc.description,
                                   expected_status: tc.expected_status, category: tc.category,
                                   requirement_ids: tc.requirement_ids,
                                   expected_body: tc.expected_body,
                                   test_data_notes: tc.test_data_notes });
                      }}>Edit</button>
                      {tc.status !== ArtifactStatus.APPROVED && (
                        <button className="btn btn-sm btn-secondary" disabled={busy}
                                onClick={() => void act(() => approveCases(
                                  { run_id: id, case_ids: [tc.case_id] }))}>Approve</button>
                      )}
                      <button className="btn btn-sm btn-ghost" disabled={busy}
                              onClick={() => void act(() => deleteCase(tc.id))}>Delete</button>
                    </td>
                  </tr>

                  {editing === tc.id && (
                    <tr className="edit-row"><td colSpan={7}>
                      <label>Title
                        <input value={draft.title ?? ''}
                               onChange={e => setDraft({ ...draft, title: e.target.value })} /></label>
                      <label>Description
                        <textarea rows={2} value={draft.description ?? ''}
                                  onChange={e => setDraft({ ...draft, description: e.target.value })} /></label>
                      <div className="field-row">
                        <label>Expected status
                          <input type="number" value={draft.expected_status ?? 200}
                                 onChange={e => setDraft({ ...draft, expected_status: Number(e.target.value) })} /></label>
                        <label>Category
                          <select value={draft.category ?? Category.HAPPY_PATH}
                                  onChange={e => setDraft({ ...draft, category: e.target.value as Category })}>
                            {Object.values(Category).map(c =>
                              <option key={c} value={c}>{CATEGORY_LABELS[c]}</option>)}
                          </select></label>
                      </div>
                      <label>Traces to <span className="muted">(requirement IDs, one per line)</span>
                        <textarea rows={2} value={(draft.requirement_ids ?? []).join('\n')}
                                  onChange={e => setDraft({ ...draft,
                                    requirement_ids: e.target.value.split('\n').map(s => s.trim()).filter(Boolean) })} /></label>
                      <label>Assertions <span className="muted">(one per line)</span>
                        <textarea rows={3} value={(draft.expected_body ?? []).join('\n')}
                                  onChange={e => setDraft({ ...draft,
                                    expected_body: e.target.value.split('\n').filter(Boolean) })} /></label>
                      <label>Test data notes
                        <input value={draft.test_data_notes ?? ''}
                               onChange={e => setDraft({ ...draft, test_data_notes: e.target.value })} /></label>
                      <div className="row-actions">
                        <button className="btn btn-sm btn-primary" disabled={busy}
                                onClick={() => void act(async () => {
                                  await updateCase(tc.id, draft); setEditing(null); })}>Save</button>
                        <button className="btn btn-sm btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
                      </div>
                    </td></tr>
                  )}

                  {focus === tc.case_id && editing !== tc.id && (
                    <tr className="detail-row"><td colSpan={7}>
                      <p>{tc.description}</p>
                      <dl className="case-detail">
                        {tc.preconditions.length > 0 &&
                          <><dt>Preconditions</dt><dd>{tc.preconditions.join(' · ')}</dd></>}
                        {Object.keys(tc.path_params).length > 0 &&
                          <><dt>Path params</dt><dd><code>{JSON.stringify(tc.path_params)}</code></dd></>}
                        {Object.keys(tc.query_params).length > 0 &&
                          <><dt>Query</dt><dd><code>{JSON.stringify(tc.query_params)}</code></dd></>}
                        {Object.keys(tc.headers).length > 0 &&
                          <><dt>Headers</dt><dd><code>{JSON.stringify(tc.headers)}</code></dd></>}
                        {tc.body != null && <><dt>Body</dt><dd><code>{JSON.stringify(tc.body)}</code></dd></>}
                        {tc.expected_body.length > 0 &&
                          <><dt>Assertions</dt><dd><ul className="criteria">
                            {tc.expected_body.map((a, i) => <li key={i}>{a}</li>)}</ul></dd></>}
                        {tc.test_data_notes && <><dt>Test data</dt><dd>{tc.test_data_notes}</dd></>}
                      </dl>
                      {tc.requirement_ids.map(rid => {
                        const req = requirements.find(r => r.req_id === rid);
                        return req ? <p key={rid} className="muted">
                          <code>{rid}</code> — {req.title}</p> : null;
                      })}
                    </td></tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
          {visible.length === 0 && <p className="muted">No cases in this category.</p>}
        </div>

        <aside className="review-side">
          <FindingsPanel reviews={reviews} filterTarget={focus}
                         onRevert={(target, finding) => void act(() => revert(id, target, finding))} />
        </aside>
      </div>

      <div className="screen-footer">
        <button className="btn btn-secondary" disabled={busy || allApproved}
                onClick={() => void act(() => approveCases({ run_id: id, all: true }))}>
          Approve all
        </button>
        <div className="footer-gate">
          {!allApproved && <p className="muted">
            {cases.length - approved.length} case(s) still need approval.</p>}
          <button className="btn btn-primary" disabled={busy || !gateOpen || !allApproved}
                  onClick={() => void act(async () => {
                    await approveCasesGate(id);
                    navigate(`/runs/${id}/export`);
                  })}>
            Approve and finish
          </button>
        </div>
      </div>
    </div>
  );
}
